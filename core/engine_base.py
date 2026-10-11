"""
Shared base of the subtitle studios (Engine 2 and Engine 3)
===========================================================
Both engines follow the same steps - ingest -> transcript -> translate -> QA repair -> render ->
report - and used to carry their own copy of the plumbing. The common parts live here:

* construction, cancellation, API-key discovery, URL detection, media probing;
* the QA "audit and repair" pass, written once as a skeleton. What genuinely differs between the
  engines (the prompt, how the model's answer is read, how a repaired line is cleaned) is a small
  set of ``_repair_*`` hooks each engine overrides.

Engine modules keep their own ``call_gemini`` binding (``_call_llm``) so tests - and anyone
monkeypatching the engine module - keep working.
"""

import json
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import brain.config as cfg
from agents.downloader_agent import DownloaderAgent
from brain.burmese_utils import (
    has_untranslated_foreign_script,
    sanitize_burmese_narration,
    sanitize_dialogue_persona_particles,
    strip_trailing_subtitle_punctuation,
)
from brain.diagnostics import log_suppressed

_WUXIA_STYLES = ("wuxia", "cultivation", "historical", "costume")
_REPAIR_BATCH_SIZE = 15
_REPAIR_RETRIES = 3
_PLACEHOLDER_LINE = "[စကားသံ]"


class StudioEngineBase:
    """Plumbing shared by ``HardsubEngine`` and ``SubtitleEngine``."""

    #: shown in "QA Audit" messages ("dialogue lines" / "subtitle lines")
    line_label = "dialogue"
    #: key holding a segment's id in this engine's records ("id" / "no")
    segment_id_key = "id"
    #: translation style used when the caller gives none
    default_translation_style = "persona"
    #: text of the InterruptedError raised on cancellation
    cancel_message = "Execution cancelled by user."
    #: print a "[STOP]" banner when cancelled
    announce_cancel = False

    def __init__(self, output_base_dir: str = "outputs", cookies_path: Optional[str] = None, cancel_event=None):
        self.output_base_dir = os.path.abspath(output_base_dir)
        # Explicit --cookies wins; otherwise reuse the cookie file uploaded through the dashboard.
        self.cookies_path = cookies_path or DownloaderAgent.find_cookies_file()
        self.cancel_event = cancel_event
        self.config_data = cfg.load_config()
        self.ffmpeg_bin = self._resolve_ffmpeg_bin()
        os.makedirs(self.output_base_dir, exist_ok=True)

    # ── hooks the engines provide ───────────────────────────────────────────
    def _resolve_ffmpeg_bin(self) -> str:
        raise NotImplementedError

    def _call_llm(self, **kwargs) -> Tuple[str, str]:
        """Calls the model. Implemented in the engine module so its ``call_gemini`` is patchable."""
        raise NotImplementedError

    def _repair_system_prompt(self, style: str, item_count: int) -> str:
        raise NotImplementedError

    def _repair_user_prompt(self, hint_block: str, items: List[Dict]) -> str:
        raise NotImplementedError

    def _repair_texts(self, segments: List[Dict], batch_idxs: List[int], parsed: List) -> List[Optional[str]]:
        """One repaired text (or None) per index in ``batch_idxs``, read from the model's answer."""
        raise NotImplementedError

    def _clean_repaired_text(self, text: str) -> str:
        raise NotImplementedError

    # ── small shared helpers ────────────────────────────────────────────────
    @staticmethod
    def is_url(path_or_url: str) -> bool:
        text = str(path_or_url).strip()
        return DownloaderAgent.is_url(text) or text.startswith(("http://", "https://", "www.youtube.com", "youtu.be"))

    def _check_cancellation(self):
        cancelled = bool(self.cancel_event and getattr(self.cancel_event, "is_set", lambda: False)())
        if cancelled or os.environ.get("CURRENT_JOB_CANCELLED") == "1":
            if self.announce_cancel:
                print(f"\n[STOP] {type(self).__name__} was force-stopped by user.")
            raise InterruptedError(self.cancel_message)

    def _get_api_keys(self) -> List[str]:
        """Config keys plus ``GEMINI_API_KEYS`` / ``GEMINI_API_KEY`` (never mutates the config)."""
        keys = list(self.config_data.get("gemini", {}).get("api_keys", []))
        env_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY")
        if env_keys:
            for key in (k.strip() for k in re.split(r"[,\n;]+", env_keys)):
                if key and key not in keys:
                    keys.append(key)
        return keys

    def _probe_video_metadata(self, video_path: str) -> dict:
        """Duration, size and whether an audio stream exists (parsed from ``ffmpeg -i``)."""
        dur, fps, w, h = 0.0, 30.0, 1920, 1080
        has_audio = True
        try:
            res = subprocess.run([self.ffmpeg_bin, "-i", video_path, "-hide_banner"],
                                 stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, errors="replace")
            err = res.stderr
            dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
            if dur_match:
                hours, minutes, seconds = dur_match.groups()
                dur = float(hours) * 3600.0 + float(minutes) * 60.0 + float(seconds)
            size_match = re.search(r",\s*(\d{2,5})x(\d{2,5})", err)
            if size_match:
                w, h = int(size_match.group(1)), int(size_match.group(2))
            has_video = bool(re.search(r"Stream #\d+:\d+.*Video:", err))
            has_audio_stream = bool(re.search(r"Stream #\d+:\d+.*Audio:", err))
            has_audio = has_audio_stream or not has_video
        except Exception as exc:
            log_suppressed("core.engine_base._probe_video_metadata", exc)
        return {
            "title": os.path.splitext(os.path.basename(video_path))[0],
            "duration": dur,
            "fps": fps,
            "width": w,
            "height": h,
            "has_audio": has_audio,
        }

    # ── QA: audit + self-healing repair (skeleton) ──────────────────────────
    def _audit_and_repair_untranslated(
        self,
        segments: List[Dict],
        translation_style: Optional[str] = None,
        context_hint: Optional[str] = None,
        source_lang: str = "auto",
    ) -> List[Dict]:
        """
        1. finds lines that are empty or still contain foreign script / original text;
        2. re-prompts the model in small batches with a strict "no foreign script" instruction;
        3. sanitises persona particles and trailing punctuation everywhere.

        Sets ``last_damaged_count`` / ``last_repaired_count``.
        """
        label = self.line_label
        api_keys = self._get_api_keys()
        damaged = [
            idx for idx, seg in enumerate(segments)
            if not str(seg.get("burmese", "")).strip() or has_untranslated_foreign_script(str(seg.get("burmese", "")).strip())
        ]
        self.last_damaged_count = len(damaged)
        self.last_repaired_count = 0

        if not damaged:
            print(f"[OK] QA Audit: 100% of {label} lines verified in pure Myanmar Unicode. Zero foreign script residue.")
            self._final_sanitise(segments)
            return segments

        print(f"\n[*] QA Auto-Repair: Detected {len(damaged)} untranslated or damaged {label} lines. "
              f"Launching focused Gemini repair pass...")

        if not api_keys:
            print("[WARN] No Gemini API keys available for repair. Applying phonetic fallback cleansing.")
            for idx in damaged:
                seg = segments[idx]
                seg["burmese"] = strip_trailing_subtitle_punctuation(
                    sanitize_burmese_narration(seg.get("burmese") or seg.get("original", ""))
                ) or _PLACEHOLDER_LINE
            return segments

        model_name = cfg.load_config().get("gemini", {}).get("models", {}).get("workhorse", "gemini-3.5-flash-lite")
        style = str(translation_style or self.default_translation_style).lower().strip()
        hint = str(context_hint).strip() if context_hint and str(context_hint).strip() else ""
        hint_block = f"USER STORY & CHARACTER GUIDANCE:\n{hint}\n\n" if hint else ""

        repaired = 0
        for start in range(0, len(damaged), _REPAIR_BATCH_SIZE):
            self._check_cancellation()
            batch = damaged[start:start + _REPAIR_BATCH_SIZE]
            items = [
                {"id": segments[i].get(self.segment_id_key, i + 1), "original": segments[i].get("original", "")}
                for i in batch
            ]
            system_prompt = self._repair_system_prompt(style, len(items))
            user_prompt = self._repair_user_prompt(hint_block, items)

            parsed = self._request_repair(system_prompt, user_prompt, api_keys, model_name)
            if not isinstance(parsed, list):
                continue

            for seg_idx, text in zip(batch, self._repair_texts(segments, batch, parsed)):
                seg = segments[seg_idx]
                text = str(text or "").strip()
                if text and not has_untranslated_foreign_script(text):
                    seg["burmese"] = self._clean_repaired_text(text)
                    repaired += 1
                else:
                    fallback = strip_trailing_subtitle_punctuation(
                        sanitize_burmese_narration(text or seg.get("burmese", ""))
                    )
                    seg["burmese"] = fallback if fallback.strip() else _PLACEHOLDER_LINE
                    if fallback.strip():
                        repaired += 1

        self._final_sanitise(segments)
        self.last_repaired_count = repaired
        print(f"[OK] QA Auto-Repair: Successfully repaired {repaired}/{len(damaged)} {label} lines.")
        return segments

    def _request_repair(self, system_prompt: str, user_prompt: str, api_keys: List[str], model_name: str):
        """One repair batch with retries; returns the parsed JSON list (or None)."""
        parsed = None
        for _attempt in range(_REPAIR_RETRIES):
            try:
                raw, _model = self._call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    api_key=api_keys,
                    model=model_name,
                    temperature=0.2,
                    response_mime_type="application/json",
                )
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and "translations" in parsed:
                    parsed = parsed["translations"]
                if isinstance(parsed, list) and len(parsed) > 0:
                    break
            except Exception:
                time.sleep(2)
        return parsed

    @staticmethod
    def _final_sanitise(segments: List[Dict]) -> None:
        for seg in segments:
            if seg.get("burmese"):
                seg["burmese"] = sanitize_dialogue_persona_particles(seg["burmese"])
                seg["burmese"] = strip_trailing_subtitle_punctuation(seg["burmese"])
