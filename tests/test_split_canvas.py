"""
core/canvas.py (a picture on a canvas of another shape) and core/part_label.py (the "Part 2/5" label), the two pieces the
Splitter uses for vertical / square / wide parts. The filter strings are checked as text; the three ways of filling the
canvas are also rendered with the real FFmpeg and the pixels looked at.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import canvas, part_label  # noqa: E402
from core import resolution as res  # noqa: E402


class CanvasSizeTests(unittest.TestCase):
    def test_the_named_shapes_take_the_size_of_the_preset(self):
        self.assertEqual(canvas.canvas_for("9:16", "1080p", 1920, 1080), (1080, 1920))
        self.assertEqual(canvas.canvas_for("1:1", "720p", 1920, 1080), (720, 720))
        self.assertEqual(canvas.canvas_for("16:9", "480p", 1080, 1920), (854, 480))

    def test_original_keeps_the_shape_of_the_source_and_never_exceeds_the_preset(self):
        self.assertEqual(canvas.canvas_for("original", "1080p", 1920, 1080), (1920, 1080))
        self.assertEqual(canvas.canvas_for("original", "1080p", 3840, 2160), (1920, 1080))
        self.assertEqual(canvas.canvas_for("original", "480p", 1920, 1080), (854, 480))
        self.assertEqual(canvas.canvas_for("original", "1080p", 640, 360), (640, 360))       # a small source is not enlarged
        self.assertEqual(canvas.canvas_for("original", "1080p", 1080, 1920), (1080, 1920))   # portrait stays portrait

    def test_sizes_are_even_for_h264(self):
        for source in ((1281, 721), (853, 479), (1919, 1079)):
            width, height = canvas.canvas_for("original", "480p", *source)
            self.assertEqual((width % 2, height % 2), (0, 0), source)

    def test_an_unknown_source_size_falls_back_to_the_landscape_preset(self):
        self.assertEqual(canvas.canvas_for("original", "720p", 0, 0), (1280, 720))
        self.assertEqual(canvas.canvas_for("original", "720p", None, None), (1280, 720))

    def test_the_square_canvas_exists_in_the_resolution_module(self):
        for preset in res.PRESETS:
            width, height = res.canvas("1:1", preset)
            self.assertEqual(width, height)
            self.assertEqual(width, res.short_side(preset))


class GraphTests(unittest.TestCase):
    def test_a_source_with_the_canvas_shape_is_just_scaled(self):
        graph = canvas.graph(1920, 1080, 1280, 720, "blur")
        self.assertEqual(graph, "[0:v]scale=1280:720:flags=lanczos,setsar=1[canvas]")
        self.assertNotIn("boxblur", graph)

    def test_an_unknown_source_is_treated_the_same_way(self):
        self.assertIn("scale=1280:720", canvas.graph(None, None, 1280, 720, "blur"))
        self.assertNotIn("split", canvas.graph(0, 0, 1280, 720, "black"))

    def test_blur_puts_the_whole_picture_over_a_blurred_copy(self):
        graph = canvas.graph(1920, 1080, 1080, 1920, "blur")
        self.assertIn("split=2[cv_bg_src][cv_fg_src]", graph)
        self.assertIn("boxblur", graph)
        self.assertIn("force_original_aspect_ratio=decrease", graph)          # the foreground is limited by BOTH sides
        self.assertIn("overlay=(W-w)/2:(H-h)/2", graph)
        self.assertTrue(graph.endswith("[canvas]"))

    def test_black_pads_the_fitted_picture(self):
        graph = canvas.graph(1920, 1080, 1080, 1920, "black")
        self.assertIn("pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black", graph)
        self.assertNotIn("boxblur", graph)

    def test_crop_fills_the_canvas_from_the_middle(self):
        graph = canvas.graph(1920, 1080, 1080, 1920, "crop")
        self.assertIn("force_original_aspect_ratio=increase", graph)
        self.assertIn("crop=1080:1920", graph)

    def test_an_unknown_fill_behaves_like_blur(self):
        self.assertEqual(canvas.graph(1920, 1080, 1080, 1920, "nonsense"), canvas.graph(1920, 1080, 1080, 1920, "blur"))

    def test_the_input_and_output_labels_can_be_chosen(self):
        graph = canvas.graph(1920, 1080, 1080, 1920, "blur", source="[in]", label="[vout]")
        self.assertTrue(graph.startswith("[in]split=2"))
        self.assertTrue(graph.endswith("[vout]"))

    def test_the_ass_filter_escapes_a_windows_path(self):
        self.assertEqual(canvas.escape_filter_path("C:\\Fonts\\it's"), "C\\:/Fonts/it\\'s")
        text = canvas.ass_filter("label_01.ass")
        self.assertTrue(text.startswith("ass=filename='label_01.ass':shaping=1:fontsdir='"))
        self.assertIn("assets/fonts", text)

    def test_the_option_model_lists_the_same_shapes_and_fills(self):
        from core import options
        shapes = next(o for o in options.OPTIONS if o.name == "split_format")
        fills = next(o for o in options.OPTIONS if o.name == "split_fill")
        self.assertEqual(tuple(shapes.choices), canvas.FORMATS)
        self.assertEqual(tuple(fills.choices), canvas.FILLS)
        self.assertEqual((shapes.default, fills.default), ("original", "blur"))


class PictureTests(unittest.TestCase):
    """The three ways of filling a vertical canvas from a 4:3 picture, looked at pixel by pixel."""

    @classmethod
    def setUpClass(cls):
        import imageio_ffmpeg
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cls.tmp = tempfile.mkdtemp(prefix="pai_canvas_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def frame(self, fill):
        import numpy as np
        from PIL import Image
        png = os.path.join(self.tmp, f"{fill}.png")
        graph = canvas.graph(320, 240, 180, 320, fill, "[0:v]", "[vout]")
        cmd = [self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5:duration=1",
               "-filter_complex", graph, "-map", "[vout]", "-frames:v", "1", png]
        done = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        return np.asarray(Image.open(png).convert("L"), dtype="float32")

    def test_every_fill_makes_the_canvas_size(self):
        for fill in canvas.FILLS:
            self.assertEqual(self.frame(fill).shape, (320, 180), fill)

    def test_black_bars_are_black_and_the_middle_is_the_picture(self):
        picture = self.frame("black")
        self.assertLess(picture[:30].mean(), 20)                 # top bar
        self.assertLess(picture[-30:].mean(), 20)                # bottom bar
        self.assertGreater(picture[130:190].mean(), 40)          # the picture in the middle

    def test_blur_fills_the_bars_with_the_picture_itself(self):
        picture = self.frame("blur")
        self.assertGreater(picture[:30].mean(), 30)              # not black
        self.assertGreater(picture[130:190].std(), 10)           # the sharp picture is still there

    def test_crop_leaves_no_bars_at_all(self):
        picture = self.frame("crop")
        self.assertGreater(picture[:30].mean(), 30)
        self.assertGreater(picture[-30:].mean(), 30)


class BlurBandTests(unittest.TestCase):
    """Blurring the band where the source's own subtitles sit (core.canvas.blur_band)."""

    @classmethod
    def setUpClass(cls):
        import imageio_ffmpeg
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cls.tmp = tempfile.mkdtemp(prefix="pai_band_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_fragment_crops_blurs_and_lays_the_band_back(self):
        graph = canvas.blur_band(0.8, 0.2)
        self.assertTrue(graph.startswith("[0:v]split=2[cv_band_src][cv_band_crop];"))
        self.assertIn("boxblur=luma_radius=18", graph)
        self.assertIn("overlay=0:'trunc(H*0.800/2)*2'", graph)
        self.assertTrue(graph.endswith("[cv_clean]"))

    def test_the_labels_can_be_chosen(self):
        graph = canvas.blur_band(0.7, 0.2, source="[in]", label="[out]")
        self.assertTrue(graph.startswith("[in]split=2"))
        self.assertTrue(graph.endswith("[out]"))

    def test_silly_numbers_stay_inside_the_picture(self):
        self.assertIn("0.950", canvas.blur_band(1.5, 0.3))                            # the band cannot start below the picture
        self.assertIn("crop=iw:'trunc(ih*0.050/2)*2'", canvas.blur_band(0.95, 0.5))   # nor reach past its bottom
        self.assertIn("crop=iw:'trunc(ih*0.020/2)*2'", canvas.blur_band(0.5, 0.0))    # nor be nothing

    def frame(self, graph):
        import numpy as np
        from PIL import Image
        png = os.path.join(self.tmp, f"band_{abs(hash(graph))}.png")
        cmd = [self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=5:duration=1",
               "-filter_complex", graph, "-map", "[cv_clean]", "-frames:v", "1", png]
        done = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        return np.asarray(Image.open(png).convert("L"), dtype="float32")

    def test_only_the_band_is_blurred(self):
        import numpy as np
        blurred = self.frame(canvas.blur_band(0.8, 0.2))
        untouched = self.frame("[0:v]null[cv_clean]")
        self.assertEqual(blurred.shape, untouched.shape)
        top = slice(0, int(240 * 0.78))
        band = slice(int(240 * 0.82), 240)
        self.assertLess(float(np.abs(blurred[top] - untouched[top]).mean()), 1.0)         # the rest of the picture is the same
        self.assertGreater(float(np.abs(blurred[band] - untouched[band]).mean()), 3.0)     # the band is not
        self.assertLess(float(blurred[band].std()), float(untouched[band].std()))          # and has lost its detail


class LabelTextTests(unittest.TestCase):
    def test_the_places_are_filled_in(self):
        self.assertEqual(part_label.render("Part {n}/{total}", 2, 5), "Part 2/5")
        self.assertEqual(part_label.render("အပိုင်း {mm}/{total_mm}", 2, 12), "အပိုင်း ၂/၁၂")
        self.assertEqual(part_label.render("{n} of {total} - {mm}", 10, 12), "10 of 12 - ၁၀")

    def test_nothing_in_nothing_out(self):
        for empty in (None, "", "   "):
            self.assertEqual(part_label.render(empty, 1, 3), "")

    def test_unknown_words_are_left_as_written_and_whitespace_is_tidied(self):
        self.assertEqual(part_label.render("  Part   {n}   {nope} ", 1, 3), "Part 1 {nope}")

    def test_a_very_long_label_is_cut(self):
        self.assertEqual(len(part_label.render("x" * 500, 1, 2)), part_label.MAX_LABEL_CHARS)

    def test_myanmar_digits(self):
        self.assertEqual(part_label.myanmar_number(0), "၀")
        self.assertEqual(part_label.myanmar_number(1234567890), "၁၂၃၄၅၆၇၈၉၀")


class LabelAssTests(unittest.TestCase):
    def test_the_label_is_on_screen_for_the_whole_part_top_centre_on_a_dark_box(self):
        ass = part_label.build_label_ass("Part 2/5", 95.0, 1080, 1920)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("Style: Label,Padauk,", ass)
        self.assertIn(",3,", ass)                                 # BorderStyle 3 = opaque box
        self.assertIn(",8,40,40,", ass)                           # alignment 8 = top centre
        line = [l for l in ass.splitlines() if l.startswith("Dialogue:")][0]
        self.assertIn("0:00:00.00", line)
        self.assertIn("0:01:36.00", line)                         # 95 s + 1 s
        self.assertTrue(line.endswith("Part 2/5"))

    def test_burmese_text_is_kept_whole(self):
        ass = part_label.build_label_ass("အပိုင်း ၂/၅", 30.0, 720, 1280)
        self.assertIn("အပိုင်း ၂/၅", ass)

    def test_override_characters_cannot_come_from_the_label(self):
        ass = part_label.build_label_ass("Part {\\an5\\fs200}2", 30.0, 720, 1280)
        line = [l for l in ass.splitlines() if l.startswith("Dialogue:")][0]
        self.assertNotIn("{", line)
        self.assertNotIn("\\an5", line)

    def test_a_long_label_wraps_into_at_most_two_lines(self):
        ass = part_label.build_label_ass("a long label with many words in it " * 3, 30.0, 480, 854)
        line = [l for l in ass.splitlines() if l.startswith("Dialogue:")][0]
        self.assertLessEqual(line.count("\\N"), 1)

    def test_the_size_follows_the_short_side_of_the_canvas(self):
        def size(width, height):
            style = [l for l in part_label.build_label_ass("x", 5, width, height).splitlines() if l.startswith("Style: Label")][0]
            return int(style.split(",")[2])
        self.assertGreater(size(1080, 1920), size(720, 1280))
        self.assertGreater(size(720, 1280), size(480, 854))
        self.assertEqual(size(1080, 1920), size(1920, 1080))       # the same look in portrait and landscape


if __name__ == "__main__":
    unittest.main()
