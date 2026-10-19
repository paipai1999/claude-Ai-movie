"""
Command-line options generated from the option model
====================================================
``main.py``, ``hardsub_engine.py`` and ``subtitle_engine.py`` used to declare the same options by
hand, with different spellings and conflicting meanings (``--style`` was the subtitle look in two
programs and the translation voice in the third; ``--lang`` was the source language in one and the
target language in another).

Now:

* every option has ONE canonical flag set (below), identical in every program;
* each program keeps the spellings it always had as *legacy aliases*, so no existing command or
  ``.bat`` file breaks;
* a flag that was not typed stays ``None`` - the default for the chosen engine is applied in one
  place, ``core.options.resolve``.

Pure argparse + ``core.options``; no engine imports.
"""

import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from core import options as O

MAIN = "main"          # main.py - all three engines through --engine-mode
HARDSUB = "hardsub"    # hardsub_engine.py
SUBTITLE = "subtitle"  # subtitle_engine.py
PROGRAMS = (MAIN, HARDSUB, SUBTITLE)


@dataclass(frozen=True)
class Flag:
    on: Tuple[str, ...]                                # canonical flags (all programs)
    off: Tuple[str, ...] = ()                          # flags that switch a boolean off
    legacy: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)      # program -> extra flags
    legacy_off: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)  # program -> extra "off" flags
    programs: Tuple[str, ...] = PROGRAMS               # programs that offer it
    metavar: Optional[str] = None


F = Flag
FLAGS: Dict[str, Flag] = {
    "engine_mode": F(("--engine-mode",), programs=(MAIN,)),
    "project_name": F(("--name",), legacy={SUBTITLE: ("-n",)}),
    "force_whisper": F(("--force-whisper",)),
    "resolution": F(("--res", "--resolution")),

    "source_language": F(("--source-lang", "--source-language"), legacy={HARDSUB: ("--lang",)}),
    "language": F(("-l", "--lang"), programs=(MAIN,)),
    "translation_style": F(("--translation-style",), legacy={MAIN: ("--style",)}),
    "script_engine": F(("--mode", "--script-engine"), programs=(MAIN,)),
    "context_hint": F(("--context-hint", "--hint")),
    "story_length": F(("--story-length",), programs=(MAIN,)),
    "narrative_tone": F(("--narrative-tone", "--tone"), programs=(MAIN,)),

    "video_format": F(("--format", "--aspect-ratio"), legacy={MAIN: ("--video-size",)}),
    "reels_enabled": F(("--reels",), off=("--no-reels",), programs=(MAIN,)),
    "subtitle_style": F(("--subtitle-style", "--sub-style"),
                        legacy={MAIN: ("--preset",), HARDSUB: ("--style",), SUBTITLE: ("--style",)}),
    "subtitle_mode": F(("--sub-mode", "--subtitle-mode"), programs=(MAIN,)),
    "render_video": F(("--render-video",), off=("--no-render", "--no-render-video", "--subtitles-only")),
    "custom_thumb_title": F(("--thumb-title",), programs=(MAIN,)),
    "thumbnail_intro": F(("--thumbnail-intro",), off=("--no-thumbnail-intro",), programs=(MAIN,)),
    "trim_end": F(("--trim-end",), programs=(MAIN,)),
    "no_smart_trim": F(("--no-smart-trim",), programs=(MAIN,)),
    "outro_card": F(("--outro-card",), off=("--no-outro-card",), programs=(MAIN,)),
    "watermark_enabled": F(("--watermark",), off=("--no-watermark",), programs=(MAIN,)),
    "watermark_text": F(("--watermark-text",), programs=(MAIN,)),
    "watermark_opacity": F(("--watermark-opacity",), programs=(MAIN,)),

    "blur_mode": F(("--blur-mode",), legacy={HARDSUB: ("--blur",)}),
    "blur_height": F(("--blur-height",)),
    "mirror": F(("--mirror",)),
    "color_grading": F(("--color-grading",), off=("--no-color-grading",)),
    "audio_anti_copyright": F(("--audio-anti-copyright", "--audio-shield")),

    "tts_engine": F(("--tts-engine",), legacy={MAIN: ("-e", "--engine")}, programs=(MAIN,)),
    "tts_voice": F(("--tts-voice",), legacy={MAIN: ("-v", "--voice")}, programs=(MAIN,)),
    "tts_rate": F(("--tts-rate",), programs=(MAIN,)),
    "audio_mode": F(("--audio-mode",)),
    "sfx_mode": F(("--sfx-mode",)),
    "sfx_volume": F(("--sfx-volume",)),
    "skip_demucs": F(("--skip-demucs", "--no-demucs"), programs=(MAIN,)),
    "detect_scenes": F(("--detect-scenes",), off=("--skip-scenes",), programs=(MAIN,)),
    "resume": F(("--resume",), off=("--fresh", "--force-restart"), programs=(MAIN,)),
}

# Options with no command-line flag (structured values, or "input" which is positional).
NO_FLAG = frozenset({"input", "stage_toggles"})

# When the recap engine is run from main.py an unset flag means "let config.json decide", NOT
# "use the API default". These are passed through as None.
RECAP_CONFIG_DRIVEN = frozenset({
    "subtitle_style", "video_format", "thumbnail_intro", "outro_card", "watermark_enabled",
    "watermark_text", "custom_thumb_title", "tts_voice", "tts_engine", "translation_style",
    "detect_scenes", "trim_end", "blur_height", "context_hint", "language", "watermark_opacity",
})


def _engine_for(program: str, engine: Optional[str]) -> str:
    if program == HARDSUB:
        return O.HARDSUB
    if program == SUBTITLE:
        return O.SUBTITLE
    return O.normalize_engine(engine)


def _offered(program: str, name: str) -> bool:
    opt = O.BY_NAME.get(name)
    flag = FLAGS.get(name)
    if opt is None or flag is None or program not in flag.programs:
        return False
    if program == HARDSUB:
        return opt.applies_to(O.HARDSUB)
    if program == SUBTITLE:
        return opt.applies_to(O.SUBTITLE)
    return True  # main.py runs every engine


def _help_text(program: str, opt: O.Option) -> str:
    text = opt.help or opt.name
    if program == MAIN:
        engines = [e for e in O.ENGINES if e in opt.engines and e != O.DOWNLOAD]
        if len(engines) < 3:
            text += f" [{'/'.join(engines)} only]"
        if opt.engine_defaults:
            shown = ", ".join(f"{e}: {v}" for e, v in opt.engine_defaults.items())
            text += f" (default per engine - {shown})"
        elif opt.default is not None:
            text += f" (default: {opt.default})"
    else:
        default = opt.default_for(_engine_for(program, None))
        if default is not None:
            text += f" (default: {default})"
    return text.replace("%", "%%")


def add_option_arguments(parser: argparse.ArgumentParser, program: str) -> None:
    """Adds every option the ``program`` offers. Unset flags are ``None`` (resolved later)."""
    if program not in PROGRAMS:
        raise ValueError(f"unknown program {program!r}")

    for name, flag in FLAGS.items():
        if not _offered(program, name):
            continue
        opt = O.BY_NAME[name]
        on = flag.on + tuple(flag.legacy.get(program, ()))
        off = flag.off + tuple(flag.legacy_off.get(program, ()))
        help_text = _help_text(program, opt)

        if opt.type is bool:
            parser.add_argument(*on, dest=name, action="store_const", const=True,
                                default=None, help=help_text)
            if off:
                parser.add_argument(*off, dest=name, action="store_const", const=False,
                                    default=None, help=f"Turn off: {opt.help or name}".replace("%", "%%"))
            continue

        kwargs: Dict[str, Any] = {"dest": name, "default": None, "help": help_text}
        if opt.type in (float, int):
            kwargs["type"] = opt.type
        if opt.choices:
            kwargs["choices"] = list(opt.choices)
        if flag.metavar:
            kwargs["metavar"] = flag.metavar
        parser.add_argument(*on, **kwargs)


def option_values(args: argparse.Namespace) -> Dict[str, Any]:
    """
    The option values the user actually typed. Flags that were not typed are omitted (rather than
    ``None``) so ``resolve`` treats them as "not chosen" - an explicit ``null`` only exists in API
    requests, where it deliberately means "let the config decide".
    """
    values = {}
    for name in O.BY_NAME:
        value = getattr(args, name, None)
        if value is not None:
            values[name] = value
    return values


def resolve_args(program: str, args: argparse.Namespace, engine: Optional[str] = None) -> Dict[str, Any]:
    """
    Final value of every option for the engine that will run.

    For ``main.py --engine-mode recap`` options that ``config.json`` owns stay ``None`` when they
    were not typed (see ``RECAP_CONFIG_DRIVEN``).
    """
    values = option_values(args)
    engine = _engine_for(program, engine if engine is not None else values.get("engine_mode"))
    resolved = O.resolve(engine, values)
    if program == MAIN and engine == O.RECAP:
        for name in RECAP_CONFIG_DRIVEN:
            if values.get(name) is None:
                resolved[name] = None
    return resolved
