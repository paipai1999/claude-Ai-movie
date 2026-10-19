from brain.diagnostics import log_suppressed
import json
import os
import re
from brain.memory import MovieState
from brain.gemini_client import call_gemini
from agents.story_memory import build_story_context, context_key as story_context_key
from agents.translation_flow import TranslationFlow
from brain.prompts import (
    FULL_MOVIE_TRANSLATION_SYSTEM_PROMPT,
    MOVIE_RECAP_STORYTELLER_SYSTEM_PROMPT,
    AUDIO_DRAMA_BURMESE_RECAP_PROMPT,
    HARDSUB_BURMESE_TRANSLATION_SYSTEM_PROMPT,
    WUXIA_BURMESE_TRANSLATION_SYSTEM_PROMPT,
)
from brain import config as cfg
from brain.burmese_utils import (
    replace_numbers_with_burmese,
    transliterate_english_acronyms,
    sanitize_burmese_narration,
)


# Recap-style directives chosen in the wizard. "standard" / "natural" add nothing, so default prompts are unchanged.
_LENGTH_DIRECTIVES = {
    "compact": "LENGTH - COMPACT RECAP: tell only the plot-critical events. Skip minor beats and filler, and fold "
               "what happens in a few seconds into one short line. Use at most about 70% of each `max_chars`.",
    "detailed": "LENGTH - DETAILED: include the visible actions, emotions and small story beats between the main "
                "events. Use most of each line's `max_chars` for rich, concrete narration.",
    "scene": "LENGTH - SCENE BY SCENE: narrate every scene and beat in order, using the keyframe images together "
             "with the dialogue. Leave nothing out and fill each line's `max_chars`.",
}
_TONE_DIRECTIVES = {
    "brisk": "TONE - BRISK: energetic and fast-paced, punchy sentences with constant momentum.",
    "calm": "TONE - CALM: a calm, unhurried, reflective storyteller with gentle phrasing.",
    "dramatic": "TONE - DRAMATIC: suspenseful and theatrical; build tension and use short striking sentences before reveals.",
}


class WriterAgent:
    def __init__(
        self,
        language: str = "burmese",
        max_blocks: int | None = None,
        script_engine: str = "recap",
    ):
        self.language = language
        self.max_blocks = max_blocks or (int(os.getenv("MAX_BLOCKS")) if os.getenv("MAX_BLOCKS") else None)
        self.script_engine = (script_engine or os.getenv("SCRIPT_ENGINE") or "recap").lower()

    def get_system_prompt_for_style(self, style: str) -> str:
        s = str(style or "").lower().strip()
        if s in ["wuxia", "cultivation", "historical", "costume"]:
            return WUXIA_BURMESE_TRANSLATION_SYSTEM_PROMPT
        elif s in ["drama_novel", "audio_drama", "novel", "short_drama", "chinese_drama"]:
            return AUDIO_DRAMA_BURMESE_RECAP_PROMPT
        elif s in ["persona", "character", "kinship", "cinematic"]:
            return HARDSUB_BURMESE_TRANSLATION_SYSTEM_PROMPT
        elif s == "recap":
            return MOVIE_RECAP_STORYTELLER_SYSTEM_PROMPT
        else:
            return FULL_MOVIE_TRANSLATION_SYSTEM_PROMPT

    def _get_system_prompt(self, state: MovieState) -> str:
        active_style = getattr(state, "translation_style", None) or self.script_engine or "recap"
        return self.get_system_prompt_for_style(active_style)

    # ─────────────────────────────────────────────────────
    # PUBLIC: generate_script (Full Movie Dialogue Translation / Movie Recap Storyteller)
    # ─────────────────────────────────────────────────────
    def generate_script(self, state: MovieState, movie_path: str = "") -> MovieState:
        """
        Dual Narration Engine:
        1. 'recap' (Default): True Myanmar Movie Recap Storyteller persona with conversational
           sentence endings, narrative bridges, and dramatic timing.
        2. 'translate': 1:1 Complete Spoken Dialogue Translation & Dubbing.
        """
        engine_label = "RECAP STORYTELLER" if self.script_engine == "recap" else "1:1 TRANSLATE DUBBING"
        print(f"[*] WriterAgent: Generating {engine_label} SCRIPT (Lang: {self.language}, Engine: {self.script_engine.upper()})...")

        if not state.transcript:
            print("[!] WriterAgent: No transcript found. Skipping dialogue translation.")
            return state

        config_data = cfg.load_config()
        gemini_cfg = config_data.get("gemini", {})
        gemini_key = gemini_cfg.get("api_keys") or os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
        models_dict = gemini_cfg.get("models", {})
        model_workhorse = models_dict.get("workhorse", "gemini-3.5-flash-lite")

        merged_segments = self._smooth_transcript(state)
        if not merged_segments:
            return state

        raw_segments = self._build_raw_segments(merged_segments, state, movie_path)
        if not raw_segments:
            print("[!] WriterAgent: No valid dialogue text found after smoothing.")
            return state

        total_count = len(raw_segments)
        if self.max_blocks and self.max_blocks > 0 and total_count > self.max_blocks:
            raw_segments = raw_segments[:self.max_blocks]
            total_count = len(raw_segments)
            print(f"[*] WriterAgent: Limited to MAX_BLOCKS={self.max_blocks} dialogue lines.")

        print(f"[*] WriterAgent: Found {total_count} sentence-level dialogue segments to translate (100% full coverage).")

        if not gemini_key:
            # Without a key every line used to fall back to its untranslated source text and the job "succeeded"
            # with a film narrated in the wrong language. Fail early, with the fix.
            raise RuntimeError(
                "No Gemini API key is configured, so the dialogue cannot be translated. Add a key in the "
                "dashboard settings or set GEMINI_API_KEYS. / Gemini API key မရှိသေးပါ - dashboard settings "
                "ထဲမှာ ထည့်ပါ သို့မဟုတ် GEMINI_API_KEYS environment variable ကို သတ်မှတ်ပါ။"
            )

        # 2. Story bible: names, genders and terms stay consistent across all batches (best effort, cached)
        self._story_context(state, raw_segments, gemini_key, model_workhorse)

        all_translated = self._translate_batches(raw_segments, state, movie_path, gemini_key, model_workhorse)

        # 3. Action Narration Bridge: Bridge silent or long action gaps (> 18s)
        all_translated = self._bridge_action_narration(all_translated, state, gemini_key, model_workhorse)

        state.generated_script = all_translated
        flagged = sum(1 for b in all_translated if b.get("needs_review"))
        print(f"[OK] WriterAgent: Full Movie Dialogue Translation complete! Total {len(all_translated)} dialogue lines dubbed"
              + (f" ({flagged} flagged for review - see the Script Editor)." if flagged else "."))
        return state

    # ─────────────────────────────────────────────────────
    # Step 1 – clean, de-duplicate and merge Whisper fragments into sentence-level segments
    # ─────────────────────────────────────────────────────
    # Minimum spoken duration (seconds) used by the smoothing pass and the TTS budget.
    MIN_SEGMENT_DUR = 2.2

    def _smooth_transcript(self, state: MovieState) -> list:
        """Cleans, de-duplicates and merges micro-fragments. Returns [] when nothing usable remains."""
        # 1. Extract, clean, and smooth Whisper dialogue segments
        # Ensures 100% full coverage while eliminating micro-fragments, repetitive stutters, and unnatural breaks
        import re

        def _clean_transcript_line(t: str) -> str:
            t = re.sub(r'\[.*?\]|\(.*?\)', '', t)
            t = re.sub(r'\.{2,}', '.', t)
            t = re.sub(r'-{2,}', ' ', t)
            t = re.sub(r'\s+', ' ', t).strip()
            return t

        whisper_hallucinations = {
            "thank you for watching", "thanks for watching", "please subscribe",
            "subscribe to my channel", "bye bye", "see you next time", "subtitles by",
            "subtitles", "thank you.", "bye.", "you"
        }

        initial_segments = []
        for i, seg in enumerate(state.transcript):
            if isinstance(seg, dict):
                t_start = float(seg.get("start", 0.0) or 0.0)
                t_end = float(seg.get("end", t_start + 2.0) or (t_start + 2.0))
                text = str(seg.get("text", "")).strip()
            else:
                t_start = float(getattr(seg, "start", 0.0) or 0.0)
                t_end = float(getattr(seg, "end", t_start + 2.0) or (t_start + 2.0))
                text = str(getattr(seg, "text", "")).strip()

            cleaned = _clean_transcript_line(text)
            if not cleaned or len(cleaned) <= 1 or t_end <= t_start:
                continue
            if cleaned.lower().rstrip('.!') in whisper_hallucinations:
                continue

            seg_dur = t_end - t_start
            # Split only very long monologue segments (> 7.0s) at full sentence boundaries
            if seg_dur > 7.0 and re.search(r'(?<!\bMr)(?<!\bDr)(?<!\bMs)(?<!\bMrs)(?<!\bSt)(?<!\be\.g)(?<!\bi\.e)[.!?]\s+[A-Z]', cleaned):
                parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+(?=[A-Z])', cleaned) if p.strip()]
                if len(parts) > 1:
                    total_chars = sum(len(p) for p in parts)
                    c_start = t_start
                    for p_idx, p in enumerate(parts):
                        prop = len(p) / total_chars if total_chars > 0 else (1.0 / len(parts))
                        c_dur = seg_dur * prop
                        c_end = t_end if (p_idx == len(parts) - 1) else (c_start + c_dur)
                        initial_segments.append({
                            "start_sec": round(c_start, 2),
                            "end_sec": round(c_end, 2),
                            "text": p
                        })
                        c_start = c_end
                    continue

            initial_segments.append({
                "start_sec": round(t_start, 2),
                "end_sec": round(t_end, 2),
                "text": cleaned
            })

        if not initial_segments:
            print("[!] WriterAgent: No valid dialogue text found in transcript.")
            return []

        # Pass 1: Deduplicate Whisper stuttering tokens (e.g. repeated word fragments)
        deduped = []
        for seg in initial_segments:
            if not deduped:
                deduped.append(seg)
                continue
            prev = deduped[-1]
            prev_words = [w.lower().strip('.,!?\'"') for w in prev["text"].split() if w.strip('.,!?\'"')]
            curr_words = [w.lower().strip('.,!?\'"') for w in seg["text"].split() if w.strip('.,!?\'"')]
            if not curr_words:
                continue
            # If the entire segment is just 1-2 words that were already spoken at the end of prev
            if len(curr_words) <= 2 and all(w in prev_words[-3:] for w in curr_words):
                prev["end_sec"] = max(prev["end_sec"], seg["end_sec"])
                continue
            if seg["text"].lower().strip('.,!?') == prev["text"].lower().strip('.,!?'):
                prev["end_sec"] = max(prev["end_sec"], seg["end_sec"])
                continue
            deduped.append(seg)

        # Pass 2: Merge micro-fragments (< 2.2s or < 4 words) into cohesive complete thoughts
        MIN_DUR = self.MIN_SEGMENT_DUR
        MIN_WORDS = 4
        MAX_GAP = 1.8

        merged_segments = []
        s_i = 0
        n_deduped = len(deduped)
        while s_i < n_deduped:
            curr = deduped[s_i]
            words = curr["text"].split()
            dur = curr["end_sec"] - curr["start_sec"]
            is_frag = (dur < MIN_DUR) or (len(words) < MIN_WORDS)

            # Try forward merge if next segment is close
            if is_frag and (s_i + 1 < n_deduped):
                nxt = deduped[s_i + 1]
                gap_forward = round(nxt["start_sec"] - curr["end_sec"], 2)
                if gap_forward <= MAX_GAP:
                    joiner = " " if curr["text"].endswith(('.', '!', '?', ',')) else ", "
                    nxt["text"] = (curr["text"].rstrip('.!?') + joiner + nxt["text"]).strip()
                    nxt["start_sec"] = curr["start_sec"]
                    s_i += 1
                    continue

            # If cannot forward-merge, try backward merge into previous segment
            if is_frag and merged_segments:
                prev = merged_segments[-1]
                gap_back = round(curr["start_sec"] - prev["end_sec"], 2)
                if gap_back <= MAX_GAP:
                    joiner = " " if prev["text"].endswith(('.', '!', '?', ',')) else ", "
                    prev["text"] = (prev["text"].rstrip('.!?') + joiner + curr["text"]).strip()
                    prev["end_sec"] = curr["end_sec"]
                    s_i += 1
                    continue

            merged_segments.append(curr)
            s_i += 1

        return merged_segments

    # ─────────────────────────────────────────────────────
    # Step 2 – attach duration budget + acoustic gender hint to every segment
    # ─────────────────────────────────────────────────────
    @staticmethod
    def _resolve_video_path(state: MovieState, movie_path: str = "") -> str:
        """Existing video file used for keyframes (empty string when there is none)."""
        vid_path = movie_path or getattr(state, "movie_path", "")
        if vid_path and not os.path.exists(vid_path):
            vid_path = ""
        return vid_path

    @staticmethod
    def _extract_keyframe_b64(vpath: str, t_sec: float, max_dim: int = 480):
        """Returns a small base64 JPEG of the frame at `t_sec` (None when unavailable)."""
        if not vpath or not os.path.exists(vpath):
            return None
        try:
            import cv2
            import base64
            cap = cv2.VideoCapture(vpath)
            if not cap.isOpened():
                return None
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t_sec * fps))
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                return None
            h, w = frame.shape[:2]
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if ret:
                return base64.b64encode(buffer).decode('utf-8')
        except Exception as _exc:
            log_suppressed("agents.writer_agent._extract_keyframe_b64", _exc)
        return None

    def _build_raw_segments(self, merged_segments: list, state: MovieState, movie_path: str = "") -> list:
        """Builds the id/duration/max_chars/acoustic_gender records sent to the LLM."""
        MIN_DUR = self.MIN_SEGMENT_DUR
        audio_source = getattr(state, "vocals_path", "") or getattr(state, "audio_path", "")
        if audio_source and not os.path.exists(audio_source):
            audio_source = ""
        raw_segments = []
        for idx, seg in enumerate(merged_segments):
            dur = round(max(MIN_DUR, seg["end_sec"] - seg["start_sec"]), 2)
            max_chars = max(24, int(dur * 11.0))
            acoustic_gender = "unknown"
            if audio_source and os.path.exists(audio_source):
                try:
                    from agents.audio_agent import AudioAgent
                    acoustic_gender = AudioAgent.detect_pitch_gender(audio_source, seg["start_sec"], seg["end_sec"])
                except Exception:
                    acoustic_gender = "unknown"

            raw_segments.append({
                "id": idx + 1,
                "start_sec": seg["start_sec"],
                "end_sec": seg["end_sec"],
                "duration_sec": dur,
                "max_chars": max_chars,
                "acoustic_gender": acoustic_gender,
                "text": seg["text"]
            })

        if not raw_segments:
            print("[!] WriterAgent: No valid dialogue text found after smoothing.")
            return []

        return raw_segments

    # ─────────────────────────────────────────────────────
    # Step 3 – batched translation with per-line fallback
    # ─────────────────────────────────────────────────────
    def _style_directive(self, state: MovieState) -> str:
        """Length / tone instructions for the recap storyteller ("" for other modes and for the default choices)."""
        if self.script_engine != "recap":
            return ""
        lines = [
            _LENGTH_DIRECTIVES.get(str(getattr(state, "story_length", "") or "").lower()),
            _TONE_DIRECTIVES.get(str(getattr(state, "narrative_tone", "") or "").lower()),
        ]
        lines = [ln for ln in lines if ln]
        return ("NARRATION STYLE (follow strictly):\n" + "\n".join(lines) + "\n\n") if lines else ""

    @staticmethod
    def _context_block(state: MovieState, context: dict | None) -> str:
        """Story bible + the neighbouring lines (already translated before, still to come after) for a batch prompt."""
        parts = []
        story = (getattr(state, "story_context", None) or "").strip()
        if story:
            parts.append(story + "\n\n")
        context = context or {}
        if context.get("previous"):
            parts.append(
                "PREVIOUS LINES (already translated - context only, DO NOT output them; continue their voice, "
                "tense and name spellings):\n" + json.dumps(context["previous"], ensure_ascii=False) + "\n\n"
            )
        if context.get("next"):
            parts.append(
                "UPCOMING SOURCE LINES (context only - DO NOT translate or output them; read them to finish a "
                "sentence that is cut at a line boundary):\n" + json.dumps(context["next"], ensure_ascii=False) + "\n\n"
            )
        if context.get("previous") or context.get("next"):
            parts.append("Translate ONLY the dialogue lines in the main list below.\n\n")
        return "".join(parts)

    def _build_batch_prompt(self, batch: list, state: MovieState, context: dict | None = None, feedback: str = "") -> tuple:
        """
        Returns (system_prompt, user_prompt) for one batch according to the active translation style.
        ``context``: ``{"previous": [...], "next": [...]}`` neighbouring lines; ``feedback``: corrections for a retry.
        """
        active_style = getattr(state, "translation_style", None) or self.script_engine or "recap"
        active_style = str(active_style).lower().strip()
        hint_str = self._context_block(state, context) + self._style_directive(state)
        custom_hint = getattr(state, "context_hint", None)
        if custom_hint and str(custom_hint).strip():
            hint_str += f"USER STORY & CHARACTER GUIDANCE:\n{str(custom_hint).strip()}\n\n"

        if active_style in ["wuxia", "cultivation", "historical", "costume"]:
            sys_prompt = WUXIA_BURMESE_TRANSLATION_SYSTEM_PROMPT
            batch_prompt = (
                f"Target Language: {self.language.upper()}\n"
                f"Movie Title: {state.movie_name}\n"
                f"{hint_str}"
                f"Translate each dialogue line below with 100% faithful precision into dramatic Wuxia/Cultivation colloquial {self.language.title()}:\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n\n"
                f"Output a JSON array where each object has: id, narration, start_sec, end_sec, emotion, character, gender (\"male\" or \"female\")."
            )
        elif active_style in ["persona", "character", "kinship", "cinematic"]:
            sys_prompt = HARDSUB_BURMESE_TRANSLATION_SYSTEM_PROMPT
            batch_prompt = (
                f"Target Language: {self.language.upper()}\n"
                f"Movie Title: {state.movie_name}\n"
                f"{hint_str}"
                f"Translate each dialogue line below with 100% faithful precision into colloquial {self.language.title()} enforcing context-appropriate Male (ကျနော်/ခင်ဗျာ), Female (ကျွန်မ/ရှင်), and Child (သား/သမီး) personas (Strict rule: Never use 'ရှင်/ရှင့်' for parent-to-child or inner thoughts):\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n\n"
                f"Output a JSON array where each object has: id, narration, start_sec, end_sec, emotion, character, gender (\"male\" or \"female\")."
            )
        elif active_style in ["drama_novel", "audio_drama", "novel", "short_drama", "chinese_drama"]:
            sys_prompt = AUDIO_DRAMA_BURMESE_RECAP_PROMPT
            batch_prompt = (
                f"Target Language: {self.language.upper()}\n"
                f"Movie Title: {state.movie_name}\n"
                f"{hint_str}"
                f"Transform each dialogue line below into thrilling, addictive Chinese Drama Audio Novel (တရုတ်ဒရမ်မာ အသံထွက်ဝတ္ထုဟန်) narration in natural colloquial {self.language.title()}.\n"
                f"CRITICAL REQUIREMENTS:\n"
                f"1. THIRD-PERSON DRAMATIC RECOUNTING & TONE TAGS: Describe the characters' vocal tone, emotion, and dramatic actions ('တင်းမာတဲ့ အသံနဲ့ ... လို့ ပြန်ပြောလိုက်တယ်', 'ဒေါသတကြီးနဲ့ ... လို့ အော်ဟစ်လိုက်တဲ့အခါ', 'ဝမ်းနည်းဒေါသထွက်နေတဲ့ အသံနဲ့ ... လို့ တုံ့ပြန်လိုက်တယ်', 'ရှက်ရွံ့စွာနဲ့ ရယ်မောလိုက်ရင်း ... လို့ ပြတ်သားစွာ ပြောချလိုက်ပါတယ်').\n"
                f"2. NOVEL-STYLE QUOTED DIALOGUE: Blend third-person narration seamlessly with direct dialogue quotes ('...' လို့ ပြန်ပြောလိုက်ပါတယ်, '...' လို့ အော်ဟစ်လိုက်တော့တယ်).\n"
                f"3. STRICT CHARACTER BUDGET & DURATION MATCH: Each line's `narration` MUST STRICTLY STAY UNDER its given `max_chars` limit (Burmese TTS rate is ~10-11 chars/sec) so audio finishes cleanly within `duration_sec` seconds!\n"
                f"4. CHINESE DRAMA TROPES & ROLES: Use proper roles ('ဥက္ကဋ္ဌကြီး', 'စီအီးအို', 'သခင်လေး', 'မမလေး', 'အတွင်းရေးမှူး') and transliterate Chinese names phonetically. Never output Chinese Hanzi characters or raw English words.\n"
                f"5. NATURAL COLLOQUIAL SPOKEN PARTICLES: Use spoken particles ('...လိုက်ပါတယ်', '...ခဲ့တာပေါ့', '...သွားခဲ့ရတယ်', '...ဖြစ်နေတာပါ'). ❌ STRICTLY FORBIDDEN: Formal bookish markers (ပါသည်, သည်, မည်, ၏, ၍, ၌).\n\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n\n"
                f"Output a JSON array where each object has: id, narration, start_sec, end_sec, emotion, character, gender (\"male\" or \"female\")."
            )
        elif active_style == "recap":
            sys_prompt = MOVIE_RECAP_STORYTELLER_SYSTEM_PROMPT
            batch_prompt = (
                f"Target Language: {self.language.upper()}\n"
                f"Movie Title: {state.movie_name}\n"
                f"{hint_str}"
                f"Write suspenseful, captivating MOVIE RECAP STORYTELLER narration for each scene below in natural colloquial {self.language.title()}.\n"
                f"CRITICAL RECAP REQUIREMENTS:\n"
                f"1. TRUE RECAP STORYTELLER STYLE: Speak directly as an engaging Myanmar YouTube movie recap narrator ('ဒီဇာတ်လမ်းမှာတော့...', '...ခဲ့တာပေါ့ဗျာ'). DO NOT do dry literal 1:1 sentence dubbing.\n"
                f"2. CONVERSATIONAL RECAP ENDINGS: Every line MUST end with natural storytelling particles: '...ခဲ့တာပေါ့ဗျာ', '...နေခဲ့ပါတယ်', '...လိုက်ရတာပါ', '...သွားခဲ့ရတယ်', '...ဖြစ်နေတာပါ', '...ကြတာပေါ့', '...ရတော့တာပါ'. ❌ FORBIDDEN: Blunt chopped endings like '...တယ်', '...တာ', '...ဘူး'.\n"
                f"3. DYNAMIC NARRATIVE TRANSITIONS: Bridge scenes naturally using recap connectors ('ဇာတ်လမ်းအစမှာတော့...', 'အဲဒီအချိန်မှာပဲ...', 'မထင်မှတ်ထားဘဲ...', 'ဒီလိုနဲ့...', 'ကြည့်လိုက်တဲ့အခါမှာတော့...').\n"
                f"4. STRICT CHARACTER BUDGET & DURATION MATCH: Each line's `narration` MUST STRICTLY STAY UNDER its given `max_chars` limit (Burmese TTS rate is ~10 chars/sec) so audio finishes cleanly within `duration_sec` seconds! Keep phrasing punchy and exciting.\n"
                f"5. CLEAN BURMESE TEXT: Transliterate names and English loanwords phonetically. Never output foreign characters.\n\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n\n"
                f"Output a JSON array where each object has: id, narration, start_sec, end_sec, emotion, character, gender (\"male\" or \"female\")."
            )
        else:
            sys_prompt = FULL_MOVIE_TRANSLATION_SYSTEM_PROMPT
            batch_prompt = (
                f"Target Language: {self.language.upper()}\n"
                f"Movie Title: {state.movie_name}\n"
                f"{hint_str}"
                f"Translate EVERY SINGLE movie dialogue sentence below into natural colloquial {self.language.title()} for professional dubbing.\n"
                f"CRITICAL REQUIREMENTS:\n"
                f"1. STRICT 1:1 TRANSLATION: Translate every single item completely. DO NOT summarize, merge, or drop any sentence.\n"
                f"2. Translate all character names, places, events, and plot points accurately without leaving anything out.\n"
                f"3. STRICT CHARACTER BUDGET & DURATION MATCH: Each translation's `narration` MUST STRICTLY STAY UNDER its given `max_chars` limit (Burmese TTS rate is ~11 chars/sec). Keep sentences punchy, concise, and direct so the spoken narration finishes precisely within `duration_sec` seconds! NEVER write long verbose sentences that exceed `max_chars`!\n"
                f"4. MULTIMODAL SPEAKER DIARIZATION & GENDER ACCURACY: Observe the visual frame context and 'acoustic_gender' hint ('male', 'female', or 'unknown') for each dialogue. Determine the speaker's true 'gender' ('male' or 'female'), 'character' name/role, and 'emotion' ('normal', 'excited', 'angry', 'sad', 'scared', 'intense'). If 'acoustic_gender' is 'female' or the visual frame shows a female speaking, mark gender as 'female'!\n"
                f"5. NATURAL CINEMATIC FLOW: Avoid repetitive sentence endings (do NOT repeat identical words like 'ပေါ့', 'ပါ', 'တယ်' in consecutive lines). Write natural storytelling movie recap dialogue.\n\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n\n"
                f"Output a JSON array where each object has: id, narration, start_sec, end_sec, emotion, character, gender (\"male\" or \"female\")."
            )

        if feedback:
            batch_prompt += (
                "\n\nCORRECTIONS REQUIRED - your previous answer for these lines was rejected:\n"
                f"{feedback}\n"
                "Return the complete, corrected objects for exactly the lines in the list above."
            )
        return sys_prompt, batch_prompt

    def _is_burmese(self) -> bool:
        return str(getattr(self, "language", "burmese")).lower() in ("burmese", "mm", "myanmar")

    @staticmethod
    def _check_cancelled() -> None:
        """Stop pressed in the dashboard: abort between Gemini calls instead of finishing the whole film."""
        if os.environ.get("CURRENT_JOB_CANCELLED") == "1":
            raise InterruptedError("Script writing cancelled by user.")

    def _clean_callbacks(self):
        """(pre_clean, final_clean) for the translation flow: Burmese digit/acronym/script clean-up."""
        if not self._is_burmese():
            return (lambda t: t), (lambda t: t)

        def pre_clean(text: str) -> str:
            try:
                return transliterate_english_acronyms(replace_numbers_with_burmese(text))
            except Exception as exc:
                log_suppressed("agents.writer_agent.pre_clean", exc)
                return text

        def final_clean(text: str) -> str:
            try:
                return sanitize_burmese_narration(text)
            except Exception as exc:
                log_suppressed("agents.writer_agent.final_clean", exc)
                return text

        return pre_clean, final_clean

    def _story_context(self, state: MovieState, raw_segments: list, gemini_key, model: str) -> str:
        """The story bible for this film (one extra Gemini call, cached on the state); "" when unavailable."""
        lines = [s["text"] for s in raw_segments]
        key = story_context_key(lines, self.language, state.movie_name)
        cached = getattr(state, "story_context", None)
        if cached and getattr(state, "story_context_key", None) == key:
            return cached
        block = build_story_context(
            lines,
            target_language=self.language,
            movie_name=state.movie_name,
            api_key=gemini_key,
            model=model,
            call=call_gemini,
            hint=getattr(state, "context_hint", None) or "",
        )
        state.story_context = block or None
        state.story_context_key = key if block else None
        return block

    def _translate_batches(self, raw_segments: list, state: MovieState, movie_path: str,
                           gemini_key, model_workhorse: str) -> list:
        """
        Translates every segment through ``TranslationFlow`` (batches of 20 with neighbouring-line context,
        validated answers, corrective retries, batch splitting, per-line fallback) and returns the script blocks.
        Lines that could not be translated reliably keep their source text and carry ``needs_review``.
        """
        vid_path = self._resolve_video_path(state, movie_path)
        frames: dict = {}

        def frame_at(t_sec: float):
            key = round(float(t_sec), 1)
            if key not in frames:
                frames[key] = self._extract_keyframe_b64(vid_path, t_sec) if vid_path and os.path.exists(vid_path) else None
            return frames[key]

        def images_for(lines: list) -> list:
            """1-2 representative keyframes per request for visual diarization & context."""
            picks = [len(lines) // 2] if len(lines) <= 5 else [0, len(lines) // 2]
            out = []
            for i in picks:
                shot = frame_at((lines[i]["start_sec"] + lines[i]["end_sec"]) / 2.0)
                if shot:
                    out.append(shot)
            return out

        def ask_batch(lines: list, context: dict, feedback: str):
            if not gemini_key:
                return None
            sys_prompt, prompt = self._build_batch_prompt(lines, state, context, feedback)
            raw, _model = call_gemini(
                sys_prompt,
                prompt,
                gemini_key,
                model_workhorse,
                temperature=0.2 if feedback else 0.3,
                # Burmese is token-hungry: a fixed 4096 cut 20-line answers off mid-array.
                max_tokens=min(16384, 3072 + 450 * len(lines)),
                response_mime_type="application/json",
                images=images_for(lines) or None,
            )
            return self._parse_script(raw)

        def ask_line(seg: dict, context: dict):
            if not gemini_key:
                return None
            lang = self.language.title()
            if self.script_engine == "recap":
                fb_sys = f"You are a master movie recap storyteller into natural colloquial {lang} with lively storytelling endings."
                task = f"Write natural storyteller movie recap narration in {lang} for this scene"
            else:
                fb_sys = f"You are a professional movie dubbing translator into natural colloquial {lang}."
                task = f"Translate this dialogue into natural colloquial {lang}"
            fb_prompt = (
                self._context_block(state, context)
                + self._style_directive(state)
                + f"{task} (at most about {seg.get('max_chars', 60)} characters):\n'{seg['text']}'\n"
                "Return ONLY the narration as plain text, without quotes or meta explanation."
            )
            line_res, _model = call_gemini(
                fb_sys, fb_prompt, gemini_key, model_workhorse,
                temperature=0.3, max_tokens=512, response_mime_type="text/plain",
            )
            clean_line = line_res.strip().strip('"').strip("'").strip()
            if clean_line and not any(bad in clean_line.lower() for bad in ["json", "```", "here is", "here's"]):
                return clean_line
            return None

        pre_clean, final_clean = self._clean_callbacks()
        flow = TranslationFlow(
            ask_batch,
            ask_line,
            pre_clean=pre_clean,
            final_clean=final_clean,
            burmese_target=self._is_burmese(),
            default_gender=lambda seg: seg.get("acoustic_gender") if seg.get("acoustic_gender") in ("male", "female") else "male",
            log=lambda msg: print(msg.replace("[*] ", "[*] WriterAgent: ", 1) if msg.startswith("[*] ") else msg),
            check_cancel=self._check_cancelled,
        )
        results = flow.run(raw_segments)

        by_id = {s["id"]: s for s in raw_segments}
        all_translated = []
        for res in results:
            seg = by_id[res.seg_id]
            # Record character profile in state for downstream multi-voice & SEO consistency
            if res.character and res.character not in state.speaker_profiles:
                state.speaker_profiles[res.character] = {"gender": res.gender, "emotions": [res.emotion]}
            elif res.character and res.emotion not in state.speaker_profiles[res.character].get("emotions", []):
                state.speaker_profiles[res.character]["emotions"].append(res.emotion)

            block = {
                "scene_id": str(res.seg_id),
                "narration": res.narration,
                "start_sec": seg["start_sec"],
                "end_sec": seg["end_sec"],
                "gender": res.gender,
                "character": res.character,
                "emotion": res.emotion,
                "visual_cue": f"Dialogue ({seg['start_sec']}s - {seg['end_sec']}s): {seg['text'][:40]}...",
                "source_text": seg["text"],
            }
            if res.needs_review:
                block["needs_review"] = True
                block["issues"] = list(res.issues)
            all_translated.append(block)

        s = flow.stats
        flagged = s["flagged"]
        print(
            f"[*] WriterAgent: Translation finished - {len(all_translated)} lines, "
            f"{s['retries']} corrective retries, {s['splits']} batch splits, {s['line_fallbacks']} single-line fallbacks"
            + (f", {flagged} line(s) flagged for review." if flagged else ", no lines flagged.")
        )
        return all_translated

    def _bridge_action_narration(self, blocks: list, state: MovieState, gemini_key: str, model: str) -> list:
        """
        Bridges silent or long action gaps (> 18s) with engaging movie recap narration in the target language.
        Turns quiet combat, chase, or suspense sequences into a lively, continuous story recap.
        """
        if not blocks or not gemini_key:
            return blocks

        config_data = cfg.load_config()
        action_cfg = config_data.get("action_narration", {})
        if not action_cfg.get("enabled", True):
            return blocks

        min_gap = float(action_cfg.get("min_gap_sec", 18.0))
        blocks = sorted(blocks, key=lambda x: float(x.get("start_sec", 0.0)))
        
        bridge_candidates = []
        first_start = float(blocks[0].get("start_sec", 0.0))
        if first_start >= min_gap:
            bridge_candidates.append({
                "pos_idx": 0,
                "gap_start": 2.0,
                "gap_end": first_start - 1.0,
                "gap_dur": first_start - 3.0,
                "prev_text": "Movie opening introduction",
                "next_text": blocks[0].get("narration", "")[:60]
            })

        for i in range(len(blocks) - 1):
            curr_end = float(blocks[i].get("end_sec", 0.0))
            next_start = float(blocks[i+1].get("start_sec", 0.0))
            gap = next_start - curr_end
            if gap >= min_gap:
                bridge_candidates.append({
                    "pos_idx": i + 1,
                    "gap_start": curr_end + 1.5,
                    "gap_end": next_start - 1.5,
                    "gap_dur": gap,
                    "prev_text": blocks[i].get("narration", "")[:80],
                    "next_text": blocks[i+1].get("narration", "")[:80]
                })

        if not bridge_candidates:
            return blocks

        print(f"[*] WriterAgent (Action Narration): Detected {len(bridge_candidates)} silent/action sequences (> {min_gap}s). Generating recap narration...")

        new_blocks = list(blocks)
        added_count = 0
        for cand in bridge_candidates[:10]:
            prompt = (
                f"Movie Title: {state.movie_name}\n"
                f"In this movie recap, there is an action or suspense sequence lasting {int(cand['gap_dur'])} seconds without dialogue.\n"
                f"Previous dialogue: '{cand['prev_text']}'\n"
                f"Upcoming dialogue: '{cand['next_text']}'\n\n"
                # FIX-BUG4: Dynamic language for bridge narration
                f"Write a short, engaging 1-2 sentence narrator recap in natural colloquial {self.language.title()} "
                f"describing what happens in the scene or setting up the tension.\n"
                f"RULES:\n"
                f"- Max 20 words.\n"
                f"- Conversational recap style.\n"
                f"- NO formal written language.\n"
                f"- Return ONLY the narration text, no JSON, no quotes, no explanation."
            )
            try:
                res, _ = call_gemini(
                    "You are an expert movie recap channel narrator.",
                    prompt,
                    gemini_key,
                    model=model,
                    temperature=0.4,
                    max_tokens=150,
                    response_mime_type="text/plain",  # FIX-BUG3: plain text, not JSON
                )
                txt = res.strip().strip('"').strip("'").strip()
                if txt.startswith("{") or txt.startswith("["):
                    try:
                        data = json.loads(txt)
                        if isinstance(data, dict):
                            for k in ["narration", "recap", "text", "summary", "recaps"]:
                                if k in data:
                                    val = data[k]
                                    txt = val[0] if isinstance(val, list) and val else str(val)
                                    break
                        elif isinstance(data, list) and data:
                            txt = str(data[0])
                    except Exception as _exc:
                        log_suppressed("agents.writer_agent._bridge_action_narration", _exc)
                txt = txt.strip().strip('"').strip("'").strip()
                if txt and len(txt) > 4:
                    if getattr(self, "language", "burmese").lower() in ["burmese", "mm", "myanmar"]:
                        try:
                            txt = replace_numbers_with_burmese(txt)
                            txt = transliterate_english_acronyms(txt)
                            txt = sanitize_burmese_narration(txt)
                        except Exception as _exc:
                            log_suppressed("agents.writer_agent._bridge_action_narration", _exc)
                    bridge_dur = min(cand["gap_dur"] - 2.0, 5.5)
                    b_start = round(cand["gap_start"], 2)
                    b_end = round(b_start + max(bridge_dur, 3.0), 2)
                    new_blocks.append({
                        "scene_id": f"action_bridge_{added_count+1}",
                        "narration": txt,
                        "start_sec": b_start,
                        "end_sec": b_end,
                        "gender": "male",
                        "character": "Narrator",
                        "emotion": "excited",
                        "visual_cue": f"Action Scene ({b_start}s - {b_end}s)"
                    })
                    added_count += 1
            except Exception as e:
                print(f"[WARN] WriterAgent: Action narration failed for gap at {cand['gap_start']}s: {e}")

        new_blocks.sort(key=lambda x: float(x.get("start_sec", 0.0)))
        print(f"[OK] WriterAgent (Action Narration): Successfully bridged {added_count} action scenes with lively commentary!")
        return new_blocks


    # ─────────────────────────────────────────────────────
    # JSON PARSER
    # ─────────────────────────────────────────────────────
    def _parse_script(self, raw: str):
        """Robustly extracts a JSON array from LLM output."""
        clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        clean = re.sub(r',\s*([\]}])', r'\1', clean)

        # Strategy 1: Direct parse
        try:
            return self._normalise(json.loads(clean))
        except json.JSONDecodeError as _exc:
            log_suppressed("agents.writer_agent._parse_script", _exc)

        # Strategy 2: Slice from first [ to last ]
        for open_c, close_c in [('[', ']'), ('{', '}')]:
            start = clean.find(open_c)
            end   = clean.rfind(close_c)
            if start != -1 and end > start:
                try:
                    return self._normalise(json.loads(clean[start:end + 1]))
                except json.JSONDecodeError as _exc:
                    log_suppressed("agents.writer_agent._parse_script", _exc)

        # Strategy 3: Strip trailing extra brackets
        clean_brackets = re.sub(r'\]\s*\]+$', ']', clean)
        try:
            return self._normalise(json.loads(clean_brackets))
        except json.JSONDecodeError as _exc:
            log_suppressed("agents.writer_agent._parse_script", _exc)

        # Strategy 4: Regex full array (greedy match from first [ to last ])
        m = re.search(r'\[.*\]', clean_brackets, re.DOTALL)
        if m:
            try:
                return self._normalise(json.loads(m.group()))
            except json.JSONDecodeError as _exc:
                log_suppressed("agents.writer_agent._parse_script", _exc)
                
        # Strategy 5: Stack-based JSON extraction (most robust)
        start_idx = clean.find('[')
        if start_idx != -1:
            stack = 0
            for i in range(start_idx, len(clean)):
                if clean[i] == '[': stack += 1
                elif clean[i] == ']': stack -= 1
                if stack == 0:
                    try:
                        return self._normalise(json.loads(clean[start_idx:i+1]))
                    except json.JSONDecodeError:
                        break

        # Strategy 5b: the answer was cut off mid-array (max tokens) - keep every complete object before the cut
        salvaged = self._salvage_objects(clean)
        if salvaged:
            print(f"[*] WriterAgent: Answer was cut off; recovered {len(salvaged)} complete item(s).")
            return self._normalise(salvaged)

        # Strategy 6: Plain paragraphs → blocks (rejecting JSON artifacts or meta chatter)
        raw_paragraphs =[p.strip() for p in raw.split('\n\n') if len(p.strip()) > 10]
        valid_paragraphs = []
        for p in raw_paragraphs:
            p_lower = p.lower()
            if any(bad in p_lower for bad in ["```", "json", "here is", "here's", "i have", "translate"]):
                continue
            valid_paragraphs.append(p)
        if valid_paragraphs:
            print(f"[*] WriterAgent: Extracted {len(valid_paragraphs)} paragraphs from plain text.")
            return [
                {
                    "scene_id":   str(i + 1),
                    "narration":  p,
                    "visual_cue": "Continue narration",
                }
                for i, p in enumerate(valid_paragraphs)
            ]

        return None

    @staticmethod
    def _salvage_objects(text: str) -> list:
        """Every complete ``{...}`` object found in ``text`` (a truncated JSON array loses only its last item)."""
        decoder = json.JSONDecoder()
        found = []
        i = text.find('{')
        while i != -1:
            try:
                obj, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                i = text.find('{', i + 1)   # a cut-off wrapper/object: look for complete ones inside it
                continue
            if isinstance(obj, dict):
                found.append(obj)
            i = text.find('{', end)
        return found

    def _normalise(self, data) -> list:
        """Ensure the script is a list of dicts with string values.
        CRITICAL: Preserve start_sec and end_sec so video sync is never broken.
        """
        if not data:
            return []
        if isinstance(data, dict):
            # Some answers wrap the array: {"translations": [...]}
            wrapped = next((data[k] for k in ("translations", "items", "lines", "script", "result", "data", "segments")
                            if isinstance(data.get(k), list)), None)
            data = wrapped if wrapped is not None else [data]
        elif not isinstance(data, list):
            return []

        result = []
        for i, item in enumerate(data):
            if isinstance(item, str):
                narration = item
                block_id = i + 1
                item = {}
            elif isinstance(item, dict):
                narration = (
                    item.get("narration")
                    or item.get("translation")
                    or item.get("myanmar_text")
                    or item.get("burmese_text")
                    or item.get("text")
                    or ""
                )
                block_id = item.get("id", item.get("scene_id", i + 1))
            else:
                continue

            if not str(narration).strip():
                continue
            block = {
                "id":         block_id,
                "scene_id":   str(block_id),
                "narration":  str(narration).strip(),
                "visual_cue": str(item.get("visual_cue", "Dialogue")),
            }
            # Preserve exact timestamps — these drive character lip-sync placement
            if "start_sec" in item:
                try: block["start_sec"] = float(item["start_sec"])
                except (ValueError, TypeError) as _exc:
                    log_suppressed("agents.writer_agent._normalise", _exc)
            if "end_sec" in item:
                try: block["end_sec"] = float(item["end_sec"])
                except (ValueError, TypeError) as _exc:
                    log_suppressed("agents.writer_agent._normalise", _exc)
            if "character" in item and item["character"]:
                block["character"] = str(item["character"]).strip()
            if "speaker" in item and item["speaker"]:
                block["speaker"] = str(item["speaker"]).strip()
                if "character" not in block:
                    block["character"] = block["speaker"]
            elif "character" in block:
                block["speaker"] = block["character"]
            if "gender" in item:
                block["gender"] = str(item["gender"])
            if "emotion" in item:
                block["emotion"] = str(item["emotion"])
            result.append(block)
        return result
