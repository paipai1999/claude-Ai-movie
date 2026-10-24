"""
Translation flow - batches with memory, validation and graceful degradation
===========================================================================
Drives the translation of every dialogue line. The Gemini calls themselves are supplied by the
caller (``WriterAgent``), so this module is pure orchestration and fully testable with fakes.

For each window of ``BATCH_SIZE`` lines:

1. ask for the batch, passing the last few *already translated* lines (so voice, tense and names
   continue) and the next few *source* lines (so a sentence cut by a segment boundary reads right);
2. validate the answer (``core.translation_quality``): missing lines, untranslated or English-leaking
   lines, copies of the source, repeated neighbours;
3. re-ask ONLY for the bad lines, telling the model exactly what was wrong;
4. if the model returned nothing usable at all (blocked, cut off, unparsable), split the batch in two
   and solve each half - a long batch is the usual cause, and a split isolates a blocked line;
5. as a last resort translate a line on its own; if even that fails the line keeps its source text but
   is flagged ``needs_review`` with the reasons, instead of being dubbed silently.

If the model *service* itself fails several calls in a row, translation stops with
``TranslationUnavailable`` rather than spending hours on retries and caching a half-translated script.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from brain.diagnostics import log_suppressed
from brain.gemini_client import GeminiContentError
from core.translation_quality import RETRY_CODES, Problem, feedback_text, find_problems

BATCH_SIZE = 20
MIN_SPLIT = 4          # a batch this small is not split further; its lines are done one by one
LOOKBACK = 3           # already translated lines shown to the model as context
LOOKAHEAD = 3          # upcoming source lines shown to the model as context
MAX_RETRIES = 2        # corrective re-asks per window before the per-line fallback
OUTAGE_LIMIT = 3       # consecutive failed model calls that mean "the service is down"

# Problems that make a line unusable (it must be retried); the rest are reported on the line only.
BLOCKING = RETRY_CODES


class TranslationUnavailable(RuntimeError):
    """The model service failed repeatedly; ``partial`` holds the lines translated before that."""

    def __init__(self, message: str, partial: Optional[List["LineResult"]] = None):
        super().__init__(message)
        self.partial = partial or []


@dataclass
class LineResult:
    """One translated dialogue line."""
    seg_id: int
    narration: str
    emotion: str = "normal"
    gender: str = "male"
    character: str = "Narrator"
    issues: List[str] = field(default_factory=list)   # problem codes still present after all retries
    origin: str = "batch"                             # batch | retry | line | source

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)


# Callables supplied by the caller:
#   translate_batch(lines, context, feedback) -> parsed items (dicts with "id"/"narration"/...) or None
#   translate_line(line, context)             -> plain text or None
# ``context`` is {"previous": [{id, narration}], "next": [{id, text}]}.
BatchFn = Callable[[List[dict], Dict[str, list], str], Optional[List[dict]]]
LineFn = Callable[[dict, Dict[str, list]], Optional[str]]

_SERVICE, _CONTENT = "service", "content"


class TranslationFlow:
    def __init__(
        self,
        translate_batch: BatchFn,
        translate_line: LineFn,
        *,
        pre_clean: Callable[[str], str] = lambda t: t,
        final_clean: Callable[[str], str] = lambda t: t,
        burmese_target: bool = True,
        default_gender: Callable[[dict], str] = lambda seg: "male",
        log: Callable[[str], None] = print,
        check_cancel: Callable[[], None] = lambda: None,
    ):
        self._translate_batch = translate_batch
        self._translate_line = translate_line
        self._pre_clean = pre_clean          # numbers / acronyms -> words (applied before validation)
        self._final_clean = final_clean      # strip stray scripts / punctuation (applied last)
        self._burmese = burmese_target
        self._default_gender = default_gender
        self._log = log
        self._check_cancel = check_cancel
        self._failed_calls = 0               # consecutive service failures (reset by any success)
        self._done: List[LineResult] = []    # lines finished so far, in order
        self.stats = {"batches": 0, "retries": 0, "splits": 0, "line_fallbacks": 0, "flagged": 0}

    # ── public ─────────────────────────────────────────────────────────────────
    def run(self, segments: Sequence[dict]) -> List[LineResult]:
        """Translates every segment (``{"id", "text", ...}``); returns one ``LineResult`` per segment."""
        segments = list(segments)
        total_batches = (len(segments) + BATCH_SIZE - 1) // BATCH_SIZE
        try:
            for n, start in enumerate(range(0, len(segments), BATCH_SIZE), 1):
                self._check_cancel()
                batch = segments[start:start + BATCH_SIZE]
                self._log(f"[*] Translating batch {n}/{total_batches} ({len(batch)} lines)...")
                upcoming = segments[start + len(batch):start + len(batch) + LOOKAHEAD]
                self._done.extend(self._solve(batch, upcoming, ()))
        except TranslationUnavailable as exc:
            exc.partial = list(self._done)
            raise
        self.stats["flagged"] = sum(1 for r in self._done if r.needs_review)
        return list(self._done)

    # ── one window ─────────────────────────────────────────────────────────────
    def _solve(self, batch: List[dict], upcoming: Sequence[dict], pending_done: Sequence[LineResult]) -> List[LineResult]:
        """
        Translates ``batch``, the lines directly after ``self._done`` + ``pending_done`` (the finished
        left half while a split batch is being solved).
        """
        ids = [s["id"] for s in batch]
        by_id = {s["id"]: s for s in batch}
        best: Dict[int, dict] = {}          # id -> best candidate so far
        origin: Dict[int, str] = {}
        ask_list = list(batch)
        feedback = ""
        service_down = False
        problems: List[Problem] = []

        for attempt in range(1 + MAX_RETRIES):
            self._check_cancel()
            got, failure = self._ask(ask_list, self._context(upcoming, pending_done), feedback)
            self.stats["batches" if attempt == 0 else "retries"] += 1
            if failure == _SERVICE:
                service_down = True
                break                       # the call layer already retried; asking again now is pointless
            if not got and len(batch) > MIN_SPLIT and (failure == _CONTENT or attempt == 0):
                return self._split(batch, upcoming, pending_done)   # blocked / cut off / nothing parsable

            for sid, cand in got.items():
                if sid in by_id and (sid not in best or self._better(cand, best[sid], by_id[sid])):
                    best[sid] = cand
                    origin[sid] = "batch" if attempt == 0 else "retry"

            problems = self._validate(batch, best, pending_done)
            blocking = {p.seg_id for p in problems if p.code in BLOCKING}
            if not blocking or failure == _CONTENT:
                break                       # done - or blocked again, so isolate the lines one by one
            ask_list = [by_id[i] for i in ids if i in blocking]
            feedback = feedback_text(p for p in problems if p.seg_id in blocking)

        problems = self._validate(batch, best, pending_done)
        if not service_down:
            # Last resort: still-unusable lines are translated on their own, with their neighbours as context.
            for sid in sorted({p.seg_id for p in problems if p.code in BLOCKING}):
                self._check_cancel()
                fixed = self._line_fallback(by_id[sid], best.get(sid), pending_done, [best[i] for i in ids if i < sid and i in best])
                if fixed is not None:
                    best[sid] = fixed
                    origin[sid] = "line"
            problems = self._validate(batch, best, pending_done)

        issues = self._group_codes(problems)
        results: List[LineResult] = []
        for seg in batch:
            sid = seg["id"]
            cand = best.get(sid) or {}
            narration = self._final_clean(cand["narration"]) if cand.get("narration") else ""
            line_issues = list(issues.get(sid, []))
            line_origin = origin.get(sid, "source")
            if not narration:
                # Nothing usable came back: keep the source so the timeline stays complete, and flag it.
                narration = str(seg["text"])
                line_issues = sorted(set(line_issues) | {"missing"})
                line_origin = "source"
            results.append(LineResult(
                seg_id=sid,
                narration=narration,
                emotion=cand.get("emotion", "normal"),
                gender=cand.get("gender") or self._default_gender(seg),
                character=cand.get("character", "Narrator"),
                issues=line_issues,
                origin=line_origin,
            ))
        return results

    def _split(self, batch: List[dict], upcoming: Sequence[dict], pending_done: Sequence[LineResult]) -> List[LineResult]:
        """The whole-batch call produced nothing usable: solve each half separately."""
        self.stats["splits"] += 1
        half = max(1, len(batch) // 2)
        left, right = batch[:half], batch[half:]
        self._log(f"[*] Splitting a batch of {len(batch)} lines into {len(left)} + {len(right)}...")
        left_res = self._solve(left, list(right) + list(upcoming), pending_done)
        right_res = self._solve(right, upcoming, list(pending_done) + left_res)
        return left_res + right_res

    # ── validation / context ───────────────────────────────────────────────────
    def _context(self, upcoming: Sequence[dict], pending_done: Sequence[LineResult]) -> Dict[str, list]:
        done = list(self._done) + list(pending_done)
        return {
            "previous": [{"id": r.seg_id, "narration": r.narration} for r in done[-LOOKBACK:]],
            "next": [{"id": s["id"], "text": s["text"]} for s in list(upcoming)[:LOOKAHEAD]],
        }

    def _validate(self, batch: Sequence[dict], best: Dict[int, dict], pending_done: Sequence[LineResult]) -> List[Problem]:
        done = list(self._done) + list(pending_done)
        return find_problems(
            {s["id"]: (best.get(s["id"]) or {}).get("narration") for s in batch},
            burmese_target=self._burmese,
            source={s["id"]: s["text"] for s in batch},
            previous_tail=done[-1].narration if done else "",
        )

    @staticmethod
    def _group_codes(problems: Sequence[Problem]) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for p in problems:
            codes = out.setdefault(p.seg_id, [])
            if p.code not in codes:
                codes.append(p.code)
        return out

    def _blocking_count(self, cand: dict, seg: dict) -> int:
        found = find_problems({seg["id"]: cand.get("narration")}, burmese_target=self._burmese, source={seg["id"]: seg["text"]})
        return sum(1 for p in found if p.code in BLOCKING)

    def _better(self, new: dict, old: dict, seg: dict) -> bool:
        """A retried candidate replaces the earlier one only when it has fewer blocking problems."""
        return self._blocking_count(new, seg) < self._blocking_count(old, seg)

    # ── model calls ────────────────────────────────────────────────────────────
    def _ask(self, lines: List[dict], context: Dict[str, list], feedback: str) -> Tuple[Dict[int, dict], Optional[str]]:
        """
        One model call -> ``({id: candidate}, failure)`` with failure None | "service" | "content".
        Raises ``TranslationUnavailable`` once the service has failed ``OUTAGE_LIMIT`` calls in a row.
        """
        try:
            items = self._translate_batch(lines, context, feedback)
        except InterruptedError:
            raise
        except GeminiContentError as exc:
            self._log(f"[WARN] The model blocked or cut off an answer ({str(exc)[:120]}); retrying with smaller pieces.")
            return {}, _CONTENT
        except Exception as exc:
            self._failed_calls += 1
            self._log(f"[WARN] Translation call failed ({type(exc).__name__}: {str(exc)[:140]}).")
            if self._failed_calls >= OUTAGE_LIMIT:
                raise TranslationUnavailable(
                    f"The Gemini service failed {OUTAGE_LIMIT} calls in a row - translation stopped instead of "
                    "retrying for hours. Try again later; earlier phases are kept. / "
                    "Gemini ချိတ်ဆက်မှု ဆက်တိုက်မအောင်မြင်ပါ - နောက်မှ ပြန်စမ်းပါ၊ ယခင်အဆင့်တွေ ဆက်သုံးနိုင်ပါတယ်။"
                ) from exc
            return {}, _SERVICE
        self._failed_calls = 0
        out: Dict[int, dict] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                sid = int(item["id"])
            except (KeyError, TypeError, ValueError) as exc:
                log_suppressed("agents.translation_flow._ask", exc)
                continue
            text = str(item.get("narration") or item.get("burmese") or item.get("translation") or "").strip()
            out[sid] = {
                "narration": self._pre_clean(text) if text else "",
                "emotion": str(item.get("emotion", "normal")).strip() or "normal",
                "gender": self._gender(item, text),
                "character": str(item.get("character") or "Narrator").strip() or "Narrator",
            }
        return out, None

    @staticmethod
    def _gender(item: dict, narration: str) -> str:
        gender = str(item.get("gender") or item.get("speaker_gender") or "male").strip().lower()
        if gender in ("child", "boy"):
            return "female" if "သမီး" in narration else "male"
        return gender if gender in ("male", "female") else "male"

    def _line_fallback(self, seg: dict, current: Optional[dict], pending_done: Sequence[LineResult],
                       earlier: Sequence[dict]) -> Optional[dict]:
        """Translates one stubborn line on its own; returns the new candidate only when it is better."""
        self.stats["line_fallbacks"] += 1
        context = self._context([], pending_done)
        if earlier and earlier[-1].get("narration"):
            context["previous"] = (context["previous"] + [{"id": seg["id"] - 1, "narration": earlier[-1]["narration"]}])[-LOOKBACK:]
        try:
            text = self._translate_line(seg, context)
        except InterruptedError:
            raise
        except GeminiContentError as exc:
            log_suppressed("agents.translation_flow._line_fallback", exc)
            return None
        except Exception as exc:
            self._failed_calls += 1
            log_suppressed("agents.translation_flow._line_fallback", exc)
            if self._failed_calls >= OUTAGE_LIMIT:
                raise TranslationUnavailable("The Gemini service keeps failing - translation stopped.") from exc
            return None
        self._failed_calls = 0
        text = str(text or "").strip()
        if not text:
            return None
        cand = dict(current or {"emotion": "normal", "gender": self._default_gender(seg), "character": "Narrator"})
        cand["narration"] = self._pre_clean(text)
        if current and current.get("narration") and not self._better(cand, current, seg):
            return None
        return cand
