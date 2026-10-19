"""
Project files (outputs/<project>)
=================================
Everything the dashboard needs to work with a finished (or half-finished) project, with no FastAPI
in sight so it can be unit-tested against a temp folder:

* ``load_script`` / ``save_script``  - the Script Editor. Edits are written to ``state.json`` AND
  ``checkpoint.json`` and the voice/merge/QA phases are un-completed, so the next run re-voices only
  the lines whose text changed (the voice manifest fingerprints every clip) and re-renders the video.
* ``quality_report`` / ``write_quality_report`` - what still needs a human look: lines the translator
  could not fix, over-long lines, clips that had to be stretched, render warnings.
* ``save_job_options`` / ``load_job_options`` - the settings a project was started with, so
  "re-render" can reuse them exactly (a changed script option would otherwise throw the edits away).
* ``storage_summary`` - disk usage of outputs / movies / temp.
"""

import datetime
import json
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional

from agents.story_memory import parse_bible
from brain.diagnostics import log_suppressed
from core.paths import resolve_inside
from core.translation_quality import OVER_BUDGET_RATIO, find_damaged_lines

STATE_FILE = "state.json"
CHECKPOINT_FILE = "checkpoint.json"
REPORT_FILE = "quality_report.json"
JOB_OPTIONS_FILE = "job_options.json"
SCRIPT_TXT_FILE = "final_recap_script.txt"

# Phase ids (kept in sync with agents.master - a test asserts they match).
PHASE_VOICE = "phase_5_voice_generation"
PHASE_MERGE = "phase_6_video_merge"
PHASE_QA = "phase_7_qa"
RERENDER_PHASES = (PHASE_VOICE, PHASE_MERGE, PHASE_QA)

MAX_NARRATION_CHARS = 1200
MAX_EDITS_PER_CALL = 5000
CHARS_PER_SEC = 11.0      # the writer's Burmese speaking-rate budget (see WriterAgent._build_raw_segments)
MIN_SLOT_SEC = 2.2
STRETCH_LIMIT = 1.15      # VoiceAgent never speeds a clip up/slows it down by more than this
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ProjectError(ValueError):
    """A request that cannot be served (bad name, nothing to edit, invalid edit). ``code`` is machine-readable."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# ── paths / json ───────────────────────────────────────────────────────────────
def resolve_project(base_dir: str, name: str) -> str:
    """Absolute project folder for ``name`` (a path relative to ``base_dir``); raises ``ProjectError``."""
    path = resolve_inside(base_dir, name) if name else None
    if not path or not os.path.isdir(path):
        raise ProjectError(f"Project not found: {name!r}", code="not_found")
    return path


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log_suppressed("services.projects._read_json", exc)
        return None


def _write_json(path: str, data: Any, indent: int = 4) -> None:
    """Atomic write (temp file + replace) so a crash can never leave a half-written state.json."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ── script view ────────────────────────────────────────────────────────────────
def slot_budget(block: Dict[str, Any]) -> Dict[str, Any]:
    """Time slot and character budget of one script block (same rule the translator was given)."""
    start = float(block.get("start_sec") or 0.0)
    end = float(block.get("end_sec") or (start + 3.0))
    slot = max(MIN_SLOT_SEC, end - start)
    return {"slot_sec": round(slot, 2), "max_chars": max(24, int(slot * CHARS_PER_SEC))}


def _block_view(index: int, block: Dict[str, Any]) -> Dict[str, Any]:
    narration = str(block.get("narration", ""))
    budget = slot_budget(block)
    return {
        "index": index,
        "scene_id": str(block.get("scene_id", index + 1)),
        "start_sec": block.get("start_sec"),
        "end_sec": block.get("end_sec"),
        "source_text": block.get("source_text") or "",
        "narration": narration,
        "character": block.get("character") or "",
        "gender": block.get("gender") or "male",
        "emotion": block.get("emotion") or "normal",
        "needs_review": bool(block.get("needs_review")),
        "issues": list(block.get("issues") or []),
        "edited": bool(block.get("edited")),
        "chars": len(narration),
        "max_chars": budget["max_chars"],
        "slot_sec": budget["slot_sec"],
        "over_budget": len(narration) > budget["max_chars"] * OVER_BUDGET_RATIO,
    }


def load_script(project_dir: str, name: str = "") -> Dict[str, Any]:
    """The editable script of a recap project (``editable: False`` with a reason for other engines)."""
    state = _read_json(os.path.join(project_dir, STATE_FILE))
    if not isinstance(state, dict):
        raise ProjectError("This project has no readable state.json.", code="no_state")
    script = state.get("generated_script")
    if not isinstance(script, list) or not script:
        engine = state.get("engine_type") or "recap"
        reason = ("Only recap (Engine 1) projects have an editable narration script."
                  if engine != "recap" else "This project has no script yet - run it first.")
        return {"project": name, "editable": False, "reason": reason, "blocks": [], "summary": _script_summary([])}
    blocks = [_block_view(i, b) for i, b in enumerate(script) if isinstance(b, dict)]
    return {
        "project": name,
        "editable": True,
        "movie_name": state.get("movie_name") or name,
        "blocks": blocks,
        "summary": _script_summary(blocks),
        "has_saved_options": os.path.exists(os.path.join(project_dir, JOB_OPTIONS_FILE)),
        "script_edited_at": state.get("script_edited_at"),
        "story": parse_bible(state.get("story_context")),
        "rendered": os.path.exists(os.path.join(project_dir, "final_recap.mp4"))
        or os.path.exists(os.path.join(project_dir, "final_reels.mp4")),
    }


def _script_summary(blocks: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "total": len(blocks),
        "needs_review": sum(1 for b in blocks if b["needs_review"]),
        "edited": sum(1 for b in blocks if b["edited"]),
        "over_budget": sum(1 for b in blocks if b["over_budget"]),
    }


# ── script edit ────────────────────────────────────────────────────────────────
def clean_narration(text: Any) -> str:
    """User-typed narration: NFC, control characters removed, runs of whitespace collapsed."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = _CONTROL.sub("", text).replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _validated_edits(edits: Any, n_blocks: int) -> List[Dict[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise ProjectError("Send a non-empty list of edits.", code="invalid")
    if len(edits) > MAX_EDITS_PER_CALL:
        raise ProjectError(f"Too many edits in one call (max {MAX_EDITS_PER_CALL}).", code="invalid")
    seen = set()
    out = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise ProjectError("Every edit must be an object.", code="invalid")
        index = edit.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < n_blocks:
            raise ProjectError(f"Edit index {index!r} is outside the script (0..{n_blocks - 1}).", code="invalid")
        if index in seen:
            raise ProjectError(f"Line {index} is edited twice in one call.", code="invalid")
        seen.add(index)
        clean: Dict[str, Any] = {"index": index}
        if "narration" in edit:
            text = clean_narration(edit["narration"])
            if not text:
                raise ProjectError(f"Line {index + 1}: the narration cannot be empty.", code="invalid")
            if len(text) > MAX_NARRATION_CHARS:
                raise ProjectError(f"Line {index + 1}: the narration is too long (max {MAX_NARRATION_CHARS}).", code="invalid")
            clean["narration"] = text
        if "gender" in edit:
            gender = str(edit["gender"]).strip().lower()
            if gender not in ("male", "female"):
                raise ProjectError(f"Line {index + 1}: gender must be male or female.", code="invalid")
            clean["gender"] = gender
        if len(clean) == 1:
            raise ProjectError(f"Line {index + 1}: nothing to change (send narration and/or gender).", code="invalid")
        out.append(clean)
    return out


def _refresh_script_txt(project_dir: str, state: Dict[str, Any]) -> bool:
    """Rewrites final_recap_script.txt from the edited script (best effort, never fatal)."""
    try:
        from agents.seo_agent import SEOAgent
        from brain.memory import MovieState
        movie_state = MovieState(**state)
        SEOAgent().export_final_outputs(movie_state, output_dir=project_dir)
        return True
    except Exception as exc:
        log_suppressed("services.projects._refresh_script_txt", exc)
        return False


def save_script(project_dir: str, edits: Any) -> Dict[str, Any]:
    """
    Applies narration/gender edits to the project's script and un-completes the voice, merge and QA
    phases so the next run regenerates only what changed. Returns what happened.
    """
    state_path = os.path.join(project_dir, STATE_FILE)
    state = _read_json(state_path)
    script = state.get("generated_script") if isinstance(state, dict) else None
    if not isinstance(script, list) or not script:
        raise ProjectError("This project has no editable script.", code="no_script")

    changed: List[int] = []
    for edit in _validated_edits(edits, len(script)):
        block = script[edit["index"]]
        if not isinstance(block, dict):
            continue
        touched = False
        if "narration" in edit and edit["narration"] != block.get("narration"):
            block["narration"] = edit["narration"]
            touched = True
        if "gender" in edit and edit["gender"] != block.get("gender"):
            block["gender"] = edit["gender"]
            touched = True
        if touched:
            block["edited"] = True
            block.pop("needs_review", None)    # a human has looked at it now
            block.pop("issues", None)
            block.pop("qa_rewritten_for_length", None)
            changed.append(edit["index"])
        elif block.get("needs_review") and "narration" in edit:
            # "I checked it and it is fine": confirm without changing the text.
            block.pop("needs_review", None)
            block.pop("issues", None)
            block["edited"] = True

    result: Dict[str, Any] = {"changed": len(changed), "lines": changed, "invalidated": [], "script_txt_updated": False}
    if not changed:
        _write_json(state_path, state)   # persists review confirmations
        return result

    invalidated = [p for p in RERENDER_PHASES if p in (state.get("completed_phases") or [])]
    state["completed_phases"] = [p for p in (state.get("completed_phases") or []) if p not in RERENDER_PHASES]
    checkpoints = state.get("phase_checkpoints")
    if isinstance(checkpoints, dict):
        for phase in RERENDER_PHASES:
            checkpoints.pop(phase, None)
    statuses = state.get("phase_statuses")
    if isinstance(statuses, dict):
        for phase in RERENDER_PHASES:
            statuses.pop(phase, None)
    # The 9:16 canvas and the "clean" copy are derived from the old render.
    state["clean_video_path"] = None
    state["reels_video_path"] = None
    state["script_edited_at"] = _now()
    _write_json(state_path, state)

    ckpt_path = os.path.join(project_dir, CHECKPOINT_FILE)
    ckpt = _read_json(ckpt_path)
    if isinstance(ckpt, dict):
        ckpt["completed_phases"] = [p for p in (ckpt.get("completed_phases") or []) if p not in RERENDER_PHASES]
        if isinstance(ckpt.get("phase_checkpoints"), dict):
            for phase in RERENDER_PHASES:
                ckpt["phase_checkpoints"].pop(phase, None)
        if isinstance(ckpt.get("phase_statuses"), dict):
            for phase in RERENDER_PHASES:
                ckpt["phase_statuses"].pop(phase, None)
        _write_json(ckpt_path, ckpt, indent=2)

    result["invalidated"] = invalidated
    result["script_txt_updated"] = _refresh_script_txt(project_dir, state)
    return result


# ── find & replace across the script (rename a character everywhere) ───────────
MAX_FIND_CHARS = 80
MAX_PREVIEW_SAMPLES = 6
MAX_PREVIEW_LINES = 300


def _replace_pattern(find: str, match_case: bool, whole_word: bool) -> "re.Pattern":
    body = re.escape(find)
    if whole_word:   # \w does not cover Myanmar vowel signs, so "word" means a run of ASCII letters/digits
        body = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])"
    return re.compile(body, 0 if match_case else re.IGNORECASE)


def replace_in_script(project_dir: str, find: Any, replace: Any, *, match_case: bool = False,
                      whole_word: bool = False, preview: bool = False) -> Dict[str, Any]:
    """
    Replaces ``find`` by ``replace`` in every narration line (e.g. fixes a character's spelling everywhere).
    ``preview=True`` only reports what would change. A real run goes through ``save_script``, so the edited
    lines are marked, the voice/merge/QA phases are un-completed and only those lines are re-voiced; the same
    rename is applied to the stored story bible so a later script regeneration keeps the new spelling.
    """
    find = str(find or "")
    replace = str(replace if replace is not None else "")
    if not find.strip():
        raise ProjectError("Enter the text to find.", code="invalid")
    if len(find) > MAX_FIND_CHARS or len(replace) > MAX_NARRATION_CHARS:
        raise ProjectError(f"The text to find is limited to {MAX_FIND_CHARS} characters.", code="invalid")
    if find == replace:
        raise ProjectError("The replacement is the same as the original.", code="invalid")

    state_path = os.path.join(project_dir, STATE_FILE)
    state = _read_json(state_path)
    script = state.get("generated_script") if isinstance(state, dict) else None
    if not isinstance(script, list) or not script:
        raise ProjectError("This project has no editable script.", code="no_script")

    pattern = _replace_pattern(find, match_case, whole_word)
    edits, occurrences, samples = [], 0, []
    for index, block in enumerate(script):
        if not isinstance(block, dict):
            continue
        before = str(block.get("narration", ""))
        after, count = pattern.subn(lambda _m: replace, before)
        if not count:
            continue
        occurrences += count
        edits.append({"index": index, "narration": after})
        if len(samples) < MAX_PREVIEW_SAMPLES:
            samples.append({"index": index, "before": before, "after": after})

    result: Dict[str, Any] = {
        "lines": len(edits), "occurrences": occurrences, "preview": preview,
        "line_numbers": [e["index"] for e in edits][:MAX_PREVIEW_LINES], "samples": samples,
    }
    if preview or not edits:
        return result

    saved = save_script(project_dir, edits)
    result.update(changed=saved["changed"], invalidated=saved["invalidated"], script_txt_updated=saved["script_txt_updated"])

    story = _read_json(state_path)
    if isinstance(story, dict) and isinstance(story.get("story_context"), str):
        renamed = pattern.sub(lambda _m: replace, story["story_context"])
        if renamed != story["story_context"]:
            story["story_context"] = renamed       # the key is kept: a later script run reuses this edited bible
            _write_json(state_path, story)
    return result


# ── saved job options (for "re-render with the same settings") ────────────────
def save_job_options(project_dir: str, options: Dict[str, Any]) -> None:
    """Stores the option snapshot a job was started with (never contains API keys - none are options)."""
    try:
        os.makedirs(project_dir, exist_ok=True)
        _write_json(os.path.join(project_dir, JOB_OPTIONS_FILE), {"saved_at": _now(), "options": options})
    except Exception as exc:
        log_suppressed("services.projects.save_job_options", exc)


def load_job_options(project_dir: str) -> Optional[Dict[str, Any]]:
    data = _read_json(os.path.join(project_dir, JOB_OPTIONS_FILE))
    options = data.get("options") if isinstance(data, dict) else None
    return options if isinstance(options, dict) else None


# ── quality report ─────────────────────────────────────────────────────────────
def _clip_durations(project_dir: str, count: int) -> Dict[int, float]:
    """Duration in seconds of voiceover/scene_NNNN.mp3 per script index (missing / unreadable clips omitted)."""
    out: Dict[int, float] = {}
    vo_dir = os.path.join(project_dir, "voiceover")
    if not os.path.isdir(vo_dir):
        return out
    try:
        import soundfile as sf
    except Exception as exc:
        log_suppressed("services.projects._clip_durations", exc)
        return out
    for idx in range(count):
        path = os.path.join(vo_dir, f"scene_{idx + 1:04d}.mp3")
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            try:
                out[idx] = float(sf.info(path).duration)
            except Exception as exc:
                log_suppressed("services.projects._clip_durations", exc)
    return out


def _file_mb(path: str) -> Optional[float]:
    return round(os.path.getsize(path) / 1e6, 1) if os.path.exists(path) else None


def quality_report(project_dir: str, name: str = "") -> Dict[str, Any]:
    """What still needs a human look in this project, plus a 0-100 score and a verdict."""
    state = _read_json(os.path.join(project_dir, STATE_FILE))
    if not isinstance(state, dict):
        raise ProjectError("This project has no readable state.json.", code="no_state")
    engine = state.get("engine_type") or "recap"
    report: Dict[str, Any] = {
        "project": name,
        "generated_at": _now(),
        "engine_type": engine,
        "pipeline": {
            "status": state.get("pipeline_status") or state.get("current_phase") or "",
            "duration": state.get("total_duration_formatted") or "",
            "warnings": list(state.get("warnings") or [])[:20],
            "errors": list(state.get("errors") or [])[:20],
        },
        "video": {
            "final_recap_mb": _file_mb(os.path.join(project_dir, "final_recap.mp4")),
            "final_reels_mb": _file_mb(os.path.join(project_dir, "final_reels.mp4")),
        },
    }

    script = state.get("generated_script")
    if isinstance(script, list) and script:
        report.update(_recap_findings(project_dir, script, report["pipeline"]))
    else:
        records = _read_json(os.path.join(project_dir, "records_data.json"))
        if isinstance(records, list) and records:
            damaged = find_damaged_lines([r for r in records if isinstance(r, dict)])
            report["subtitles"] = {
                "lines": len(records),
                "needs_review": len(damaged),
                "review_lines": [
                    {"index": i, "original": str(records[i].get("original", ""))[:80],
                     "burmese": str(records[i].get("burmese", ""))[:80]} for i in damaged[:50]
                ],
            }
            lines, flagged = len(records), len(damaged)
            report["score"], report["verdict"] = _score(lines, flagged, 0, report["pipeline"])
        else:
            report["score"], report["verdict"] = None, "unknown"
    return report


def _recap_findings(project_dir: str, script: List[Dict[str, Any]], pipeline: Dict[str, Any]) -> Dict[str, Any]:
    views = [_block_view(i, b) for i, b in enumerate(script) if isinstance(b, dict)]
    issue_counts: Dict[str, int] = {}
    for v in views:
        for code in v["issues"]:
            issue_counts[code] = issue_counts.get(code, 0) + 1
    review = [v for v in views if v["needs_review"]]
    over = [v for v in views if v["over_budget"]]

    durations = _clip_durations(project_dir, len(views))
    overflow = []
    for idx, actual in durations.items():
        slot = views[idx]["slot_sec"]
        if actual > slot * STRETCH_LIMIT:
            overflow.append({"index": idx, "slot_sec": slot, "clip_sec": round(actual, 2)})
    missing = [i for i in range(len(views)) if i not in durations] if os.path.isdir(os.path.join(project_dir, "voiceover")) else []

    section = {
        "script": {
            "lines": len(views),
            "needs_review": len(review),
            "edited": sum(1 for v in views if v["edited"]),
            "over_budget": len(over),
            "issues": issue_counts,
            "review_lines": [
                {"index": v["index"], "scene_id": v["scene_id"], "issues": v["issues"],
                 "source_text": v["source_text"][:80], "narration": v["narration"][:80]}
                for v in review[:50]
            ],
        },
        "voice": {
            "clips": len(durations),
            "missing_clips": len(missing),
            "overflowing_clips": len(overflow),
            "overflow": overflow[:30],
        },
    }
    section["score"], section["verdict"] = _score(len(views), len(review) + len(over), len(overflow), pipeline)
    return section


def _score(lines: int, flagged: int, overflow: int, pipeline: Dict[str, Any]) -> tuple:
    """0-100 score: unreviewed lines weigh most, then clips that overflow their slot, then run warnings/errors."""
    if lines <= 0:
        return None, "unknown"
    penalty = min(60.0, 150.0 * flagged / lines) + min(25.0, 100.0 * overflow / lines)
    penalty += min(15.0, 5.0 * len(pipeline.get("warnings") or []))
    penalty += 20.0 if pipeline.get("errors") else 0.0
    score = max(0, round(100 - penalty))
    if pipeline.get("errors") or flagged / lines >= 0.15:
        verdict = "attention"
    elif flagged or overflow:
        verdict = "review"
    else:
        verdict = "ready"
    return score, verdict


def write_quality_report(project_dir: str, name: str = "") -> Optional[Dict[str, Any]]:
    """Builds the report and saves it as ``quality_report.json`` (None if the project cannot be read)."""
    try:
        report = quality_report(project_dir, name)
        _write_json(os.path.join(project_dir, REPORT_FILE), report, indent=2)
        return report
    except Exception as exc:
        log_suppressed("services.projects.write_quality_report", exc)
        return None


# ── listing / storage ──────────────────────────────────────────────────────────
def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError as exc:
                log_suppressed("services.projects._dir_size", exc)
    return total


def storage_summary(roots: Dict[str, str]) -> Dict[str, Any]:
    """``{label: path}`` -> sizes in MB per folder plus the total; missing folders count as 0."""
    out: Dict[str, Any] = {}
    total = 0
    for label, path in roots.items():
        size = _dir_size(path) if os.path.isdir(path) else 0
        out[label] = round(size / 1e6, 1)
        total += size
    out["total_mb"] = round(total / 1e6, 1)
    return out


def list_projects(base_dir: str) -> List[Dict[str, Any]]:
    """One row per project folder that has a state.json, newest first."""
    rows: List[Dict[str, Any]] = []
    if not os.path.isdir(base_dir):
        return rows
    for entry in os.scandir(base_dir):
        state_path = os.path.join(entry.path, STATE_FILE)
        if not entry.is_dir() or not os.path.exists(state_path):
            continue
        state = _read_json(state_path)
        if not isinstance(state, dict):
            continue
        script = state.get("generated_script")
        n_review = sum(1 for b in script if isinstance(b, dict) and b.get("needs_review")) if isinstance(script, list) else 0
        report = _read_json(os.path.join(entry.path, REPORT_FILE))
        rows.append({
            "name": entry.name,
            "movie_name": state.get("movie_name") or entry.name,
            "engine_type": state.get("engine_type") or "recap",
            "status": state.get("pipeline_status") or state.get("current_phase") or "",
            "progress": state.get("progress"),
            "duration": state.get("total_duration_formatted") or "",
            "updated": datetime.datetime.fromtimestamp(os.path.getmtime(state_path)).isoformat(timespec="seconds"),
            "mtime": os.path.getmtime(state_path),
            "lines": len(script) if isinstance(script, list) else state.get("total_records"),
            "needs_review": n_review,
            "score": report.get("score") if isinstance(report, dict) else None,
            "verdict": report.get("verdict") if isinstance(report, dict) else None,
            "has_video": any(os.path.exists(os.path.join(entry.path, f))
                             for f in ("final_recap.mp4", "final_reels.mp4", "01_hardsub_16_9.mp4")),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    for row in rows:
        row.pop("mtime", None)
    return rows
