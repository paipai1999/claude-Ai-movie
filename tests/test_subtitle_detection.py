"""
Regression tests for burnt-in subtitle detection.

Run 1 of the first end-to-end test blurred the source subtitle (1 of 5 frames positive was
enough); run 2 on the very same video did not. The decision is now a vote over frames taken
where people actually speak.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.subtitle_detect import decide_subtitle_region, pick_sample_times

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - lightweight CI set has no OpenCV
    cv2 = None
    np = None


def _pos(y=0.70, h=0.08):
    return {"has_subtitles": True, "start_y_pct": y, "height_pct": h}


def _neg():
    return {"has_subtitles": False, "start_y_pct": 0.82, "height_pct": 0.18}


class TestDecideSubtitleRegion(unittest.TestCase):
    def test_single_positive_frame_is_not_enough(self):
        # The exact pattern from the bug report: 1 positive out of 5.
        d = decide_subtitle_region([_neg(), _neg(), _pos(), _neg(), _neg()])
        self.assertFalse(d["found"])
        self.assertEqual((d["positives"], d["analysed"]), (1, 5))

    def test_majority_positive_is_confirmed(self):
        d = decide_subtitle_region([_pos(), _pos(), _neg(), _pos(), _neg()])
        self.assertTrue(d["found"])
        self.assertEqual(d["positives"], 3)

    def test_two_of_seven_is_below_ratio(self):
        d = decide_subtitle_region([_pos(), _pos()] + [_neg()] * 5)
        self.assertFalse(d["found"])

    def test_few_frames_accept_one_positive(self):
        self.assertTrue(decide_subtitle_region([_pos()])["found"])
        self.assertTrue(decide_subtitle_region([_pos(), _neg()])["found"])

    def test_no_analysed_frames_is_not_found(self):
        d = decide_subtitle_region([])
        self.assertFalse(d["found"])
        self.assertIn("no frame", d["reason"])

    def test_text_in_upper_half_is_ignored(self):
        d = decide_subtitle_region([_pos(y=0.05, h=0.1)] * 5)
        self.assertFalse(d["found"])

    def test_pixel_values_are_normalised(self):
        d = decide_subtitle_region([_pos(y=864, h=130)] * 3)  # 1080p pixels
        self.assertTrue(d["found"])
        self.assertAlmostEqual(d["start_y"], 0.80 - 0.02, places=2)

    def test_region_uses_median_and_one_outlier_does_not_blow_up_height(self):
        results = [_pos(0.72, 0.08)] * 5 + [_pos(0.72, 0.40)]
        d = decide_subtitle_region(results)
        self.assertTrue(d["found"])
        self.assertLess(d["height"], 0.2)
        self.assertAlmostEqual(d["start_y"], 0.70, places=2)

    def test_region_stays_inside_frame(self):
        d = decide_subtitle_region([_pos(0.97, 0.3)] * 4)
        self.assertTrue(d["found"])
        self.assertLessEqual(d["start_y"] + d["height"], 1.0 + 1e-9)


class TestPickSampleTimes(unittest.TestCase):
    def test_samples_follow_dialogue_and_span_the_video(self):
        starts = [float(t) for t in range(5, 500, 5)]  # 99 dialogue lines
        times = pick_sample_times(starts, duration=520.0, count=7)
        self.assertEqual(len(times), 7)
        self.assertEqual(times, sorted(times))
        self.assertLess(times[0], 40)
        self.assertGreater(times[-1], 450)
        self.assertTrue(all(0 < t < 520 for t in times))

    def test_few_dialogue_lines_are_all_used(self):
        times = pick_sample_times([22.19, 54.9, 60.5], duration=90.0)
        self.assertEqual(times, [22.69, 55.4, 61.0])

    def test_falls_back_to_fractions_without_dialogue(self):
        times = pick_sample_times([], duration=100.0, count=7)
        self.assertEqual(len(times), 7)
        self.assertAlmostEqual(times[0], 10.0, places=1)

    def test_ignores_intro_and_garbage_values(self):
        times = pick_sample_times([0.0, 0.5, None, 30.0], duration=60.0)
        self.assertEqual(times, [30.5])

    def test_times_never_exceed_video_end(self):
        times = pick_sample_times([59.9], duration=60.0)
        self.assertLessEqual(times[0], 59.5)

    def test_unknown_duration_and_no_dialogue_returns_nothing(self):
        self.assertEqual(pick_sample_times([], duration=0.0), [])


@unittest.skipIf(cv2 is None, "OpenCV not installed")
class TestDetectorWithFlakyModel(unittest.TestCase):
    """Drives the real detector with a stubbed vision model that is wrong some of the time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.video = os.path.join(self.tmp, "clip.mp4")
        writer = cv2.VideoWriter(self.video, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 240))
        for _ in range(10 * 40):  # 40 s
            writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
        writer.release()

        from agents.video_merger_agent import VideoMergerAgent
        self.agent = VideoMergerAgent(output_dir=self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, answers, dialogue):
        seq = iter(answers)

        def fake_vision(**kwargs):
            a = next(seq)
            if isinstance(a, Exception):
                raise a
            return json.dumps(a), "stub-model"

        with patch("brain.gemini_client.call_gemini_vision", side_effect=fake_vision), \
                patch("brain.config.load_config", return_value={"gemini": {"api_keys": ["k"], "model": "m"}}):
            return self.agent._detect_subtitle_region_with_vision(self.video, dialogue_times=dialogue)

    DIALOGUE = [3.0, 8.0, 14.0, 20.0, 26.0, 32.0, 37.0]

    def test_one_false_positive_does_not_blur(self):
        answers = [_neg(), _neg(), _pos(), _neg(), _neg(), _neg(), _neg()]
        _, _, found = self._run(answers, self.DIALOGUE)
        self.assertFalse(found)

    def test_one_missed_frame_does_not_disable_blur(self):
        answers = [_pos(), _pos(), _neg(), _pos(), _pos(), _pos(), _neg()]
        y, h, found = self._run(answers, self.DIALOGUE)
        self.assertTrue(found)
        self.assertAlmostEqual(y, 0.68, places=2)

    def test_api_failures_are_unknown_not_negative(self):
        # 3 frames fail; the 4 that were analysed all show a subtitle.
        answers = [RuntimeError("429"), _pos(), RuntimeError("429"), _pos(), _pos(),
                   RuntimeError("429"), _pos()]
        _, _, found = self._run(answers, self.DIALOGUE)
        self.assertTrue(found)

    def test_uses_exactly_one_call_per_sampled_frame(self):
        calls = []

        def fake_vision(**kwargs):
            calls.append(kwargs.get("image_path"))
            return json.dumps(_neg()), "stub"

        with patch("brain.gemini_client.call_gemini_vision", side_effect=fake_vision), \
                patch("brain.config.load_config", return_value={"gemini": {"api_keys": ["k"], "model": "m"}}):
            self.agent._detect_subtitle_region_with_vision(self.video, dialogue_times=self.DIALOGUE)
        self.assertEqual(len(calls), 7)  # no second "stage 2" sweep any more


if __name__ == "__main__":
    unittest.main()
