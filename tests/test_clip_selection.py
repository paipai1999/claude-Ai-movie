"""core.clip_selection - choosing the clips of a long video (pure logic)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import clip_selection as cs  # noqa: E402


def seg(start, end, text):
    return {"start": float(start), "end": float(end), "original": text}


def talk(count, step=5.0, length=4.0, text="This is a sentence about the plan."):
    """``count`` back-to-back sentences, one every ``step`` seconds."""
    return [seg(i * step, i * step + length, f"{text} {i}.") for i in range(count)]


class ParseTimeTests(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {12.5: 12.5, 7: 7.0, "12.5": 12.5, "01:02": 62.0, "1:02:03": 3723.0, "00:01:02,500": 62.5,
                 " 0:05 ": 5.0, "00:00:07.250": 7.25}
        for raw, expected in cases.items():
            self.assertEqual(cs.parse_time(raw), expected, raw)

    def test_rejected_forms(self):
        for raw in (None, "", "abc", "-3", -1, float("nan"), float("inf"), True, "1:2:3:4"):
            self.assertIsNone(cs.parse_time(raw), repr(raw))

    def test_clock(self):
        self.assertEqual(cs.clock(75), "01:15")
        self.assertEqual(cs.clock(3725), "1:02:05")
        self.assertEqual(cs.clock(-5), "00:00")


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.segments = talk(40)           # 0-4, 5-9, 10-14 ... 195-199
        self.norm = lambda item, **kw: cs.normalize_candidate(
            item, self.segments, kw.get("duration", 200.0), kw.get("min_sec", 20.0), kw.get("max_sec", 60.0))

    def test_times_snap_onto_sentence_edges(self):
        clip = self.norm({"start": 10.9, "end": 48.7, "score": 80, "title": "  A   title  "})
        self.assertEqual((clip["start"], clip["end"]), (10.0, 49.0))     # 10.9 -> 10 (start of line 2), 48.7 -> 49
        self.assertEqual(clip["title"], "A title")
        self.assertEqual(clip["duration"], 39.0)

    def test_swapped_and_clamped_times(self):
        clip = self.norm({"start": 190, "end": 30})
        self.assertEqual((clip["start"], clip["end"]), (30.0, 89.0))      # swapped, then cut back to the last sentence end within 60 s
        self.assertLessEqual(clip["duration"], 60.0)
        tail = self.norm({"start": 150, "end": 900})                       # end clamps to the video length
        self.assertLessEqual(tail["end"], 200.0)

    def test_too_long_ends_on_a_sentence(self):
        clip = self.norm({"start": 0, "end": 200})
        self.assertEqual(clip["start"], 0.0)
        self.assertLessEqual(clip["duration"], 60.0)
        self.assertIn(clip["end"], [s["end"] for s in self.segments])

    def test_too_short_is_stretched(self):
        clip = self.norm({"start": 0, "end": 6})
        self.assertGreaterEqual(clip["duration"], 20.0)
        self.assertIn(clip["end"], [s["end"] for s in self.segments])

    def test_not_enough_video_left_drops_the_clip(self):
        self.assertIsNone(self.norm({"start": 190, "end": 199}, duration=200.0))

    def test_unusable_items(self):
        for item in (None, "text", {}, {"start": "x", "end": 30}, {"start": 30}, {"start": 30, "end": 30}):
            self.assertIsNone(self.norm(item), repr(item))

    def test_score_scales_and_defaults(self):
        self.assertEqual(self.norm({"start": 0, "end": 30, "score": 0.8})["score"], 80.0)
        self.assertEqual(self.norm({"start": 0, "end": 30, "score": 250})["score"], 100.0)
        self.assertEqual(self.norm({"start": 0, "end": 30, "score": "n/a"})["score"], 50.0)
        self.assertEqual(self.norm({"start": 0, "end": 30})["score"], 50.0)

    def test_text_fields_are_cleaned(self):
        clip = self.norm({"start": 0, "end": 30, "title": "x" * 500, "hashtags": "#a, b #a  #c d"})
        self.assertEqual(len(clip["title"]), cs.MAX_TITLE_CHARS)
        self.assertEqual(clip["hashtags"], ["#a", "#b", "#c", "#d"])
        many = self.norm({"start": 0, "end": 30, "hashtags": [f"t{i}" for i in range(20)]})
        self.assertEqual(len(many["hashtags"]), cs.MAX_HASHTAGS)
        self.assertEqual(self.norm({"start": 0, "end": 30})["source"], "model")


class ChooseTests(unittest.TestCase):
    def clip(self, start, end, score):
        return {"start": start, "end": end, "duration": end - start, "score": score}

    def test_best_non_overlapping_in_time_order(self):
        candidates = [self.clip(0, 30, 70), self.clip(10, 40, 95), self.clip(100, 130, 60), self.clip(200, 230, 80)]
        picked = cs.choose_clips(candidates, 3)
        self.assertEqual([(c["start"], c["rank"], c["index"]) for c in picked], [(10, 1, 1), (100, 3, 2), (200, 2, 3)])
        self.assertNotIn(0, [c["start"] for c in picked])        # overlaps the best clip, so it lost

    def test_small_overlap_is_allowed(self):
        a, b = self.clip(0, 30, 90), self.clip(28, 58, 80)       # 2 s of 30 s = 6.7 %
        self.assertEqual(len(cs.choose_clips([a, b], 2)), 2)

    def test_count_zero_and_ties(self):
        self.assertEqual(cs.choose_clips([self.clip(0, 30, 90)], 0), [])
        tied = cs.choose_clips([self.clip(100, 130, 80), self.clip(0, 30, 80)], 1)
        self.assertEqual(tied[0]["start"], 0)                     # the earlier one wins a tie

    def test_inputs_are_not_mutated(self):
        original = self.clip(0, 30, 90)
        cs.choose_clips([original], 1)
        self.assertNotIn("rank", original)


class TranscriptTests(unittest.TestCase):
    def test_chunks_cover_everything_with_a_look_ahead(self):
        segments = talk(400, step=5.0)               # ~2000 s
        chunks = cs.chunk_segments(segments, chunk_sec=720, overlap_sec=45)
        self.assertEqual(len(chunks), 3)
        covered = {id(s) for chunk in chunks for s in chunk}
        self.assertEqual(len(covered), len(segments))
        self.assertGreater(cs._end(chunks[0][-1]), 720)           # first chunk reaches into the second
        self.assertLessEqual(cs._start(chunks[0][-1]), 765)
        self.assertEqual(cs.chunk_segments([]), [])

    def test_format_transcript(self):
        text = cs.format_transcript([seg(0, 2, "Hello  there"), seg(65, 67, ""), seg(3725, 3727, "x" * 400)])
        lines = text.splitlines()
        self.assertEqual(lines[0], "[00:00-00:02] Hello there")
        self.assertEqual(len(lines), 2)                           # the empty segment is skipped
        self.assertTrue(lines[1].startswith("[1:02:05-1:02:07] "))
        self.assertEqual(len(lines[1]), len("[1:02:05-1:02:07] ") + 300)

    def test_text_key_fallback(self):
        self.assertEqual(cs.text_of({"text": " hi "}), "hi")
        self.assertEqual(cs.text_of({"original": "a", "text": "b"}), "a")


class HeuristicTests(unittest.TestCase):
    def build(self):
        quiet = [seg(i * 12, i * 12 + 2, "mm.") for i in range(5)]                     # 0-50 s: sparse
        lively = [seg(60, 64, "Why did nobody ever tell you this secret about money?"),
                  seg(64.2, 69, "In 2024 I lost 3 million dollars because of one crazy mistake!"),
                  seg(69.2, 74, "Here is exactly how to avoid it, step by step."),
                  seg(74.2, 79, "First, never trust a tip you cannot check."),
                  seg(79.2, 84, "Second, always read the contract before you sign!")]
        return quiet + lively

    def test_finds_the_lively_opening_window(self):
        found = cs.heuristic_candidates(self.build(), 20, 40)
        self.assertTrue(found)
        best = max(found, key=lambda c: c["score"])
        self.assertEqual(best["start"], 60.0)
        self.assertEqual(best["source"], "heuristic")
        self.assertTrue(best["title"].startswith("Why did nobody"))
        self.assertGreaterEqual(best["duration"], 20.0)

    def test_respects_the_length_limits(self):
        for clip in cs.heuristic_candidates(talk(60), 20, 40):
            self.assertGreaterEqual(clip["duration"], 20.0)
            self.assertLessEqual(clip["duration"], 40.0)

    def test_empty_and_silent_input(self):
        self.assertEqual(cs.heuristic_candidates([], 20, 60), [])
        self.assertEqual(cs.heuristic_candidates([seg(0, 30, "")], 20, 60), [])

    def test_scores_stay_in_range_and_feed_choose_clips(self):
        found = cs.heuristic_candidates(talk(80), 20, 50)
        self.assertTrue(all(0.0 <= c["score"] <= 100.0 for c in found))
        picked = cs.choose_clips(found, 4)
        self.assertEqual(len(picked), 4)
        for a, b in zip(picked, picked[1:]):
            self.assertLessEqual(a["end"], b["start"] + 0.2 * min(a["duration"], b["duration"]))

    def test_focus_changes_the_ranking(self):
        funny = [seg(0, 6, "He told a joke and everyone started to laugh haha lol."),
                 seg(6.2, 12, "It was the funniest prank, truly hilarious."),
                 seg(12.2, 24, "Anyway, the weather today is fine, I think, maybe.")]
        calm = [seg(40, 46, "The committee will review the document next week."),
                seg(46.2, 52, "A decision is expected after the review is complete."),
                seg(52.2, 64, "Anyway, the weather today is fine, I think, maybe.")]
        found_funny = cs.heuristic_candidates(funny + calm, 20, 30, focus="funny")
        top = max(found_funny, key=lambda c: c["score"])
        self.assertLess(top["start"], 30)

    def test_burmese_text_counts_as_speech(self):
        segments = [seg(0, 12, "ဒီနေ့ ကျွန်တော်တို့ အရေးကြီးတဲ့ အကြောင်းအရာတစ်ခုကို ပြောပြပါမယ်။"),
                    seg(12.2, 24, "အဲဒီအကြောင်းအရာက သင့်ဘဝကို ပြောင်းလဲစေနိုင်ပါတယ်။")]
        found = cs.heuristic_candidates(segments, 20, 30)
        self.assertEqual(len(found), 1)
        self.assertGreater(found[0]["score"], 0)


if __name__ == "__main__":
    unittest.main()
