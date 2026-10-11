"""
Regression tests for two defects found in the first end-to-end run of Engine 3:

1. A vertical (9:16) source rendered with ``--format 16:9`` was squashed into the landscape frame.
2. A Whisper segment that ran on through silence kept its subtitle on screen for ~20 seconds.
"""

import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.letterbox import plan_canvas_mode
from core.segment_timing import estimate_speech_seconds, tighten_end
from hardsub_engine import HardsubEngine


class TestPlanCanvasMode(unittest.TestCase):
    def test_matching_aspect_is_direct(self):
        self.assertEqual(plan_canvas_mode(1920, 1080, 1280, 720), "direct")
        self.assertEqual(plan_canvas_mode(1280, 720, 1920, 1080), "direct")

    def test_vertical_source_into_landscape_is_fit(self):
        self.assertEqual(plan_canvas_mode(720, 1280, 1280, 720), "fit")

    def test_cinemascope_and_square_sources_are_fit(self):
        self.assertEqual(plan_canvas_mode(2560, 1080, 1920, 1080), "fit")
        self.assertEqual(plan_canvas_mode(1080, 1080, 1920, 1080), "fit")

    def test_small_aspect_deviation_is_tolerated(self):
        # 1916x1080 is "16:9" for all practical purposes.
        self.assertEqual(plan_canvas_mode(1916, 1080, 1920, 1080), "direct")

    def test_unknown_dimensions_keep_previous_behaviour(self):
        self.assertEqual(plan_canvas_mode(0, 0, 1920, 1080), "direct")
        self.assertEqual(plan_canvas_mode(None, None, 1920, 1080), "direct")


class TestRenderCanvasFilter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.engine = HardsubEngine(output_base_dir=self.temp_dir)
        self.video_in = os.path.join(self.temp_dir, "in.mp4")
        self.ass = os.path.join(self.temp_dir, "sub.ass")
        for p in (self.video_in, self.ass):
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _filter_for(self, width, height, aspect="16:9", blur=False):
        meta = {"duration": 10.0, "fps": 30.0, "width": width, "height": height, "has_audio": True}
        with patch.object(self.engine, "_probe_video_metadata", return_value=meta), \
                patch("subprocess.run") as run:
            self.engine._render_hardsub_video(
                video_path=self.video_in,
                output_path=os.path.join(self.temp_dir, "out.mp4"),
                ass_path=self.ass,
                blur_info=(0.82, 0.18, blur),
                aspect_ratio=aspect,
                resolution="720p",
                color_grading=False,
            )
        cmd = run.call_args[0][0]
        return cmd[cmd.index("-filter_complex") + 1]

    def test_landscape_source_uses_plain_scale(self):
        fc = self._filter_for(1920, 1080)
        self.assertIn("scale=1280:720,setsar=1", fc)
        self.assertNotIn("overlay=(W-w)/2", fc)

    def test_vertical_source_is_fitted_over_blurred_background(self):
        fc = self._filter_for(720, 1280)
        self.assertIn("force_original_aspect_ratio=decrease", fc)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", fc)
        self.assertIn("boxblur", fc)
        # The foreground must be limited by BOTH output dimensions, never forced to 1280x720.
        self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease", fc)
        self.assertNotIn("[canvas]scale=1280:720", fc)

    def test_vertical_source_with_subtitle_blur_still_composes(self):
        fc = self._filter_for(720, 1280, blur=True)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", fc)
        self.assertIn("[vout]", fc)

    def test_reels_output_resets_sample_aspect_ratio(self):
        fc = self._filter_for(1920, 1080, aspect="9:16")
        self.assertIn("setsar=1", fc)
        self.assertIn("scale=720:1280:force_original_aspect_ratio=decrease", fc)


class TestSegmentTiming(unittest.TestCase):
    def test_runaway_segment_is_capped_by_text_length(self):
        # 12 CJK characters (~3.6 s of speech) reported as lasting 20 s.
        end = tighten_end(29.43, 49.34, "你们这样跑不掉的我要把你")
        self.assertLess(end - 29.43, 8.0)
        self.assertGreater(end - 29.43, 2.0)

    def test_word_timestamps_trim_trailing_silence(self):
        words = [SimpleNamespace(start=10.0, end=10.4), SimpleNamespace(start=10.5, end=11.2)]
        end = tighten_end(10.0, 19.0, "hello there my friend how are you doing today ok", words)
        self.assertAlmostEqual(end, 11.2 + 0.35, places=2)

    def test_never_extends_a_segment(self):
        words = [SimpleNamespace(start=1.0, end=9.0)]
        self.assertLessEqual(tighten_end(1.0, 3.0, "short", words), 3.0)

    def test_normal_segment_is_untouched(self):
        self.assertAlmostEqual(tighten_end(5.0, 8.5, "This is a normal sentence."), 8.5, places=2)

    def test_dict_words_and_empty_inputs(self):
        self.assertAlmostEqual(
            tighten_end(0.0, 10.0, "hi", [{"start": 0.0, "end": 0.5}]), 0.85, places=2
        )
        self.assertEqual(estimate_speech_seconds(""), 0.0)
        self.assertEqual(tighten_end(5.0, 5.0, "x"), 5.0)

    def test_minimum_duration_is_kept(self):
        words = [SimpleNamespace(start=2.0, end=2.01)]
        self.assertGreaterEqual(tighten_end(2.0, 6.0, "uh", words) - 2.0, 0.3)


if __name__ == "__main__":
    unittest.main()
