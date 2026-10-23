"""
CapCut-ready package
====================
Burmese creators finish their videos in CapCut. This builds ``capcut_package.zip`` from a finished project
of ANY studio, so that nothing has to be re-done by hand there. What goes in depends on the project:

    Recap       narration.mp3 (the whole voice-over as ONE track, every line already at its time) +
                subtitles.srt + subtitles.ass + script.txt + README.txt
    Subtitle /  subtitles.srt (Burmese) + subtitles_original.srt + subtitles.ass + transcript.txt + README.txt
    Hardsub     (the original video and its own audio go on the timeline as they are)
    Shorts      clip_NN.mp4 (a clean cut of the source, no burnt-in text) + clip_NN.srt + captions.txt + README.txt

Every .srt is UTF-8 *with BOM* - CapCut garbles Burmese in a BOM-less file. The recap track reuses the merger's
own clip layout and assembly, so it is the same audio the rendered video has. A package only reads the project
folder: nothing is re-rendered and the project is not changed. It is built automatically at the end of a run in
the "CapCut pipeline" / "both" delivery modes (core/delivery.py) and on demand from the Projects view.
"""

import os
import shutil
import subprocess
import tempfile
import types
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brain.diagnostics import log_suppressed
from core.translation_quality import block_is_silent
from services import projects as project_store

PACKAGE_NAME = "capcut_package.zip"
PHASE_VOICE = project_store.PHASE_VOICE
CLEAN_CLIPS_DIR = "capcut_clips"          # clean Shorts (no burnt-in text) written by the Shorts tool
_ENCODE_TIMEOUT_SEC = 900
_BOM = "﻿"


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


def _clock(seconds: float, rounded: bool = False) -> str:
    """``hh:mm:ss``. A time code is cut down (65.5 s is 00:01:05, the second it is in); a length is rounded."""
    total = max(0, int(round(seconds)) if rounded else int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


# ── README texts (Burmese + English) ──────────────────────────────────────────────
_HEAD = """\
Pai AI Movie Studio - CapCut package / CapCut မှာ ဆက်လုပ်ရန် လမ်းညွှန်
=====================================================================

Project: {project}
{facts}
"""

_FOOT = """
If a line sounds or reads wrong, fix it in the dashboard (Projects > Edit script, then Save & re-render), then build this package again.
"""

README_NARRATION = _HEAD + """
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
""" + _FOOT

README_TEXT_ONLY = _HEAD + """
This project has no synthesized voice-over (the audio mode is not "AI voice-over", or the voice step has not run),
so the package has the captions and the script only.
ဒီပရောဂျက်မှာ AI အသံ မပါပါ။ စာတန်းနဲ့ စာသားဖိုင်ပဲ ပါပါတယ်။

[မြန်မာ]
၁။ CapCut ထဲ မူရင်းဗီဒီယိုကို timeline ပေါ်တင်ပါ။
၂။ Captions > Import captions (Local) > subtitles.srt ကို ရွေးပါ (UTF-8 BOM နဲ့ မြန်မာစာ မပျက်ပါ)။
၃။ script.txt ကို ကိုယ်တိုင်ဖတ်သံသွင်းရန် သုံးနိုင်ပါတယ်။

[English]
1. Put your original video on the CapCut timeline.
2. Captions > Import captions (Local) > subtitles.srt (UTF-8 with BOM, so Burmese displays correctly).
3. script.txt is the narration with time codes, e.g. to record your own voice.

Files:
  subtitles.srt   captions, UTF-8 with BOM
{ass_line}  script.txt      narration with time codes
""" + _FOOT

README_SUBTITLES = _HEAD + """
[မြန်မာ]
၁။ CapCut ထဲ မူရင်းဗီဒီယိုကို timeline ပေါ်တင်ပါ (မူရင်းအသံက သူ့အတိုင်း ရှိနေပါမယ်)။
၂။ Captions > Import captions (Local) > subtitles.srt ကို ရွေးပါ။ UTF-8 BOM နဲ့ သိမ်းထားလို့ မြန်မာစာ မပျက်ပါဘူး။
၃။ စာတန်း ဖောင့်ကို Padauk သို့မဟုတ် Noto Sans Myanmar (အခမဲ့) ရွေးပါ၊ မဟုတ်ရင် Windows ရဲ့ Myanmar Text ကိုသုံးပါ။
၄။ ဘာသာနှစ်မျိုး ပြချင်ရင် subtitles_original.srt ကိုလည်း ထပ်ထည့်နိုင်ပါတယ်။

[English]
1. Put your original video on the CapCut timeline; its own audio stays as it is.
2. Captions > Import captions (Local) > choose subtitles.srt (UTF-8 with BOM, so Burmese displays correctly).
3. Pick a Myanmar font for the captions (Padauk or Noto Sans Myanmar are free; Windows has Myanmar Text).
4. For two languages on screen, import subtitles_original.srt as a second caption track.

Files:
  subtitles.srt            Burmese captions, UTF-8 with BOM
{original_line}{ass_line}  transcript.txt           the Burmese text with time codes (for proof-reading)
""" + _FOOT

README_SHORTS = _HEAD + """
[မြန်မာ]
၁။ CapCut မှာ clip_NN.mp4 တစ်ခုချင်းကို ပရောဂျက်အသစ်ထဲ ထည့်ပါ၊ canvas ကို 9:16 (TikTok / Reels / Shorts) ရွေးပါ။
၂။ Captions > Import captions (Local) > ဒီ clip နဲ့ အမည်တူ clip_NN.srt ကို ရွေးပါ (UTF-8 BOM နဲ့ မြန်မာစာ မပျက်ပါ)။
၃။ captions.txt ထဲမှာ clip တစ်ခုချင်းရဲ့ ခေါင်းစဉ်၊ caption၊ hashtag တွေ အဆင်သင့်ပါပါတယ်။
{finished_note_my}
[English]
1. In CapCut add each clip_NN.mp4 to a new project and set the canvas to 9:16 (TikTok / Reels / Shorts).
2. Captions > Import captions (Local) > the clip_NN.srt with the same number (UTF-8 with BOM, so Burmese displays correctly).
3. captions.txt has each clip's title, caption and hashtags, ready to paste.
{finished_note_en}
Files:
{file_lines}""" + _FOOT

_FINISHED_NOTE_MY = "မှတ်ချက် - ဒီ clip တွေမှာ စာတန်း ပါပြီးသား (burnt-in) ဖြစ်ပါတယ်။ စာတန်းမပါတဲ့ clip လိုချင်ရင် 'CapCut pipeline' mode နဲ့ ပြန် run ပါ။\n"
_FINISHED_NOTE_EN = "Note: these clips already have the subtitles burnt in. For clean clips, run again with the 'CapCut pipeline' delivery mode.\n"


def _ass_line(has_ass: bool) -> str:
    return "  subtitles.ass   styled captions (for players / editors that read ASS)\n" if has_ass else ""


# ── shared helpers ─────────────────────────────────────────────────────────────────
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


def _bom_srt(rows: List[Dict[str, Any]], text_key: str = "burmese") -> str:
    from core.subtitle_builder import build_srt_script
    return _BOM + build_srt_script(rows, text_key=text_key)


def _bom_text(raw: bytes) -> str:
    """Existing .srt bytes as text with exactly one BOM (CapCut needs it for Burmese)."""
    return _BOM + raw.decode("utf-8-sig", errors="replace")


def _write_package(project_dir: str, entries: List[Tuple[str, Any, int]]) -> Tuple[str, List[str]]:
    """
    Writes ``capcut_package.zip`` atomically. ``entries`` are (name, content, compression): content is text or bytes
    to store, or a ``Path`` of an existing file to copy in. Returns (zip path, names in the zip).
    """
    out_zip = os.path.join(project_dir, PACKAGE_NAME)
    tmp_zip = out_zip + ".tmp"
    try:
        with zipfile.ZipFile(tmp_zip, "w") as zf:
            for name, content, compression in entries:
                if isinstance(content, Path):
                    zf.write(content, name, compress_type=compression)
                else:
                    zf.writestr(name, content, compress_type=compression)
            names = zf.namelist()
        os.replace(tmp_zip, out_zip)
    finally:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except OSError as exc:
                log_suppressed("services.capcut_export._write_package", exc)
    return out_zip, names


def _description(out_zip: str, names: List[str], kind: str, **extra: Any) -> Dict[str, Any]:
    result = {"zip": PACKAGE_NAME, "size_mb": round(os.path.getsize(out_zip) / 1e6, 1), "files": names, "kind": kind}
    result.update(extra)
    return result


def _read_state(project_dir: str) -> Dict[str, Any]:
    state = project_store._read_json(os.path.join(project_dir, project_store.STATE_FILE))
    if not isinstance(state, dict):
        raise CapcutError("This project has no readable state.json.", code="no_state")
    return state


# ── Recap ───────────────────────────────────────────────────────────────────────────
def _recap_package(project_dir: str, name: str, state: Dict[str, Any]) -> Dict[str, Any]:
    script = state.get("generated_script")
    if not isinstance(script, list) or not script:
        raise CapcutError("This project has no narration script to export.", code="no_script")
    voice_dir = os.path.join(project_dir, "voiceover")
    voice_done = PHASE_VOICE in (state.get("completed_phases") or [])
    if os.path.isdir(voice_dir) and not voice_done:
        raise CapcutError(
            "The voice-over is not up to date (lines were edited, or the voice step has not finished). "
            "Press 'Save & re-render' first, then export.", code="not_ready")

    # The merger's own layout: collect the clips, anchor each to its scene time, then assemble one linear track.
    from agents.merger.media_utils import _assemble_voiceover_track
    from agents.merger.plan import MergePlan
    from agents.merger.voiceover import VoiceoverLayoutMixin

    blocks = [dict(b) for b in script if isinstance(b, dict)]
    fake_state = types.SimpleNamespace(generated_script=blocks, subtitle_timings=[])
    plan = MergePlan(output_dir=project_dir, video_dur=float(source_duration_sec(state)))
    layout = VoiceoverLayoutMixin()
    if os.path.isdir(voice_dir):
        layout._collect_voiceover_clips(plan, fake_state)
        if plan.audio_items:
            layout._layout_voiceover_clips(plan, fake_state)
    narrated = bool(plan.audio_items) and bool(plan.clips_with_timing)

    ass_path = os.path.join(project_dir, "myanmar_subs.ass")
    has_ass = os.path.exists(ass_path)
    if narrated:
        timings = plan.subtitle_timings
        total = max(plan.video_dur, plan.curr_t)
        missing = len(blocks) - len(plan.audio_items)
    else:                              # no voice-over: captions and script from the script's own times
        timings = []
        for block in blocks:
            text = str(block.get("narration") or block.get("text") or "").strip()
            if text and not block_is_silent(block):             # source-language text is not a caption
                start = float(block.get("start_sec") or 0.0)
                end = float(block.get("end_sec") or start + 3.0)
                timings.append((start, max(end - start, 0.8), text))
        total = max([plan.video_dur] + [s + d for s, d, _t in timings])
        missing = 0

    work = tempfile.mkdtemp(prefix="pai_capcut_")
    try:
        entries: List[Tuple[str, Any, int]] = []
        if narrated:
            wav, mp3 = os.path.join(work, "narration.wav"), os.path.join(work, "narration.mp3")
            _assemble_voiceover_track(plan.clips_with_timing, total, wav)
            res = subprocess.run(
                [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", wav, "-c:a", "libmp3lame", "-q:a", "2", mp3],
                capture_output=True, timeout=_ENCODE_TIMEOUT_SEC)
            if res.returncode != 0 or not os.path.exists(mp3):
                tail = res.stderr.decode("utf-8", errors="replace")[-200:] if res.stderr else ""
                raise CapcutError(f"Could not encode narration.mp3: {tail}", code="encode_failed")
            entries.append(("narration.mp3", Path(mp3), zipfile.ZIP_STORED))                # already compressed
        segments = [{"start": start, "end": start + dur, "burmese": text} for start, dur, text in timings]
        script_txt = "\n".join(f"[{_clock(s)}] {t}" for s, _d, t in timings if t)
        entries.append(("subtitles.srt", _bom_srt(segments), zipfile.ZIP_DEFLATED))
        if has_ass:
            entries.append(("subtitles.ass", Path(ass_path), zipfile.ZIP_DEFLATED))
        entries.append(("script.txt", script_txt + "\n", zipfile.ZIP_DEFLATED))
        lines = len(plan.clips_with_timing) if narrated else len(timings)
        facts = (f"Narration length: {_clock(total, rounded=True)}   |   Lines: {lines}" if narrated
                 else f"Lines: {lines}   |   No voice-over in this project")
        template = README_NARRATION if narrated else README_TEXT_ONLY
        entries.append(("README.txt", template.format(project=name or state.get("movie_name") or "", facts=facts,
                                                      ass_line=_ass_line(has_ass)), zipfile.ZIP_DEFLATED))
        out_zip, names = _write_package(project_dir, entries)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return _description(out_zip, names, "recap", narration=narrated, narration_sec=round(total, 1) if narrated else None,
                        lines=lines, missing_clips=max(0, missing))


# ── Subtitle / Hardsub ──────────────────────────────────────────────────────────────
def _subtitle_package(project_dir: str, name: str, state: Dict[str, Any]) -> Dict[str, Any]:
    records = project_store._read_json(os.path.join(project_dir, "records_data.json"))
    records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
    srt_on_disk = os.path.join(project_dir, "05_subtitle_burmese.srt")
    ass_path = os.path.join(project_dir, "05_subtitle_burmese.ass")
    has_ass = os.path.exists(ass_path)

    entries: List[Tuple[str, Any, int]] = []
    if any(str(r.get("burmese") or "").strip() for r in records):
        entries.append(("subtitles.srt", _bom_srt(records, "burmese"), zipfile.ZIP_DEFLATED))
        has_original = any(str(r.get("original") or "").strip() for r in records)
        if has_original:
            entries.append(("subtitles_original.srt", _bom_srt(records, "original"), zipfile.ZIP_DEFLATED))
        lines = []
        for r in records:
            text = str(r.get("burmese") or "").strip()
            start = r.get("start_s", r.get("start", 0.0))
            try:
                stamp = _clock(float(start))
            except (TypeError, ValueError):
                stamp = "00:00:00"
            if text:
                lines.append(f"[{stamp}] {text}")
        entries.append(("transcript.txt", "\n".join(lines) + "\n", zipfile.ZIP_DEFLATED))
        count = len(lines)
    elif os.path.isfile(srt_on_disk):                    # no records (older project): reuse the written .srt
        with open(srt_on_disk, "rb") as fh:
            entries.append(("subtitles.srt", _bom_text(fh.read()), zipfile.ZIP_DEFLATED))
        has_original = False
        count = state.get("total_records") or 0
    else:
        raise CapcutError("This project has no subtitles to export yet.", code="no_script")
    if has_ass:
        entries.append(("subtitles.ass", Path(ass_path), zipfile.ZIP_DEFLATED))
    facts = f"Subtitle lines: {count}   |   The original video and its audio are not included"
    original_line = "  subtitles_original.srt   original-language captions, UTF-8 with BOM\n" if has_original else ""
    entries.append(("README.txt", README_SUBTITLES.format(project=name or state.get("movie_name") or "", facts=facts,
                                                          original_line=original_line, ass_line=_ass_line(has_ass)),
                    zipfile.ZIP_DEFLATED))
    out_zip, names = _write_package(project_dir, entries)
    return _description(out_zip, names, state.get("engine_type") or "subtitle", narration=False, lines=count)


# ── Shorts ──────────────────────────────────────────────────────────────────────────
def _shorts_package(project_dir: str, name: str, state: Dict[str, Any]) -> Dict[str, Any]:
    manifest = project_store._read_json(os.path.join(project_dir, "shorts.json"))
    clips = manifest.get("clips") if isinstance(manifest, dict) else None
    if not clips:
        raise CapcutError("This project has no Shorts to export yet.", code="no_script")

    entries: List[Tuple[str, Any, int]] = []
    clean = finished = 0
    for clip in clips:
        number = int(clip.get("index") or 0)
        clean_name = clip.get("clean_file")
        source = os.path.join(project_dir, clean_name) if clean_name else None
        if not (source and os.path.isfile(source)):
            main = (clip.get("files") or {}).get("9:16") or next(iter((clip.get("files") or {}).values()), None)
            source = os.path.join(project_dir, main) if main else None
            if not (source and os.path.isfile(source)):
                continue
            finished += 1
        else:
            clean += 1
        entries.append((f"clip_{number:02d}.mp4", Path(source), zipfile.ZIP_STORED))
        srt = clip.get("subtitles")
        if srt and os.path.isfile(os.path.join(project_dir, srt)):
            with open(os.path.join(project_dir, srt), "rb") as fh:
                entries.append((f"clip_{number:02d}.srt", _bom_text(fh.read()), zipfile.ZIP_DEFLATED))
        cover = clip.get("cover")
        if cover and os.path.isfile(os.path.join(project_dir, cover)):
            entries.append((f"clip_{number:02d}.jpg", Path(project_dir, cover), zipfile.ZIP_STORED))
    if not entries:
        raise CapcutError("The Shorts of this project are missing on disk.", code="no_audio")
    captions = os.path.join(project_dir, "captions.txt")
    if os.path.isfile(captions):
        entries.append(("captions.txt", Path(captions), zipfile.ZIP_DEFLATED))
    total = clean + finished
    file_lines = "".join(f"  {n}\n" for n, _c, _z in entries)
    facts = f"Clips: {total}   |   {'clean cuts (no burnt-in text)' if clean else 'finished clips (subtitles burnt in)'}"
    entries.append(("README.txt", README_SHORTS.format(
        project=name or state.get("movie_name") or "", facts=facts, ass_line="", file_lines=file_lines,
        finished_note_my=_FINISHED_NOTE_MY if finished and not clean else "",
        finished_note_en=_FINISHED_NOTE_EN if finished and not clean else ""), zipfile.ZIP_DEFLATED))
    out_zip, names = _write_package(project_dir, entries)
    return _description(out_zip, names, "shorts", narration=False, clips=total, clean_clips=clean, lines=total)


_BUILDERS = {"recap": _recap_package, "subtitle": _subtitle_package, "hardsub": _subtitle_package, "shorts": _shorts_package}


def export_capcut_package(project_dir: str, name: str = "") -> Dict[str, Any]:
    """Builds ``capcut_package.zip`` inside ``project_dir`` and describes it. Raises ``CapcutError`` when not possible."""
    state = _read_state(project_dir)
    engine = state.get("engine_type") or "recap"
    builder = _BUILDERS.get(engine)
    if builder is None:
        raise CapcutError(f"A CapCut package cannot be built for a '{engine}' project.", code="unsupported")
    return builder(project_dir, name, state)


def supported_engines() -> Tuple[str, ...]:
    """Engine types a package can be built for (the dashboard shows the CapCut button only for these)."""
    return tuple(_BUILDERS)
