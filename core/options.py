"""
Option model — every user-facing option defined ONCE
====================================================
The CLI flags, the API request schemas and the dashboard wizard all describe the same
choices. They used to be copied into five places, with different defaults and meanings.
This module is the single definition they are generated from:

* ``STEPS``    – the order in which a user is asked (a step-by-step flow).
* ``OPTIONS``  – each option once: name, type, default, allowed values, the step it belongs
                 to, and the engines it applies to.
* ``resolve``  – applies the defaults for the chosen engine (the one place where
                 "what happens when the user did not choose?" is answered).

Pure Python: no FastAPI / pydantic / argparse imports, so it can be used (and tested) anywhere.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

# ── Engines ──────────────────────────────────────────────────────────────────
RECAP = "recap"        # Engine 1 – movie recap + AI dubbing
SUBTITLE = "subtitle"  # Engine 2 – subtitle & transcript studio
HARDSUB = "hardsub"    # Engine 3 – original audio + burnt-in Burmese subtitles
DOWNLOAD = "download"  # dedicated downloader

ENGINES: Tuple[str, ...] = (RECAP, SUBTITLE, HARDSUB, DOWNLOAD)
ALL = frozenset(ENGINES)
_PIPELINES = frozenset({RECAP, SUBTITLE, HARDSUB})

# ── Steps (asked in this order) ──────────────────────────────────────────────
STEP_SOURCE = "source"
STEP_LANGUAGE = "language"
STEP_OUTPUT = "output"
STEP_PROTECTION = "protection"
STEP_AUDIO = "audio"


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    question: str


STEPS: Tuple[Step, ...] = (
    Step(STEP_SOURCE, "1. Source", "What do you want to process, and with which engine?"),
    Step(STEP_LANGUAGE, "2. Language & translation", "Which language, and in what voice should it be translated?"),
    Step(STEP_OUTPUT, "3. Output & style", "What should the finished video look like?"),
    Step(STEP_PROTECTION, "4. Protection", "How should the original subtitles and copyright fingerprints be handled?"),
    Step(STEP_AUDIO, "5. Audio & pipeline", "How is the audio produced, and which pipeline stages run?"),
)
STEP_IDS: Tuple[str, ...] = tuple(s.id for s in STEPS)


# ── Option definition ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Option:
    name: str
    type: type                       # str | bool | float | int | dict
    default: Any = None
    step: str = STEP_AUDIO
    engines: frozenset = _PIPELINES  # engines that actually use the option
    help: str = ""
    choices: Tuple[str, ...] = ()    # informational (UI / CLI); the API stays permissive
    engine_defaults: Mapping[str, Any] = field(default_factory=dict)
    batch: bool = True               # accepted by the batch endpoint
    required: bool = False
    # An explicit ``null`` is a meaningful value ("let the config decide") and is passed through.
    keep_none: bool = False

    def default_for(self, engine: str) -> Any:
        return self.engine_defaults.get(engine, self.default)

    def applies_to(self, engine: str) -> bool:
        return engine in self.engines


def _o(name, type_, default=None, step=STEP_AUDIO, engines=_PIPELINES, **kw) -> Option:
    return Option(name=name, type=type_, default=default, step=step, engines=frozenset(engines), **kw)


TRANSLATION_STYLES = ("recap", "drama_novel", "audio_drama", "dialogue", "persona", "wuxia", "cinematic")
SUBTITLE_STYLES = ("box_black", "yellow_pop", "white_stroke", "cyan_cyber", "crimson_box")

OPTIONS: Tuple[Option, ...] = (
    # 1 ─ Source
    _o("engine_mode", str, RECAP, STEP_SOURCE, ALL, choices=ENGINES,
       help="Which studio engine runs the job."),
    _o("input", str, None, STEP_SOURCE, ALL, required=True,
       help="Video path or URL."),
    _o("project_name", str, None, STEP_SOURCE, {SUBTITLE, HARDSUB, DOWNLOAD}, batch=False,
       help="Custom project / file name."),
    _o("force_whisper", bool, False, STEP_SOURCE, {SUBTITLE, HARDSUB},
       help="Transcribe with Whisper even when captions exist."),
    _o("resolution", str, "1080p", STEP_SOURCE, ALL, choices=("1080p", "720p"),
       help="Download / output resolution."),

    # 2 ─ Language & translation
    _o("source_language", str, "auto", STEP_LANGUAGE,
       help="Spoken language of the source (auto = detect)."),
    # Target language *and* narrator-voice shortcut (burmese_thiha, nilar ...): the dashboard shows
    # it as the voice picker, so it is asked together with the audio options.
    _o("language", str, "burmese", STEP_AUDIO, {RECAP},
       choices=("burmese", "english", "mm", "en", "burmese_nilar", "burmese_thiha", "nilar", "thiha"),
       help="Target language / narrator voice shortcut."),
    _o("translation_style", str, None, STEP_LANGUAGE, choices=TRANSLATION_STYLES,
       engine_defaults={RECAP: "recap", SUBTITLE: "dialogue", HARDSUB: "persona"},
       help="Voice of the translation."),
    _o("script_engine", str, "recap", STEP_LANGUAGE, {RECAP}, choices=("recap", "translate"),
       help="Script generation mode: recap storyteller or 1:1 translation."),
    _o("context_hint", str, None, STEP_LANGUAGE,
       help="Story / character / genre guidance for the translator."),

    # 3 ─ Output & style
    _o("video_format", str, "both", STEP_OUTPUT, choices=("16:9", "9:16", "both"),
       engine_defaults={SUBTITLE: "16:9"},
       help="Landscape, vertical Reels, or both."),
    _o("reels_enabled", bool, True, STEP_OUTPUT, {RECAP}, keep_none=True,
       help="Legacy toggle: also export 9:16 when video_format is not set."),
    _o("subtitle_style", str, "box_black", STEP_OUTPUT, choices=SUBTITLE_STYLES,
       help="Subtitle look."),
    _o("subtitle_mode", str, "burn", STEP_OUTPUT, {RECAP}, choices=("burn", "none", "auto"),
       help="How subtitles are delivered."),
    _o("render_video", bool, True, STEP_OUTPUT,
       engine_defaults={RECAP: True, SUBTITLE: False, HARDSUB: True},
       help="Render the video (otherwise only text deliverables)."),
    _o("custom_thumb_title", str, None, STEP_OUTPUT, {RECAP}, help="Thumbnail headline."),
    _o("thumbnail_intro", bool, False, STEP_OUTPUT, {RECAP}, keep_none=True,
       help="Prepend a thumbnail intro."),
    _o("trim_end", float, None, STEP_OUTPUT, {RECAP}, batch=False,
       help="Seconds to cut from the end."),
    _o("no_smart_trim", bool, False, STEP_OUTPUT, {RECAP}, batch=False,
       help="Disable automatic outro trimming."),
    _o("outro_card", bool, False, STEP_OUTPUT, {RECAP}, batch=False,
       help="Append an outro card."),
    _o("watermark_enabled", bool, True, STEP_OUTPUT, {RECAP}, keep_none=True,
       help="Overlay a watermark (null = use config)."),
    _o("watermark_text", str, None, STEP_OUTPUT, {RECAP}, help="Watermark text."),
    _o("watermark_opacity", float, None, STEP_OUTPUT, {RECAP}, help="Watermark opacity 0-1."),

    # 4 ─ Protection
    _o("blur_mode", str, "auto", STEP_PROTECTION, choices=("auto", "yes", "no"),
       help="Blur the source's burnt-in subtitles."),
    _o("blur_height", float, None, STEP_PROTECTION, help="Blur band height (0.05-0.40)."),
    _o("mirror", bool, False, STEP_PROTECTION, help="Mirror the video horizontally."),
    _o("color_grading", bool, True, STEP_PROTECTION, {SUBTITLE, HARDSUB},
       help="Subtle colour grade."),
    _o("audio_anti_copyright", bool, False, STEP_PROTECTION, help="Audio tempo shield."),

    # 5 ─ Audio & pipeline
    _o("tts_engine", str, None, STEP_AUDIO, {RECAP}, choices=("edge_tts", "f5_tts"),
       help="Text-to-speech engine."),
    _o("tts_voice", str, None, STEP_AUDIO, {RECAP}, help="Narrator voice."),
    _o("audio_mode", str, None, STEP_AUDIO, choices=("ai_voiceover", "original", "none"),
       engine_defaults={RECAP: "ai_voiceover", SUBTITLE: "original", HARDSUB: "original"},
       help="AI voice-over or the original audio."),
    _o("sfx_mode", str, "original_sfx", STEP_AUDIO, choices=("original_sfx", "bgm", "both", "none"),
       help="Background sound-effect handling."),
    _o("sfx_volume", float, 0.15, STEP_AUDIO, help="Sound-effect volume."),
    _o("skip_demucs", bool, False, STEP_AUDIO, {RECAP}, help="Skip vocal separation."),
    _o("detect_scenes", bool, False, STEP_AUDIO, {RECAP}, help="Scene-cut detection."),
    _o("resume", bool, True, STEP_AUDIO, {RECAP}, help="Resume from cached phases."),
    _o("stage_toggles", dict, None, STEP_AUDIO, help="Per-stage on/off switches."),
)

BY_NAME: Dict[str, Option] = {o.name: o for o in OPTIONS}
assert len(BY_NAME) == len(OPTIONS), "duplicate option name"
assert all(o.step in STEP_IDS for o in OPTIONS), "option with unknown step"


# ── Queries ──────────────────────────────────────────────────────────────────
def normalize_engine(engine: Optional[str]) -> str:
    """Unknown / empty engine names behave like the recap engine (previous behaviour)."""
    return engine if engine in ENGINES else RECAP


def options_for(engine: str) -> List[Option]:
    engine = normalize_engine(engine)
    return [o for o in OPTIONS if o.applies_to(engine)]


def steps_for(engine: str) -> List[Tuple[Step, List[Option]]]:
    """The step-by-step flow for an engine: only steps that have something to ask."""
    opts = options_for(engine)
    flow = []
    for step in STEPS:
        in_step = [o for o in opts if o.step == step.id]
        if in_step:
            flow.append((step, in_step))
    return flow


def describe() -> Dict[str, Any]:
    """JSON-serialisable description of the whole model (for ``GET /api/options``)."""
    return {
        "engines": list(ENGINES),
        "steps": [
            {
                "id": s.id,
                "title": s.title,
                "question": s.question,
                "options": [o.name for o in OPTIONS if o.step == s.id],
            }
            for s in STEPS
        ],
        "options": {
            o.name: {
                "type": o.type.__name__,
                "default": o.default,
                "engine_defaults": dict(o.engine_defaults),
                "step": o.step,
                "engines": [e for e in ENGINES if e in o.engines],
                "choices": list(o.choices),
                "help": o.help,
                "batch": o.batch,
                "required": o.required,
            }
            for o in OPTIONS
        },
    }


# ── Resolution ───────────────────────────────────────────────────────────────
def _is_unset(value: Any, opt: Option) -> bool:
    if value is None:
        return True
    # An empty string is "not chosen" for text options (``value or default`` semantics).
    return opt.type is str and isinstance(value, str) and value == ""


def resolve(engine: Optional[str], values: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Returns a value for every known option: the user's choice, or the engine's default.

    This is the single place where "the user did not choose" is turned into a value; callers
    must not add their own ``or "..."`` fallbacks.
    """
    engine = normalize_engine(engine)
    out: Dict[str, Any] = {}
    for opt in OPTIONS:
        raw = values.get(opt.name)
        if _is_unset(raw, opt):
            out[opt.name] = None if (opt.keep_none and raw is None and opt.name in values) else opt.default_for(engine)
        else:
            out[opt.name] = raw

    # Legacy rule: when no format was chosen it follows the old reels toggle, unless the engine
    # has a format of its own (the subtitle engine defaults to a single 16:9 render).
    vf = BY_NAME["video_format"]
    if _is_unset(values.get("video_format"), vf) and engine not in vf.engine_defaults:
        out["video_format"] = "both" if out.get("reels_enabled") else "16:9"
    return out


def unknown_choices(engine: Optional[str], resolved: Mapping[str, Any]) -> List[str]:
    """Human-readable warnings for values outside an option's documented choices."""
    warnings = []
    for opt in options_for(engine):
        value = resolved.get(opt.name)
        if opt.choices and value is not None and value not in opt.choices:
            warnings.append(f"{opt.name}={value!r} is not one of {', '.join(opt.choices)}")
    return warnings


def names(options: Iterable[Option]) -> List[str]:
    return [o.name for o in options]
