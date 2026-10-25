"""
core/smart_sync.py - the arithmetic of Smart Sync (the picture follows the voice). Pure logic, no FFmpeg.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import smart_sync as ss  # noqa: E402

LIMITS = ss.SyncLimits()          # slow down 1.25x, speed up 1.25x, voice 1.15x


def plan_of(blocks, duration=100.0, **kwargs):
    return ss.plan(blocks, duration, **kwargs)


class ModeTests(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(ss.normalize_mode("smart"), "smart")
        self.assertEqual(ss.normalize_mode(" Smart Sync "), "smart")
        self.assertEqual(ss.normalize_mode("smart-sync"), "smart")
        for other in (None, "", "audio_fit", "nonsense", 3):
            self.assertEqual(ss.normalize_mode(other), "audio_fit")

    def test_the_limits_come_from_the_config_within_sane_bounds(self):
        self.assertEqual(ss.limits_from_config(None), ss.SyncLimits())
        self.assertEqual(ss.limits_from_config({"sync": {"smart": {"max_slowdown": 1.4}}}).max_slowdown, 1.4)
        silly = ss.limits_from_config({"sync": {"smart": {"max_slowdown": 99, "max_speedup": "x", "max_voice_speedup": 0.2}}})
        self.assertEqual(silly.max_slowdown, 2.0)
        self.assertEqual(silly.max_speedup, 1.25)                # unreadable: the default
        self.assertEqual(silly.max_voice_speedup, 1.0)          # never slows the voice down by "speeding it up"

    def test_lo_and_hi(self):
        self.assertAlmostEqual(LIMITS.lo, 0.8)
        self.assertAlmostEqual(LIMITS.hi, 1.25)
        self.assertEqual(ss.SyncLimits(max_slowdown=0.5, max_speedup=0.5).hi, 1.0)         # never below "unchanged"
        self.assertEqual(ss.SyncLimits(max_slowdown=0.5, max_speedup=0.5).lo, 1.0)


class SceneRuleTests(unittest.TestCase):
    def one(self, slot, voice):
        result = plan_of([(10.0, 10.0 + slot, voice)], 100.0)
        segment = result.block_segment(0)
        return segment, result.voice_tempo.get(0), result

    def test_a_voice_that_fits_the_scene_changes_nothing(self):
        segment, tempo, _ = self.one(5.0, 5.0)
        self.assertAlmostEqual(segment.factor, 1.0)
        self.assertEqual(tempo, 1.0)

    def test_a_slightly_longer_voice_slows_the_picture_to_the_voice(self):
        segment, tempo, _ = self.one(5.0, 5.5)
        self.assertAlmostEqual(segment.out_length, 5.5)
        self.assertAlmostEqual(segment.factor, 1.1)
        self.assertEqual(tempo, 1.0)

    def test_a_slightly_shorter_voice_speeds_the_picture_up_to_the_voice(self):
        segment, tempo, _ = self.one(5.0, 4.5)
        self.assertAlmostEqual(segment.out_length, 4.5)
        self.assertAlmostEqual(segment.factor, 0.9)
        self.assertEqual(tempo, 1.0)

    def test_a_much_shorter_voice_is_followed_only_as_far_as_the_limit(self):
        segment, tempo, _ = self.one(10.0, 4.0)
        self.assertAlmostEqual(segment.factor, 0.8)                      # sped up 1.25x at most ...
        self.assertAlmostEqual(segment.out_length, 8.0)                  # ... so the voice ends early and the scene goes on
        self.assertEqual(tempo, 1.0)

    def test_a_longer_voice_speeds_the_voice_up_before_slowing_the_picture_more(self):
        segment, tempo, result = self.one(5.0, 7.0)                       # ratio 1.4 > 1.25
        self.assertAlmostEqual(segment.factor, 1.25)                     # the picture is slowed as far as allowed
        self.assertAlmostEqual(tempo, 7.0 / (5.0 * 1.25))               # and the voice takes up the rest: 1.12x
        self.assertLessEqual(tempo, LIMITS.max_voice_speedup)
        self.assertEqual(result.warnings, [])

    def test_the_voice_never_gets_faster_than_its_limit_the_picture_gives_way(self):
        segment, tempo, result = self.one(5.0, 8.0)                       # 8 / (5 * 1.25) = 1.28 > 1.15
        self.assertAlmostEqual(tempo, 1.15)
        self.assertAlmostEqual(segment.out_length, 8.0 / 1.15)
        self.assertGreater(segment.factor, 1.25)
        self.assertEqual(result.warnings, [])                             # 1.39x: noticeable, not alarming

    def test_a_far_too_long_line_is_reported(self):
        segment, tempo, result = self.one(3.0, 9.0)
        self.assertAlmostEqual(tempo, 1.15)
        self.assertGreater(segment.factor, ss.WARN_SLOWDOWN)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("Line 1", result.warnings[0])
        self.assertIn("Script Editor", result.warnings[0])

    def test_the_limits_can_be_changed(self):
        result = ss.plan([(0.0, 5.0, 7.0)], 20.0, ss.SyncLimits(max_slowdown=1.5))
        self.assertAlmostEqual(result.block_segment(0).factor, 1.4)       # 7 / 5 fits within 1.5: no voice speed-up needed
        self.assertEqual(result.voice_tempo[0], 1.0)

    def test_a_scene_with_no_voice_is_left_alone(self):
        segment, tempo, result = self.one(5.0, 0.0)
        self.assertAlmostEqual(segment.factor, 1.0)
        self.assertIsNone(tempo)

    def test_a_tiny_scene_gets_room_to_be_slowed(self):
        result = plan_of([(10.0, 10.1, 1.0)], 100.0)
        segment = result.block_segment(0)
        self.assertGreaterEqual(segment.src_length, ss.MIN_SLOT_SEC)


class CoverageTests(unittest.TestCase):
    def test_the_segments_cover_the_whole_film_without_holes(self):
        result = plan_of([(10, 15, 6.0), (20, 24, 3.0), (60, 70, 12.0)], 100.0)
        for before, after in zip(result.segments, result.segments[1:]):
            self.assertAlmostEqual(before.src_end, after.src_start)
            self.assertAlmostEqual(before.out_end, after.out_start)
        self.assertEqual(result.segments[0].src_start, 0.0)
        self.assertAlmostEqual(result.segments[-1].src_end, 100.0)
        self.assertAlmostEqual(result.out_duration, sum(s.out_length for s in result.segments))

    def test_the_film_between_the_lines_is_not_touched(self):
        result = plan_of([(10, 15, 6.0), (40, 45, 5.0)], 100.0)
        gaps = [s for s in result.segments if s.block is None]
        self.assertEqual(len(gaps), 3)                                    # before, between, after
        for gap in gaps:
            self.assertAlmostEqual(gap.factor, 1.0)

    def test_lines_in_the_wrong_order_are_sorted_and_keep_their_own_numbers(self):
        result = plan_of([(40, 45, 5.0), (10, 15, 5.0)], 100.0)
        self.assertEqual([s.block for s in result.segments if s.block is not None], [1, 0])
        self.assertAlmostEqual(result.block_segment(1).src_start, 10)

    def test_overlapping_lines_never_overlap_on_the_new_film(self):
        result = plan_of([(10, 20, 8.0), (15, 25, 8.0)], 100.0)
        first, second = result.block_segment(0), result.block_segment(1)
        self.assertGreaterEqual(second.src_start, first.src_end)
        self.assertGreaterEqual(second.out_start, first.out_end)

    def test_a_line_after_the_end_is_dropped_and_one_that_runs_over_is_clipped(self):
        result = plan_of([(95, 120, 5.0), (150, 160, 5.0)], 100.0)
        self.assertEqual(result.dropped, [1])
        self.assertAlmostEqual(result.block_segment(0).src_end, 100.0)
        self.assertAlmostEqual(result.segments[-1].src_end, 100.0)

    def test_no_lines_means_nothing_changes(self):
        result = plan_of([], 100.0)
        self.assertTrue(result.is_identity)
        self.assertEqual(len(result.segments), 1)
        self.assertAlmostEqual(result.out_duration, 100.0)

    def test_a_scene_that_changes_makes_the_plan_not_an_identity(self):
        self.assertFalse(plan_of([(10, 15, 9.0)], 100.0).is_identity)


class FrameTests(unittest.TestCase):
    def test_every_segment_lasts_a_whole_number_of_frames(self):
        result = plan_of([(10, 15.3, 6.07), (20.2, 24.9, 3.11), (60, 70.4, 12.5)], 100.0, fps=25.0)
        for segment in result.segments:
            frames = segment.out_length * 25.0
            self.assertAlmostEqual(frames, round(frames), places=6, msg=segment)
            self.assertGreaterEqual(round(frames), 1)

    def test_the_voice_of_a_line_starts_exactly_on_a_frame(self):
        result = plan_of([(10, 15.3, 6.07), (20.2, 24.9, 3.11)], 100.0, fps=25.0)
        for block in (0, 1):
            start = result.block_start(block)
            self.assertAlmostEqual(start * 25.0, round(start * 25.0), places=6)

    def test_a_gap_shorter_than_half_a_frame_is_absorbed(self):
        result = plan_of([(10, 15, 5.0), (15.01, 20, 5.0)], 100.0, fps=25.0)
        self.assertEqual(len([s for s in result.segments if s.block is None]), 2)   # before the first, after the last: no hair-line gap
        self.assertAlmostEqual(result.block_segment(1).src_start, 15.0)

    def test_no_fps_means_no_rounding(self):
        result = plan_of([(10, 15, 5.123456)], 100.0)
        self.assertAlmostEqual(result.block_segment(0).out_length, 5.123456)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.result = plan_of([(10, 20, 14.0)], 100.0)       # a 10 s scene lasting 12.5 s (the voice is sped up 1.12x)

    def test_times_before_during_and_after_the_scene(self):
        self.assertAlmostEqual(self.result.to_output(5.0), 5.0)
        self.assertAlmostEqual(self.result.to_output(10.0), 10.0)
        self.assertAlmostEqual(self.result.to_output(15.0), 10.0 + 5.0 * 1.25)
        self.assertAlmostEqual(self.result.to_output(20.0), 22.5)
        self.assertAlmostEqual(self.result.to_output(50.0), 52.5)
        self.assertAlmostEqual(self.result.to_output(100.0), self.result.out_duration)

    def test_times_outside_the_film_are_clamped(self):
        self.assertAlmostEqual(self.result.to_output(-5.0), 0.0)
        self.assertAlmostEqual(self.result.to_output(500.0), self.result.out_duration)

    def test_the_two_directions_agree(self):
        for t in (0.0, 4.2, 10.0, 13.3, 20.0, 77.7, 100.0):
            self.assertAlmostEqual(self.result.to_source(self.result.to_output(t)), t, places=6)

    def test_an_empty_plan_maps_to_itself(self):
        empty = ss.SyncPlan()
        self.assertEqual(empty.to_output(3.0), 3.0)
        self.assertEqual(empty.to_source(3.0), 3.0)


class ReportTests(unittest.TestCase):
    def test_the_summary_counts_what_happened(self):
        result = plan_of([(0, 10, 12.0), (20, 30, 8.0), (40, 50, 10.0), (60, 70, 4.0)], 100.0)
        summary = result.summary()
        self.assertEqual(summary["scenes"], 4)
        self.assertEqual(summary["slowed"], 1)
        self.assertEqual(summary["sped_up"], 2)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(summary["source_seconds"], 100.0)
        self.assertEqual(summary["output_seconds"], round(result.out_duration, 2))
        self.assertEqual(summary["slowest"], 1.2)
        self.assertEqual(summary["fastest"], 0.8)

    def test_the_plan_survives_json(self):
        import json
        result = plan_of([(10, 20, 15.0), (30, 33, 2.0)], 100.0, fps=25.0)
        again = ss.SyncPlan.from_dict(json.loads(json.dumps(result.to_dict())))
        self.assertEqual(len(again.segments), len(result.segments))
        for t in (0.0, 12.0, 31.0, 99.0):
            self.assertAlmostEqual(again.to_output(t), result.to_output(t), places=2)
        self.assertEqual({k: round(v, 2) for k, v in again.voice_tempo.items()},
                         {k: round(v, 2) for k, v in result.voice_tempo.items() if abs(v - 1.0) > 1e-3})

    def test_a_plan_without_data_reads_as_empty(self):
        self.assertEqual(ss.SyncPlan.from_dict({}).segments, [])
        self.assertEqual(ss.SyncPlan.from_dict(None).segments, [])


class VerifyTests(unittest.TestCase):
    """The check at the end of the re-timing: the rendered film against the plan."""

    def setUp(self):
        self.sync = ss.plan([(10, 20, 14.0)], 100.0, fps=25.0)          # 100 s -> 102.5 s

    def test_a_film_as_long_as_planned_with_the_narration_inside_it_is_ok(self):
        check = ss.verify(self.sync, self.sync.out_duration + 0.04, 95.0)
        self.assertTrue(check["ok"])
        self.assertAlmostEqual(check["drift_seconds"], 0.04, places=3)
        self.assertEqual(check["plan_seconds"], round(self.sync.out_duration, 3))

    def test_a_picture_that_drifted_from_the_plan_is_reported(self):
        check = ss.verify(self.sync, self.sync.out_duration + 2.0, 95.0)
        self.assertFalse(check["ok"])
        self.assertAlmostEqual(check["drift_seconds"], 2.0, places=3)

    def test_a_shorter_picture_is_reported_too(self):
        self.assertFalse(ss.verify(self.sync, self.sync.out_duration - 1.0, 95.0)["ok"])

    def test_a_narration_that_would_be_cut_off_by_the_end_of_the_film_is_reported(self):
        picture = self.sync.out_duration
        self.assertTrue(ss.verify(self.sync, picture, picture + 0.4)["ok"])         # half a second of tail is fine
        self.assertFalse(ss.verify(self.sync, picture, picture + 3.0)["ok"])

    def test_the_tolerance_grows_a_little_with_the_length_of_the_film(self):
        short = ss.verify(ss.plan([], 60.0), 60.0, 0.0)["tolerance"]
        long = ss.verify(ss.plan([], 7200.0), 7200.0, 0.0)["tolerance"]
        self.assertGreater(long, short)
        self.assertLess(long, 10.0)

    def test_the_check_survives_json_with_the_plan(self):
        import json
        self.sync.check = ss.verify(self.sync, self.sync.out_duration, 90.0)
        again = ss.SyncPlan.from_dict(json.loads(json.dumps(self.sync.to_dict())))
        self.assertEqual(again.check, self.sync.check)
        self.assertEqual(ss.SyncPlan().to_dict()["check"], {})


if __name__ == "__main__":
    unittest.main()
