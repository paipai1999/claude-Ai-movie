"""
Tests for the generated command line (core/cli.py) used by main.py, hardsub_engine.py and
subtitle_engine.py.

Guarantees:
* every flag spelling the three programs accepted before still parses;
* a legacy spelling means the same thing as before in the program that owned it
  (``--style`` = subtitle look in the engine scripts, translation voice in main.py ...);
* unset flags are resolved in one place, per engine;
* ``config.json`` keeps ownership of the recap options it always owned.
"""

import argparse
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import cli
from core import options as O

# ── Flag spellings accepted before the option model existed (frozen on purpose) ──────────────
LEGACY_MAIN = {
    "--aspect-ratio", "--audio-anti-copyright", "--audio-mode", "--batch", "--blocks", "--blur-height",
    "--blur-mode", "--clean", "--context-hint", "--detect-scenes", "--download", "--download-urls",
    "--download-video", "--engine", "--engine-mode", "--force", "--force-restart", "--format", "--fresh",
    "--hint", "--input", "--lang", "--mirror", "--mode", "--no-demucs", "--no-outro-card", "--no-reels",
    "--no-render", "--no-smart-trim", "--no-thumb-title", "--no-thumbnail-intro", "--no-voice",
    "--no-watermark", "--outro-card", "--preset", "--reels", "--res", "--resolution", "--resume",
    "--script-engine", "--sfx-mode", "--sfx-volume", "--skip-demucs", "--skip-scenes", "--source-lang",
    "--source-language", "--style", "--sub-mode", "--sub-style", "--subtitle", "--subtitle-mode",
    "--subtitle-style", "--subtitles-only", "--thumb-title", "--thumbnail-intro", "--translation-style",
    "--trim-end", "--urls", "--video-size", "--voice", "--watermark-text", "-b", "-e", "-i", "-l", "-u", "-v",
}
LEGACY_HARDSUB = {
    "--audio-anti-copyright", "--audio-shield", "--blur", "--blur-height", "--color-grading",
    "--context-hint", "--cookies", "--force-whisper", "--format", "--hint", "--lang", "--mirror", "--name",
    "--no-color-grading", "--no-render", "--render-video", "--res", "--source-lang", "--style",
    "--translation-style",
}
LEGACY_SUBTITLE = {
    "--aspect-ratio", "--audio-anti-copyright", "--audio-shield", "--blur", "--blur-height", "--blur-mode",
    "--context-hint", "--cookies", "--force-whisper", "--format", "--hint", "--input", "--mirror", "--name",
    "--no-blur", "--no-render", "--output-dir", "--render-video", "--res", "--source-lang", "--style",
    "--translation-style", "-i", "-n", "-o",
}

# Flags that are run switches of one program, not studio options (declared by the program itself).
MAIN_ONLY = {"--batch", "--blocks", "--clean", "--download", "--download-urls", "--download-video",
             "--force", "--input", "--no-thumb-title", "--no-voice", "--subtitle", "--urls", "-b", "-i", "-u"}
HARDSUB_ONLY = {"--cookies"}
SUBTITLE_ONLY = {"--cookies", "--blur", "--no-blur", "--input", "--output-dir", "-i", "-o"}


def option_parser(program):
    parser = argparse.ArgumentParser(prog=program)
    cli.add_option_arguments(parser, program)
    return parser


def flags_of(parser):
    return {o for a in parser._actions for o in a.option_strings if o not in ("-h", "--help")}


def parse(program, *argv):
    return option_parser(program).parse_args(list(argv))


class TestLegacySpellingsStillWork(unittest.TestCase):
    def test_every_studio_flag_of_main_is_generated(self):
        missing = (LEGACY_MAIN - MAIN_ONLY) - flags_of(option_parser(cli.MAIN))
        self.assertFalse(missing, f"main.py lost flags: {sorted(missing)}")

    def test_every_studio_flag_of_hardsub_is_generated(self):
        missing = (LEGACY_HARDSUB - HARDSUB_ONLY) - flags_of(option_parser(cli.HARDSUB))
        self.assertFalse(missing, f"hardsub_engine.py lost flags: {sorted(missing)}")

    def test_every_studio_flag_of_subtitle_is_generated(self):
        # --blur / --no-blur are declared by subtitle_engine.py itself (boolean shortcut).
        missing = (LEGACY_SUBTITLE - SUBTITLE_ONLY) - flags_of(option_parser(cli.SUBTITLE))
        self.assertFalse(missing, f"subtitle_engine.py lost flags: {sorted(missing)}")

    def test_programs_build_without_flag_conflicts(self):
        for program in cli.PROGRAMS:
            option_parser(program)  # argparse raises on a duplicate option string

    def test_main_builds_with_its_run_switches(self):
        import main
        parser = main.build_parser()
        self.assertTrue(LEGACY_MAIN <= flags_of(parser) | {"-h"}, sorted(LEGACY_MAIN - flags_of(parser)))


class TestSameFlagSameMeaningPerProgram(unittest.TestCase):
    """The conflicting spellings keep the meaning each program always gave them."""

    def test_style_is_subtitle_look_in_engine_scripts(self):
        for program in (cli.HARDSUB, cli.SUBTITLE):
            self.assertEqual(parse(program, "--style", "yellow_pop").subtitle_style, "yellow_pop")

    def test_style_is_translation_voice_in_main(self):
        args = parse(cli.MAIN, "--style", "wuxia")
        self.assertEqual(args.translation_style, "wuxia")
        self.assertIsNone(args.subtitle_style)

    def test_lang_is_source_language_in_hardsub_and_target_in_main(self):
        self.assertEqual(parse(cli.HARDSUB, "--lang", "zh").source_language, "zh")
        main_args = parse(cli.MAIN, "--lang", "english")
        self.assertEqual(main_args.language, "english")
        self.assertIsNone(main_args.source_language)

    def test_blur_means_mode_in_hardsub(self):
        self.assertEqual(parse(cli.HARDSUB, "--blur", "no").blur_mode, "no")

    def test_canonical_flags_are_identical_everywhere(self):
        for program in cli.PROGRAMS:
            a = parse(program, "--subtitle-style", "cyan_cyber", "--source-lang", "ko", "--blur-mode", "yes",
                      "--format", "9:16", "--res", "720p")
            self.assertEqual((a.subtitle_style, a.source_language, a.blur_mode, a.video_format, a.resolution),
                             ("cyan_cyber", "ko", "yes", "9:16", "720p"), program)

    def test_aliases_land_on_the_same_option(self):
        self.assertEqual(parse(cli.MAIN, "--preset", "white_stroke").subtitle_style, "white_stroke")
        self.assertEqual(parse(cli.MAIN, "--video-size", "16:9").video_format, "16:9")
        self.assertEqual(parse(cli.MAIN, "--voice", "thiha").tts_voice, "thiha")
        self.assertEqual(parse(cli.MAIN, "-e", "f5_tts").tts_engine, "f5_tts")
        self.assertEqual(parse(cli.SUBTITLE, "-n", "Proj").project_name, "Proj")
        self.assertTrue(parse(cli.HARDSUB, "--audio-shield").audio_anti_copyright)

    def test_negative_flags(self):
        self.assertIs(parse(cli.HARDSUB, "--no-render").render_video, False)
        self.assertIs(parse(cli.SUBTITLE, "--no-render-video").render_video, False)
        self.assertIs(parse(cli.MAIN, "--subtitles-only").render_video, False)
        self.assertIs(parse(cli.HARDSUB, "--no-color-grading").color_grading, False)
        self.assertIs(parse(cli.MAIN, "--fresh").resume, False)
        self.assertIs(parse(cli.MAIN, "--no-reels").reels_enabled, False)
        self.assertIs(parse(cli.MAIN, "--skip-scenes").detect_scenes, False)
        self.assertIs(parse(cli.MAIN, "--no-watermark").watermark_enabled, False)

    def test_untyped_flags_stay_none(self):
        args = parse(cli.MAIN)
        for name in ("subtitle_style", "video_format", "render_video", "audio_mode", "blur_mode"):
            self.assertIsNone(getattr(args, name), name)

    def test_choices_come_from_the_model(self):
        for bad in (["--format", "4:3"], ["--blur-mode", "maybe"], ["--res", "480p"]):
            with self.assertRaises(SystemExit):
                parse(cli.HARDSUB, *bad)


class TestDefaultsResolvedOncePerEngine(unittest.TestCase):
    def _resolve(self, program, *argv, engine=None):
        return cli.resolve_args(program, parse(program, *argv), engine)

    def test_hardsub_script_defaults(self):
        r = self._resolve(cli.HARDSUB)
        self.assertEqual((r["video_format"], r["resolution"], r["subtitle_style"], r["blur_mode"],
                          r["translation_style"], r["render_video"], r["color_grading"], r["source_language"]),
                         ("both", "1080p", "box_black", "auto", "persona", True, True, "auto"))

    def test_subtitle_script_defaults(self):
        r = self._resolve(cli.SUBTITLE)
        self.assertEqual((r["video_format"], r["translation_style"], r["render_video"], r["blur_mode"]),
                         ("16:9", "dialogue", False, "auto"))

    def test_main_uses_the_engine_selected_with_engine_mode(self):
        self.assertEqual(self._resolve(cli.MAIN, "--engine-mode", "hardsub")["translation_style"], "persona")
        self.assertEqual(self._resolve(cli.MAIN, "--engine-mode", "subtitle")["translation_style"], "dialogue")
        self.assertEqual(self._resolve(cli.MAIN, "--engine-mode", "subtitle")["video_format"], "16:9")
        self.assertEqual(self._resolve(cli.MAIN, "--engine-mode", "hardsub")["video_format"], "both")
        self.assertEqual(self._resolve(cli.MAIN, "--engine-mode", "hardsub")["audio_mode"], "original")
        self.assertEqual(self._resolve(cli.MAIN, "--engine-mode", "recap")["audio_mode"], "ai_voiceover")

    def test_explicit_flag_always_wins(self):
        r = self._resolve(cli.MAIN, "--engine-mode", "hardsub", "--translation-style", "wuxia",
                          "--audio-mode", "ai_voiceover", "--format", "9:16")
        self.assertEqual((r["translation_style"], r["audio_mode"], r["video_format"]),
                         ("wuxia", "ai_voiceover", "9:16"))

    def test_recap_options_owned_by_config_json_stay_none(self):
        r = self._resolve(cli.MAIN, "--engine-mode", "recap")
        for name in cli.RECAP_CONFIG_DRIVEN:
            self.assertIsNone(r[name], f"{name} must stay None so config.json decides")
        # ... while options main.py always defaulted are filled in.
        self.assertEqual((r["resolution"], r["source_language"], r["blur_mode"], r["sfx_mode"], r["resume"]),
                         ("1080p", "auto", "auto", "original_sfx", True))

    def test_recap_config_driven_value_is_used_when_typed(self):
        r = self._resolve(cli.MAIN, "--sub-style", "yellow_pop", "--trim-end", "4.5")
        self.assertEqual((r["subtitle_style"], r["trim_end"]), ("yellow_pop", 4.5))


class TestMainRunOptions(unittest.TestCase):
    def _opts(self, *argv):
        import main
        saved = {k: os.environ.get(k) for k in ("DISABLE_REELS", "ENABLE_REELS", "ENABLE_SUBTITLES",
                                                 "SKIP_DEMUCS", "SKIP_SCENES", "VOICE_ENABLED", "MAX_BLOCKS")}
        try:
            args = main.build_parser().parse_args(list(argv))
            return args, main.resolve_run_options(args), main
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_reels_forces_landscape(self):
        _, opts, _ = self._opts("--no-reels", "--format", "9:16")
        self.assertEqual(opts.chosen_format, "16:9")

    def test_reels_defaults_to_both(self):
        _, opts, _ = self._opts("--reels")
        self.assertEqual(opts.chosen_format, "both")

    def test_nilar_shortcut_sets_voice(self):
        _, opts, _ = self._opts("-l", "nilar")
        self.assertEqual((opts.clean_lang, opts.chosen_voice), ("burmese", "my-MM-NilarNeural"))

    def test_no_thumb_title_means_none_string(self):
        _, opts, _ = self._opts("--no-thumb-title")
        self.assertEqual(opts.thumb_title, "none")

    def test_subtitle_switch_forces_blur_and_burn_unless_overridden(self):
        _, opts, _ = self._opts("--subtitle")
        self.assertEqual((opts.values["blur_mode"], opts.values["subtitle_mode"]), ("yes", "burn"))
        _, opts, _ = self._opts("--subtitle", "--blur-mode", "no")
        self.assertEqual(opts.values["blur_mode"], "no")

    def test_run_kwargs_match_what_the_engines_accept(self):
        import inspect
        from hardsub_engine import HardsubEngine
        from subtitle_engine import SubtitleEngine
        from agents.master import MasterAgent
        from brain.planner import BatchProcessor
        for engine, accepted, builder in (
            ("hardsub", set(inspect.signature(HardsubEngine.run).parameters), "hardsub_run_kwargs"),
            ("subtitle", set(inspect.signature(SubtitleEngine.run).parameters), "subtitle_run_kwargs"),
        ):
            args, opts, main = self._opts("--engine-mode", engine)
            kwargs = getattr(main, builder)(args, opts)
            self.assertFalse(set(kwargs) - accepted, f"{engine}: unknown kwargs {set(kwargs) - accepted}")
        args, opts, main = self._opts("--engine-mode", "recap")
        kwargs = main.recap_common_kwargs(args, opts)
        self.assertFalse(set(kwargs) - set(inspect.signature(MasterAgent.__init__).parameters))
        self.assertFalse(set(kwargs) - set(inspect.signature(BatchProcessor.__init__).parameters))


class TestEngineScriptsParse(unittest.TestCase):
    """The engine scripts' own ``main()`` end to end (engines stubbed)."""

    def test_hardsub_main_passes_resolved_options(self):
        from unittest.mock import patch
        import hardsub_engine
        argv = ["hardsub_engine.py", "in.mp4", "--format", "16:9", "--style", "yellow_pop", "--lang", "zh",
                "--no-render", "--audio-shield", "--blur", "no", "--name", "P"]
        with patch.object(sys, "argv", argv), \
                patch.object(hardsub_engine.HardsubEngine, "run", return_value={"status": "ok"}) as run, \
                patch.object(hardsub_engine.HardsubEngine, "__init__", return_value=None):
            hardsub_engine.main()
        kw = run.call_args.kwargs
        self.assertEqual(run.call_args.kwargs["input_source"], "in.mp4")
        self.assertEqual((kw["video_format"], kw["subtitle_style"], kw["source_language"], kw["render_video"],
                          kw["audio_anti_copyright"], kw["blur_mode"], kw["project_name"]),
                         ("16:9", "yellow_pop", "zh", False, True, "no", "P"))

    def test_subtitle_main_blur_shortcuts(self):
        from unittest.mock import patch
        import subtitle_engine
        for flag, expect_render, expect_blur in (("--blur", True, "auto"), ("--no-blur", False, "no")):
            argv = ["subtitle_engine.py", "-i", "x.mp4", flag]
            with patch.object(sys, "argv", argv), \
                    patch.object(subtitle_engine.SubtitleEngine, "run", return_value={}) as run, \
                    patch.object(subtitle_engine.SubtitleEngine, "__init__", return_value=None):
                subtitle_engine.main()
            kw = run.call_args.kwargs
            self.assertEqual((kw["render_video"], kw["blur_mode"]), (expect_render, expect_blur), flag)
            self.assertEqual((kw["video_format"], kw["translation_style"]), ("16:9", "dialogue"))


class TestEveryOptionIsReachable(unittest.TestCase):
    def test_every_option_has_a_flag_or_is_explicitly_exempt(self):
        undeclared = [o.name for o in O.OPTIONS if o.name not in cli.FLAGS and o.name not in cli.NO_FLAG]
        self.assertFalse(undeclared, f"options without a CLI declaration: {undeclared}")

    def test_flag_table_only_references_known_options(self):
        self.assertFalse(set(cli.FLAGS) - set(O.BY_NAME))

    def test_help_text_renders_for_every_program(self):
        for program in cli.PROGRAMS:
            text = option_parser(program).format_help()
            self.assertIn("--format", text)


if __name__ == "__main__":
    unittest.main()
