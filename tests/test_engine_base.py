"""
Tests for the plumbing the subtitle studios (Engine 2 and Engine 3) now share:
core/engine_base.py, core/subtitle_parse.py and core/transcribe.py.

What must stay engine-specific (prompts, how the model's answer is read, how a repaired line is
cleaned) is asserted here too, so the shared skeleton cannot silently flatten the differences.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import hardsub_engine
import subtitle_engine
from core.engine_base import StudioEngineBase
from core.subtitle_parse import parse_subtitle_cues
from core.transcribe import transcribe_media


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hard = hardsub_engine.HardsubEngine(output_base_dir=self.tmp)
        self.sub = subtitle_engine.SubtitleEngine(output_base_dir=self.tmp)


class TestSharedBase(_Tmp):
    def test_both_engines_share_one_base(self):
        self.assertIsInstance(self.hard, StudioEngineBase)
        self.assertIsInstance(self.sub, StudioEngineBase)
        for name in ("_audit_and_repair_untranslated", "_probe_video_metadata", "_get_api_keys",
                     "_check_cancellation", "is_url"):
            for cls in (hardsub_engine.HardsubEngine, subtitle_engine.SubtitleEngine):
                self.assertNotIn(name, vars(cls), f"{cls.__name__} re-defines shared {name}")

    def test_engine_specific_identity_is_kept(self):
        self.assertEqual((self.hard.segment_id_key, self.sub.segment_id_key), ("id", "no"))
        self.assertEqual((self.hard.default_translation_style, self.sub.default_translation_style),
                         ("persona", "dialogue"))
        self.assertEqual((self.hard.line_label, self.sub.line_label), ("dialogue", "subtitle"))

    def test_is_url_accepts_scheme_less_youtube_links_in_both(self):
        for engine in (self.hard, self.sub):
            for url in ("https://youtu.be/x", "http://a.b/c", "www.youtube.com/watch?v=1", "youtu.be/abc"):
                self.assertTrue(engine.is_url(url), url)
            for path in ("movies/a.mp4", "C:\\v\\a.mp4"):
                self.assertFalse(engine.is_url(path), path)

    def test_api_keys_merge_env_without_mutating_config(self):
        for engine in (self.hard, self.sub):
            engine.config_data = {"gemini": {"api_keys": ["cfg"]}}
            with patch.dict(os.environ, {"GEMINI_API_KEYS": "a, b;cfg\nc", "GEMINI_API_KEY": ""}):
                self.assertEqual(engine._get_api_keys(), ["cfg", "a", "b", "c"])
            self.assertEqual(engine.config_data["gemini"]["api_keys"], ["cfg"])

    def test_cancellation_message_per_engine(self):
        event = threading.Event()
        event.set()
        hard = hardsub_engine.HardsubEngine(output_base_dir=self.tmp, cancel_event=event)
        sub = subtitle_engine.SubtitleEngine(output_base_dir=self.tmp, cancel_event=event)
        with self.assertRaises(InterruptedError) as h:
            hard._check_cancellation()
        with self.assertRaises(InterruptedError) as s:
            sub._check_cancellation()
        self.assertIn("HardsubEngine", str(h.exception))
        self.assertIn("Subtitle generation", str(s.exception))

    def test_probe_reports_audio_presence(self):
        class R:
            stderr = "  Duration: 00:01:30.50\n  Stream #0:0: Video: h264, 720x1280\n  Stream #0:1: Audio: aac\n"

        class NoAudio:
            stderr = "  Duration: 00:00:10.00\n  Stream #0:0: Video: h264, 1920x1080\n"

        for engine in (self.hard, self.sub):
            with patch("subprocess.run", return_value=R):
                meta = engine._probe_video_metadata("x.mp4")
            self.assertEqual((meta["duration"], meta["width"], meta["height"], meta["has_audio"]),
                             (90.5, 720, 1280, True))
            with patch("subprocess.run", return_value=NoAudio):
                self.assertFalse(engine._probe_video_metadata("x.mp4")["has_audio"])


class TestRepairKeepsEngineDifferences(_Tmp):
    def _repair(self, engine, module, segments, answer, **kwargs):
        calls = []

        def fake(**kw):
            calls.append(kw)
            return answer, "stub"

        with patch.object(module, "call_gemini", side_effect=fake), \
                patch("time.sleep", lambda s: None), \
                patch.dict(os.environ, {"GEMINI_API_KEYS": "k"}):
            result = engine._audit_and_repair_untranslated(segments, **kwargs)
        return result, calls

    def test_hardsub_asks_for_id_keyed_objects_and_reads_them_by_id(self):
        segs = [{"id": 7, "original": "快跑", "burmese": "快跑"}, {"id": 9, "original": "走", "burmese": ""}]
        answer = json.dumps([{"id": 9, "burmese": "ကိုးမြောက်"}, {"id": 7, "burmese": "ခုနစ်မြောက်"}], ensure_ascii=False)
        result, calls = self._repair(self.hard, hardsub_engine, segs, answer)
        self.assertEqual([s["burmese"] for s in result], ["ခုနစ်မြောက်", "ကိုးမြောက်"])
        self.assertIn("array of objects", calls[0]["user_prompt"])
        self.assertEqual(self.hard.last_repaired_count, 2)

    def test_subtitle_asks_for_plain_list_and_reads_by_position(self):
        segs = [{"no": 1, "original": "快跑", "burmese": "快跑"}, {"no": 2, "original": "走", "burmese": ""}]
        answer = json.dumps(["ပထမ", "ဒုတိယ"], ensure_ascii=False)
        result, calls = self._repair(self.sub, subtitle_engine, segs, answer)
        self.assertEqual([s["burmese"] for s in result], ["ပထမ", "ဒုတိယ"])
        self.assertIn("array of strings containing EXACTLY 2", calls[0]["user_prompt"])

    def test_wuxia_style_selects_a_different_prompt(self):
        segs = [{"id": 1, "original": "走", "burmese": ""}]
        _, plain = self._repair(self.hard, hardsub_engine, [dict(segs[0])], "[]", translation_style="persona")
        _, wuxia = self._repair(self.hard, hardsub_engine, [dict(segs[0])], "[]", translation_style="wuxia")
        self.assertNotEqual(plain[0]["system_prompt"], wuxia[0]["system_prompt"])

    def test_context_hint_reaches_the_prompt(self):
        segs = [{"id": 1, "original": "走", "burmese": ""}]
        _, calls = self._repair(self.hard, hardsub_engine, segs, "[]", context_hint="hero is Li Wei")
        self.assertIn("hero is Li Wei", calls[0]["user_prompt"])

    def test_foreign_script_in_the_answer_falls_back_to_cleansing_not_the_raw_answer(self):
        segs = [{"id": 1, "original": "走", "burmese": ""}]
        result, _ = self._repair(self.hard, hardsub_engine, segs, json.dumps(["走 again"]))
        self.assertTrue(result[0]["burmese"].strip())

    def test_no_keys_never_calls_the_model_and_still_fills_every_line(self):
        segs = [{"id": 1, "original": "走", "burmese": ""}, {"id": 2, "original": "hi", "burmese": "မင်္ဂလာပါ"}]
        self.hard.config_data = {"gemini": {"api_keys": []}}
        with patch.object(hardsub_engine, "call_gemini", side_effect=AssertionError("model called")), \
                patch.dict(os.environ, {"GEMINI_API_KEYS": "", "GEMINI_API_KEY": ""}):
            result = self.hard._audit_and_repair_untranslated(segs)
        self.assertEqual(self.hard.last_damaged_count, 1)
        self.assertTrue(all(s["burmese"].strip() for s in result))
        self.assertEqual(result[1]["burmese"], "မင်္ဂလာပါ")  # the undamaged line is left alone

    def test_clean_input_is_not_sent_to_the_model(self):
        segs = [{"id": 1, "original": "hi", "burmese": "မင်္ဂလာပါ။"}]
        result, calls = self._repair(self.hard, hardsub_engine, segs, "[]")
        self.assertEqual(calls, [])
        self.assertEqual(self.hard.last_damaged_count, 0)
        self.assertFalse(result[0]["burmese"].endswith("။"))  # trailing punctuation still stripped


class TestDamagedLineDetection(_Tmp):
    """The repair pass must catch lines that were never translated, not only foreign-script leftovers."""

    OK = "ကျနော် သိပါပြီ"

    def damaged(self, *pairs):
        segs = [{"id": i + 1, "original": o, "burmese": b} for i, (o, b) in enumerate(pairs)]
        return StudioEngineBase._find_damaged(segs)

    def test_english_fallback_line_is_damaged(self):
        # A failed batch copies the source text in: it contains no Burmese, so the old CJK-only check missed it.
        self.assertEqual(self.damaged(("Thank you very much.", "Thank you very much."), ("Hello", self.OK)), [0])

    def test_empty_and_foreign_script_lines_are_still_damaged(self):
        self.assertEqual(self.damaged(("a b c", ""), ("快跑", "快跑"), ("hi there", self.OK)), [0, 1])

    def test_loose_english_words_inside_a_burmese_line(self):
        self.assertEqual(self.damaged(("x", self.OK + " Pai Studio Movie")), [0])

    def test_acronyms_and_a_single_brand_word_are_accepted(self):
        self.assertEqual(self.damaged(("It is on CCTV", self.OK + " CCTV"), ("Use WiFi", self.OK + " iPhone")), [])

    def test_sound_only_lines_are_left_alone(self):
        self.assertEqual(self.damaged(("♪ ♪", "♪ ♪"), ("...", "..."), ("hi there", self.OK)), [])

    def test_lines_whose_source_is_already_burmese_are_not_repaired(self):
        self.assertEqual(self.damaged(("မင်္ဂလာပါ", "မင်္ဂလာပါ")), [])

    def test_model_chatter_is_damaged(self):
        self.assertEqual(self.damaged(("x y z", "Here is the translation: " + self.OK)), [0])

    def test_repair_pass_now_fixes_an_english_fallback(self):
        segs = [
            {"id": 1, "original": "Thank you very much.", "burmese": "Thank you very much."},
            {"id": 2, "original": "Hello", "burmese": self.OK},
        ]
        answer = json.dumps([{"id": 1, "burmese": "ကျေးဇူးအများကြီးတင်ပါတယ်"}], ensure_ascii=False)
        with patch.object(hardsub_engine, "call_gemini", return_value=(answer, "stub")) as call, \
                patch("time.sleep", lambda s: None), patch.dict(os.environ, {"GEMINI_API_KEYS": "k"}):
            result = self.hard._audit_and_repair_untranslated(segs)
        self.assertEqual(self.hard.last_damaged_count, 1)
        self.assertEqual(self.hard.last_repaired_count, 1)
        self.assertEqual(result[0]["burmese"], "ကျေးဇူးအများကြီးတင်ပါတယ်")
        self.assertEqual(result[1]["burmese"], self.OK)
        self.assertEqual(call.call_count, 1)


class TestStoryBlock(_Tmp):
    LINES = [f"This is a spoken line number {i}, long enough to count." for i in range(30)]
    BIBLE = json.dumps({"summary": "S", "characters": [{"name": "John", "target_name": "ဂျွန်", "gender": "male"}]})

    def _block(self, engine, module, lines=None, keys="k"):
        calls = []

        def fake(**kw):
            calls.append(kw)
            return self.BIBLE, "stub"

        with patch.object(module, "call_gemini", side_effect=fake), patch.dict(os.environ, {"GEMINI_API_KEYS": keys}):
            return engine._story_block(lines or self.LINES, "hero is John", "My Film"), calls

    def test_block_is_built_with_the_engines_own_model_binding(self):
        for engine, module in ((self.hard, hardsub_engine), (self.sub, subtitle_engine)):
            with self.subTest(engine=type(engine).__name__):
                block, calls = self._block(engine, module)
                self.assertTrue(block.startswith("STORY BIBLE"))
                self.assertTrue(block.endswith("\n\n"))
                self.assertIn("ဂျွန်", block)
                self.assertEqual(len(calls), 1)
                self.assertIn("hero is John", calls[0]["user_prompt"])     # the user's guidance is passed on
                self.assertEqual(calls[0]["response_mime_type"], "application/json")

    def test_short_clips_and_missing_keys_cost_nothing(self):
        block, calls = self._block(self.hard, hardsub_engine, lines=self.LINES[:5])
        self.assertEqual((block, calls), ("", []))
        self.hard.config_data = {"gemini": {"api_keys": []}}
        block, calls = self._block(self.hard, hardsub_engine, keys="")
        self.assertEqual((block, calls), ("", []))

    def test_failure_is_silent(self):
        with patch.object(hardsub_engine, "call_gemini", side_effect=RuntimeError("quota")), \
                patch.dict(os.environ, {"GEMINI_API_KEYS": "k"}):
            self.assertEqual(self.hard._story_block(self.LINES, None, ""), "")


class TestSubtitleParse(unittest.TestCase):
    def test_srt_with_markup_and_multiline_text(self):
        cues = parse_subtitle_cues("1\n00:00:01,000 --> 00:00:03,500\nHello <i>there</i>\nsecond\n\n"
                                   "2\n00:00:04,000 --> 00:00:06,000\nBye\n")
        self.assertEqual(cues, [(1.0, 3.5, "Hello there second"), (4.0, 6.0, "Bye")])

    def test_vtt_header_and_cue_settings(self):
        cues = parse_subtitle_cues("WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:03.000 align:start\n"
                                   "<c.x>Hello</c> world\n")
        self.assertEqual(cues, [(1.0, 3.0, "Hello world")])

    def test_ass_style_tags_are_removed(self):
        cues = parse_subtitle_cues("1\n00:00:01,000 --> 00:00:02,000\n{\\an8}Top\n")
        self.assertEqual(cues[0][2], "Top")

    def test_empty_cues_and_headers_are_dropped(self):
        self.assertEqual(parse_subtitle_cues("1\n00:00:01,000 --> 00:00:02,000\n<i></i>\n"), [])
        self.assertEqual(parse_subtitle_cues("just text\nno timestamps"), [])

    def test_zero_length_cues_are_returned_for_the_caller_to_judge(self):
        cues = parse_subtitle_cues("1\n00:00:05,000 --> 00:00:05,000\nx\n")
        self.assertEqual(cues, [(5.0, 5.0, "x")])

    def test_cue_numbers_are_not_part_of_the_text(self):
        cues = parse_subtitle_cues("12\n00:00:01,000 --> 00:00:02,000\nHi\n\n13\n00:00:03,000 --> 00:00:04,000\nYo\n")
        self.assertEqual([c[2] for c in cues], ["Hi", "Yo"])


class _Word:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Seg:
    def __init__(self, start, end, text, words=None):
        self.start, self.end, self.text, self.words = start, end, text, words


class _Model:
    def __init__(self, segments):
        self.segments, self.calls = segments, []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return iter(self.segments), object()


class TestTranscribeMedia(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.chdir, self.cwd)

    def _run(self, model, *, returncode=0, wav_bytes=2000, **kwargs):
        def fake_run(cmd, **kw):
            if returncode == 0:
                with open(cmd[-1], "wb") as handle:
                    handle.write(b"0" * wav_bytes)

            class R:
                pass
            R.returncode = returncode
            return R

        with patch("subprocess.run", side_effect=fake_run), \
                patch("core.transcribe.load_faster_whisper", return_value=(model, "base", "cpu")):
            return transcribe_media("ffmpeg", "movie.mp4", "auto", {}, temp_prefix="t", log=lambda m: None, **kwargs)

    def test_returns_tightened_segments_and_drops_junk(self):
        model = _Model([_Seg(0.2, 9.0, " hello world ", [_Word(0.2, 0.6), _Word(0.7, 1.2)]),
                        _Seg(1.0, 1.0, "zero"), _Seg(1.5, 2.0, "  "), _Seg(2.0, 2.6, "tail")])
        result = self._run(model)
        self.assertEqual([r["text"] for r in result], ["hello world", "tail"])
        self.assertAlmostEqual(result[0]["end"], 1.55, places=2)  # last word end + pad, not 9.0
        self.assertLess(result[0]["end"], 9.0)

    def test_whisper_options(self):
        model = _Model([])
        self._run(model, vad_parameters={"min_silence_duration_ms": 400})
        _, kwargs = model.calls[0]
        self.assertTrue(kwargs["word_timestamps"])
        self.assertTrue(kwargs["vad_filter"])
        self.assertIsNone(kwargs["language"])
        self.assertEqual(kwargs["vad_parameters"], {"min_silence_duration_ms": 400})

    def test_temp_wav_is_removed(self):
        self._run(_Model([_Seg(0, 1, "x")]))
        self.assertEqual(os.listdir("temp"), [])

    def test_failed_extraction_returns_nothing_by_default(self):
        model = _Model([_Seg(0, 1, "x")])
        self.assertEqual(self._run(model, returncode=1), [])
        self.assertEqual(model.calls, [])

    def test_failed_extraction_can_fall_back_to_the_media_file(self):
        model = _Model([_Seg(0, 1, "x")])
        result = self._run(model, returncode=1, use_media_when_extraction_fails=True)
        self.assertEqual([r["text"] for r in result], ["x"])
        self.assertEqual(model.calls[0][0], "movie.mp4")


if __name__ == "__main__":
    unittest.main()
