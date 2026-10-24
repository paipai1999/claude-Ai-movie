"""core.split_plan - where the splitter cuts (pure planning, no FFmpeg)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import split_plan as sp  # noqa: E402


class IdealCutsTests(unittest.TestCase):
    def test_every_n_minutes(self):
        self.assertEqual(sp.ideal_cuts(30 * 60, "duration", minutes=10), [600.0, 1200.0])
        self.assertEqual(sp.ideal_cuts(35 * 60, "duration", minutes=10), [600.0, 1200.0, 1800.0])

    def test_a_short_leftover_joins_the_last_part(self):
        # 22 minutes in 10-minute parts: the 2 minute leftover must not become its own file.
        self.assertEqual(sp.ideal_cuts(22 * 60, "duration", minutes=10), [600.0])
        parts = sp.parts_from_cuts(22 * 60, [600.0])
        self.assertEqual(parts, [(0.0, 600.0), (600.0, 1320.0)])

    def test_a_video_shorter_than_one_part_has_no_cuts(self):
        self.assertEqual(sp.ideal_cuts(5 * 60, "duration", minutes=10), [])

    def test_equal_parts(self):
        cuts = sp.ideal_cuts(900, "parts", parts=3)
        self.assertEqual([round(c) for c in cuts], [300, 600])
        self.assertEqual(sp.parts_from_cuts(900, cuts)[-1], (600.0, 900.0))

    def test_bad_input_is_explained(self):
        for kwargs, fragment in (
            ({"duration": 0, "mode": "duration"}, "unknown"),
            ({"duration": 600, "mode": "duration", "minutes": 0.1}, "at least"),
            ({"duration": 600, "mode": "parts", "parts": 1}, "at least 2"),
            ({"duration": 30, "mode": "parts", "parts": 5}, "shorter than"),
            ({"duration": 600, "mode": "weekly"}, "Unknown split mode"),
        ):
            with self.assertRaises(ValueError) as caught:
                sp.ideal_cuts(**kwargs)
            self.assertIn(fragment, str(caught.exception))


class ParseSilenceTests(unittest.TestCase):
    LOG = (
        "[silencedetect @ 0000] silence_start: 12.5\n"
        "[silencedetect @ 0000] silence_end: 14.1 | silence_duration: 1.6\n"
        "frame=  1 fps=0 q=-0.0 size=N/A\n"
        "[silencedetect @ 0000] silence_start: -0.00098\n"
        "[silencedetect @ 0000] silence_end: 0.8 | silence_duration: 0.8\n"
        "[silencedetect @ 0000] silence_start: 95.0\n"
    )

    def test_pairs_and_negative_start(self):
        self.assertEqual(sp.parse_silencedetect(self.LOG, duration=100.0), [(12.5, 14.1), (0.0, 0.8), (95.0, 100.0)])

    def test_an_open_pause_is_dropped_without_a_duration(self):
        self.assertEqual(sp.parse_silencedetect(self.LOG), [(12.5, 14.1), (0.0, 0.8)])

    def test_noise_and_empty_logs(self):
        self.assertEqual(sp.parse_silencedetect(""), [])
        self.assertEqual(sp.parse_silencedetect("size=1kB time=00:00:01.00"), [])


class SnapTests(unittest.TestCase):
    def test_cuts_move_to_the_middle_of_the_nearest_pause(self):
        cuts = [600.0, 1200.0]
        pauses = [(595.0, 597.0), (610.0, 611.0), (1203.0, 1205.0)]
        # 596 is 4 s from 600, 610.5 is 10.5 s away: the nearer pause wins.
        self.assertEqual(sp.snap_cuts(cuts, pauses, 1800.0), [596.0, 1204.0])

    def test_breaths_are_not_places_to_cut(self):
        self.assertEqual(sp.snap_cuts([600.0], [(598.0, 598.2)], 1200.0), [600.0])

    def test_the_longer_pause_wins_a_tie(self):
        pauses = [(595.0, 596.0), (604.0, 606.0)]   # midpoints 595.5 and 605.0 -> 4.5 s and 5 s away
        self.assertEqual(sp.snap_cuts([600.0], pauses, 1200.0), [595.5])
        tie = [(594.0, 596.0), (604.0, 606.0)]      # both 5 s away; both 2 s long -> the earlier midpoint
        self.assertEqual(sp.snap_cuts([600.0], tie, 1200.0), [595.0])
        tie_long = [(594.0, 596.0), (603.0, 607.0)]  # 5 s each, the second pause is longer
        self.assertEqual(sp.snap_cuts([600.0], tie_long, 1200.0), [605.0])

    def test_nothing_in_reach_keeps_the_cut(self):
        self.assertEqual(sp.snap_cuts([600.0], [(100.0, 102.0)], 1200.0), [600.0])
        self.assertEqual(sp.snap_cuts([], [(100.0, 102.0)], 1200.0), [])

    def test_a_snap_that_would_make_a_stub_is_refused(self):
        # Parts of 25 s: moving the first cut 10 s earlier would leave a 15 s part (< MIN_PART_SEC).
        cuts = [25.0, 50.0]
        pauses = [(14.0, 16.0)]
        self.assertEqual(sp.snap_cuts(cuts, pauses, 75.0, window=20.0), [25.0, 50.0])

    def test_window_never_crosses_half_a_part(self):
        # 60 s parts: the window shrinks to 24 s, so a pause 30 s away is out of reach.
        self.assertEqual(sp.snap_cuts([60.0], [(88.0, 92.0)], 120.0, window=40.0), [60.0])

    def test_cuts_stay_ordered(self):
        cuts = [600.0, 1200.0, 1800.0]
        pauses = [(590.0, 592.0), (1190.0, 1192.0), (1790.0, 1792.0)]
        snapped = sp.snap_cuts(cuts, pauses, 2400.0)
        self.assertEqual(snapped, sorted(snapped))
        self.assertEqual(len(set(snapped)), 3)


if __name__ == "__main__":
    unittest.main()
