"""core.short_ass - subtitles for one Short (clip-relative times, hook title, safe line breaks)."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.config import SUBTITLE_PRESETS  # noqa: E402
from core import short_ass as sa  # noqa: E402


def events(document):
    return [line for line in document.splitlines() if line.startswith("Dialogue:")]


class WrapTests(unittest.TestCase):
    def test_short_text_stays_whole(self):
        self.assertEqual(sa.wrap_text("Hello there", 22), ["Hello there"])
        self.assertEqual(sa.wrap_text("", 22), [])
        self.assertEqual(sa.wrap_text("   ", 22), [])

    def test_breaks_at_spaces_and_respects_the_width(self):
        lines = sa.wrap_text("this sentence is clearly longer than one comfortable line", 22)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 22 for line in lines))
        self.assertEqual(" ".join(lines), "this sentence is clearly longer than one comfortable line")

    def test_myanmar_breaks_after_punctuation_never_inside_a_syllable(self):
        text = "ဒီနေ့ကျွန်တော်တို့ အရေးကြီးတဲ့ အကြောင်းတစ်ခုကို၊ ပြောပြပါမယ် သင့်ဘဝကို ပြောင်းလဲစေမယ့် အကြောင်းပါ"
        lines = sa.wrap_text(text, 22)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))   # nothing lost
        for line in lines[:-1]:
            self.assertTrue(text.index(line) == 0 or text[text.index(line) - 1] in " ")   # starts at a word edge

    def test_text_without_break_points_is_left_to_the_renderer(self):
        word = "x" * 60
        self.assertEqual(sa.wrap_text(word, 22), [word])

    def test_line_limit(self):
        lines = sa.wrap_text(" ".join(["word"] * 60), 22, max_lines=2)
        self.assertEqual(len(lines), 2)


class DocumentTests(unittest.TestCase):
    def test_header_and_styles(self):
        doc = sa.build_short_ass([], 100.0, 130.0, "9:16", "1080p")
        self.assertIn("PlayResX: 1080", doc)
        self.assertIn("PlayResY: 1920", doc)
        self.assertIn("Style: Default,Padauk,56,", doc)
        self.assertIn("Style: Hook,Padauk,64,", doc)
        wide = sa.build_short_ass([], 0.0, 30.0, "16:9", "720p")
        self.assertIn("PlayResX: 1280", wide)
        self.assertIn("PlayResY: 720", wide)
        self.assertEqual(sa.geometry("4:3", "8k"), sa.GEOMETRY[("9:16", "1080p")])

    def test_times_are_counted_from_the_start_of_the_clip(self):
        lines = [{"start": 100.0, "end": 103.5, "text": "first"},
                 {"start": 110.0, "end": 114.0, "text": "second"}]
        found = events(sa.build_short_ass(lines, 100.0, 130.0))
        self.assertEqual(len(found), 2)
        self.assertIn("0:00:00.00,0:00:03.50,Default", found[0])
        self.assertIn("0:00:10.00,0:00:14.00,Default", found[1])

    def test_lines_are_clamped_to_the_clip_and_outsiders_dropped(self):
        lines = [{"start": 90.0, "end": 102.0, "text": "starts before"},      # 0 - 2 s of the clip
                 {"start": 128.0, "end": 140.0, "text": "ends after"},        # 28 - 30 s
                 {"start": 60.0, "end": 70.0, "text": "outside"},
                 {"start": 129.9, "end": 130.1, "text": "a blink"},           # too short once clamped
                 {"start": 105.0, "end": 106.0, "text": "   "}]               # no text
        found = events(sa.build_short_ass(lines, 100.0, 130.0))
        self.assertEqual(len(found), 2)
        self.assertIn("0:00:00.00,0:00:02.00,Default", found[0])
        self.assertIn("0:00:28.00,0:00:30.00,Default", found[1])
        self.assertFalse(any("outside" in f or "blink" in f for f in found))

    def test_hook_title_is_shown_first_with_a_fade(self):
        doc = sa.build_short_ass([], 100.0, 130.0, title="Nobody tells you this")
        hook = events(doc)[0]
        self.assertIn("Dialogue: 1,0:00:00.00,0:00:03.20,Hook", hook)
        self.assertIn("{\\fad(250,350)}Nobody tells you this", hook)

    def test_hook_is_never_longer_than_a_tiny_clip(self):
        hook = events(sa.build_short_ass([], 0.0, 2.0, title="Short"))[0]
        self.assertIn("0:00:00.00,0:00:02.00,Hook", hook)

    def test_no_hook_without_a_title(self):
        self.assertEqual(events(sa.build_short_ass([], 0.0, 30.0, title="   ")), [])
        self.assertEqual(events(sa.build_short_ass([], 0.0, 30.0, title=None)), [])

    def test_ass_control_characters_are_removed_from_text(self):
        lines = [{"start": 0.0, "end": 3.0, "text": "bad {\\i1}tag\\Nhere"}]
        found = events(sa.build_short_ass(lines, 0.0, 30.0, title="{\\pos(0,0)}x"))
        self.assertTrue(all("{\\i1}" not in f and "{\\pos" not in f for f in found))
        self.assertIn("bad i1tagNhere", found[1])

    def test_long_lines_get_ass_line_breaks(self):
        lines = [{"start": 0.0, "end": 4.0, "text": "this sentence is clearly longer than one comfortable line"}]
        self.assertIn("\\N", events(sa.build_short_ass(lines, 0.0, 30.0))[0])

    def test_style_follows_the_preset(self):
        yellow = SUBTITLE_PRESETS["yellow_pop"]["primary_color"]
        doc = sa.build_short_ass([], 0.0, 30.0, preset="yellow_pop")
        self.assertIn(f"Style: Default,Padauk,56,{yellow},", doc)
        unknown = sa.build_short_ass([], 0.0, 30.0, preset="nope")
        self.assertIn(SUBTITLE_PRESETS["box_black"]["primary_color"], unknown)

    def test_valid_ass_timestamps_only(self):
        doc = sa.build_short_ass([{"start": 3600.0, "end": 3604.0, "text": "late"}], 3600.0, 3630.0, title="t")
        for stamp in re.findall(r"Dialogue: \d,([\d:.]+),([\d:.]+)", doc):
            for value in stamp:
                self.assertRegex(value, r"^\d:\d{2}:\d{2}\.\d{2}$")


class ClipLinesTests(unittest.TestCase):
    def test_overlapping_segments_use_the_translation(self):
        segments = [{"start": 0, "end": 5, "original": "a", "burmese": "A"},
                    {"start": 5, "end": 10, "original": "b", "burmese": ""},
                    {"start": 10, "end": 15, "original": "c", "burmese": "C"},
                    {"start": 20, "end": 25, "original": "d", "burmese": "D"}]
        lines = sa.text_lines_for_clip(segments, 4.0, 12.0)
        self.assertEqual([(l["start"], l["text"]) for l in lines], [(0.0, "A"), (5.0, "b"), (10.0, "C")])   # blank -> original
        self.assertEqual([l["text"] for l in sa.text_lines_for_clip(segments, 4.0, 12.0, field="original")], ["a", "b", "c"])

    def test_touching_edges_do_not_overlap(self):
        segments = [{"start": 0, "end": 5, "original": "a"}, {"start": 12, "end": 15, "original": "z"}]
        self.assertEqual(sa.text_lines_for_clip(segments, 5.0, 12.0), [])


if __name__ == "__main__":
    unittest.main()
