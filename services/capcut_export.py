"""
CapCut-ready package
====================
Burmese creators finish their videos in CapCut. This builds ``capcut_package.zip`` from a finished recap
project so that nothing has to be re-done by hand there:

    narration.mp3    the whole voice-over as ONE track, every line already at its time (drop it on the timeline)
    subtitles.srt    UTF-8 *with BOM* (CapCut mangles Burmese in a BOM-less file) timed to that track
    subtitles.ass    the styled subtitles, when the render produced them
    script.txt       the narration with time codes, for proof-reading
    README.txt       the import steps (Burmese + English)

It reuses the merger's own clip layout and track assembly, so the track is the same audio the rendered video
has. It reads the project folder only - nothing is re-rendered and the project is not changed.
"""

import os
import shutil
import subprocess
import tempfile
import types
import zipfile
from typing import Any, Dict, Optional

from brain.diagnostics import log_suppressed
from services import projects as project_store

PACKAGE_NAME = "capcut_package.zip"
PHASE_VOICE = project_store.PHASE_VOICE
_ENCODE_TIMEOUT_SEC = 900


class CapcutError(project_store.ProjectError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
    if found and os.path.exists(found):
        return found
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except Exception as exc:
        log_suppressed("services.capcut_export._ffmpeg", exc)
        return "ffmpeg"


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


README_TEXT = """\
My Narrator package for CapCut Desktop / CapCut မှာ ဆက်လုပ်ရန် လမ်းညွှန်
=====================================================================

Project: {project}
Narration length: {length}   |   Lines: {lines}

[မြန်မာ]
၁။ CapCut ကိုဖွင့်ပြီး ပရောဂျက်အသစ်ဖန်တီးပါ၊ မူရင်းဗီဒီယိုကို timeline ပေါ်တင်ပါ။
၂။ narration.mp3 ကို Import လုပ်ပြီး audio track ပေါ်သို့ အစ (00:00) မှာ ချပါ။
   (စာကြောင်းတိုင်းကို သူ့အချိန်နေရာမှာ ထည့်ပြီးသား တစ်ဖိုင်တည်း ဖြစ်ပါတယ်။)
၃။ Captions > Import captions (Local) > subtitles.srt ကို ရွေးပါ။
   ဖိုင်ကို UTF-8 BOM နဲ့ သိမ်းထားလို့ မြန်မာစာ မပျက်ပါဘူး။
၄။ စာတန်း ဖောင့်ကို Padauk သို့မဟုတ် Noto Sans Myanmar (အခမဲ့) ရွေးပါ၊ မဟုတ်ရင် Windows ရဲ့ Myanmar Text ကိုသုံးပါ။
၅။ မူရင်းဗီဒီယိုရဲ့ အသံကို လိုသလောက် လျှော့ပြီး (သို့မဟုတ် ပိတ်ပြီး) narration ကို ကြားနိုင်အောင် လုပ်ပါ။

[English]
1. Open CapCut, create a project and put your original video on the timeline.
2. Import narration.mp3 and place it on an audio track at 00:00. It is one file with every line already at its time.
3. Captions > Import captions (Local) > choose subtitles.srt. It is saved as UTF-8 with BOM so Burmese displays correctly.
4. Pick a Myanmar font for the captions (Padauk or Noto Sans Myanmar are free; Windows has Myanmar Text).
5. Lower or mute the original video's own audio so the narration is heard.

Files:
  narration.mp3   full narration track (lines at their original time positions)
  subtitles.srt   captions, UTF-8 with BOM
{ass_line}  script.txt      narration with time codes (for proof-reading)

If a line sounds wrong, fix it in the dashboard's Script Editor, press "Save & re-render", then build this package again.
"""


def _ass_line(has_ass: bool) -> str:
    return "  subtitles.ass   styled captions (for players / editors that read ASS)\n" if has_ass else ""


def export_capcut_package(project_dir: str, name: str = "") -> Dict[str, Any]:
    """Builds ``capcut_package.zip`` inside ``project_dir`` and describes it. Raises ``CapcutError`` when not ready."""
    state = project_store._read_json(os.path.join(project_dir, project_store.STATE_FILE))
    script = state.get("generated_script") if isinstance(state, dict) else None
    if not isinstance(script, list) or not script:
        raise CapcutError("This project has no narration script to export.", code="no_script")
    if PHASE_VOICE not in (state.get("completed_phases") or []):
        raise CapcutError(
            "The voice-over is not up to date (lines were edited, or the voice step has not finished). "
            "Press 'Save & re-render' first, then export.", code="not_ready")
    if not os.path.isdir(os.path.join(project_dir, "voiceover")):
        raise CapcutError("The voiceover folder is missing - run the project first.", code="no_audio")

    # The merger's own layout: collect the clips, anchor each to its scene time, then assemble one linear track.
    from agents.merger.media_utils import _assemble_voiceover_track
    from agents.merger.plan import MergePlan
    from agents.merger.voiceover import VoiceoverLayoutMixin
    from core.subtitle_builder import build_srt_script

    blocks = [dict(b) for b in script if isinstance(b, dict)]
    fake_state = types.SimpleNamespace(generated_script=blocks, subtitle_timings=[])
    plan = MergePlan(output_dir=project_dir, video_dur=float(source_duration_sec(state)))
    layout = VoiceoverLayoutMixin()
    layout._collect_voiceover_clips(plan, fake_state)
    if not plan.audio_items:
        raise CapcutError("No synthesized voice clips were found.", code="no_audio")
    layout._layout_voiceover_clips(plan, fake_state)
    if not plan.clips_with_timing:
        raise CapcutError("No voice clip fits inside the video length.", code="no_audio")

    missing = len(blocks) - len(plan.audio_items)
    total = max(plan.video_dur, plan.curr_t)
    out_zip = os.path.join(project_dir, PACKAGE_NAME)
    work = tempfile.mkdtemp(prefix="pai_capcut_")
    try:
        wav = os.path.join(work, "narration.wav")
        mp3 = os.path.join(work, "narration.mp3")
        _assemble_voiceover_track(plan.clips_with_timing, total, wav)
        res = subprocess.run(
            [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", wav, "-c:a", "libmp3lame", "-q:a", "2", mp3],
            capture_output=True, timeout=_ENCODE_TIMEOUT_SEC,
        )
        if res.returncode != 0 or not os.path.exists(mp3):
            tail = res.stderr.decode("utf-8", errors="replace")[-200:] if res.stderr else ""
            raise CapcutError(f"Could not encode narration.mp3: {tail}", code="encode_failed")

        segments = [{"start": start, "end": start + dur, "burmese": text} for start, dur, text in plan.subtitle_timings]
        srt = build_srt_script(segments, text_key="burmese")
        script_txt = "\n".join(f"[{_clock(s)}] {t}" for s, _d, t in plan.subtitle_timings if t)
        ass_path = os.path.join(project_dir, "myanmar_subs.ass")
        has_ass = os.path.exists(ass_path)
        readme = README_TEXT.format(project=name or state.get("movie_name") or "", length=_clock(total),
                                    lines=len(plan.clips_with_timing), ass_line=_ass_line(has_ass))

        tmp_zip = out_zip + ".tmp"
        with zipfile.ZipFile(tmp_zip, "w") as zf:
            zf.write(mp3, "narration.mp3", compress_type=zipfile.ZIP_STORED)           # already compressed
            zf.writestr("subtitles.srt", "﻿" + srt, compress_type=zipfile.ZIP_DEFLATED)
            if has_ass:
                zf.write(ass_path, "subtitles.ass", compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("script.txt", script_txt + "\n", compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("README.txt", readme, compress_type=zipfile.ZIP_DEFLATED)
            files = zf.namelist()
        os.replace(tmp_zip, out_zip)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(out_zip + ".tmp"):
            try:
                os.remove(out_zip + ".tmp")
            except OSError as exc:
                log_suppressed("services.capcut_export.export_capcut_package", exc)

    return {
        "zip": PACKAGE_NAME,
        "size_mb": round(os.path.getsize(out_zip) / 1e6, 1),
        "files": files,
        "narration_sec": round(total, 1),
        "lines": len(plan.clips_with_timing),
        "missing_clips": max(0, missing),
    }


def source_duration_sec(state: Dict[str, Any]) -> float:
    """The source video length in seconds from state.json ("HH:MM:SS" string or a number); 0 when unknown."""
    value: Optional[Any] = state.get("duration") if isinstance(state, dict) else None
    try:
        parts = [float(p) for p in str(value).split(":")]
    except (TypeError, ValueError):
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds if len(parts) <= 3 else 0.0

