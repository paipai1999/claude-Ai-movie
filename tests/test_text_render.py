"""
core.text_render – Myanmar-safe text rendering through FFmpeg/HarfBuzz.

Pillow's basic layout mis-shapes Burmese (ေ drawn after its consonant, ူ detached), so the outro
card and the watermark go through FFmpeg's drawtext instead. These tests need the FFmpeg that ships
with imageio-ffmpeg; they skip themselves when it cannot render text.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import text_render  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "assets", "fonts", "Padauk.ttf")
BURMESE = "ကျေးဇူးတင်ပါတယ်"


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _ffmpeg()
CAN_RENDER = bool(FFMPEG) and os.path.exists(FONT) and text_render.render_text(FFMPEG, "Test", FONT, 32) is not None


@unittest.skipUnless(CAN_RENDER, "FFmpeg drawtext or the Padauk font is not available")
class RenderTextTests(unittest.TestCase):
    def test_burmese_line_is_a_tight_rgba_image_in_the_requested_colour(self):
        img = text_render.render_text(FFMPEG, BURMESE, FONT, 48, (234, 179, 8, 255))
        self.assertIsNotNone(img)
        self.assertEqual(img.mode, "RGBA")
        # Cropped to the ink: far smaller than the generous drawing canvas, but not degenerate.
        self.assertGreater(img.width, 100)
        self.assertLess(img.height, 48 * 2)
        solid = [px for px in img.getdata() if px[3] == 255]
        self.assertTrue(solid, "expected fully covered glyph pixels")
        self.assertEqual(solid[0][:3], (234, 179, 8))

    def test_alpha_in_fill_scales_coverage(self):
        full = text_render.render_text(FFMPEG, "Pai", FONT, 48, (255, 255, 255, 255))
        half = text_render.render_text(FFMPEG, "Pai", FONT, 48, (255, 255, 255, 128))
        self.assertLessEqual(max(half.getchannel("A").getdata()), 128)
        self.assertEqual(max(full.getchannel("A").getdata()), 255)

    def test_shaping_differs_from_pillow_basic_layout(self):
        """The whole point: FFmpeg/HarfBuzz output must not be Pillow's mis-shaped line."""
        if text_render.pillow_shapes_complex_scripts():
            self.skipTest("this Pillow can shape Myanmar itself (raqm)")
        shaped = text_render.render_text(FFMPEG, BURMESE, FONT, 48)
        basic = text_render._pillow_line(BURMESE, FONT, 48, (255, 255, 255, 255))
        self.assertNotEqual(shaped.tobytes(), basic.resize(shaped.size).tobytes())
        # Basic layout stretches the word out (detached ူ, ေ after the consonant); shaped text is tighter.
        self.assertLess(shaped.width, basic.width)

    def test_awkward_characters_do_not_break_the_filtergraph(self):
        for text in ("It's 50% off: now", r"back\slash", "%{localtime} injection", "quote\"s & ;,[]"):
            with self.subTest(text=text):
                img = text_render.render_text(FFMPEG, text, FONT, 36)
                self.assertIsNotNone(img)
                self.assertTrue(img.getchannel("A").getbbox())

    def test_expansion_is_disabled_so_user_text_is_rendered_literally(self):
        literal = text_render.render_text(FFMPEG, "%{localtime}", FONT, 36)
        # A 12-glyph literal is far wider than a rendered timestamp would be only if expansion ran;
        # more importantly it must contain ink for every character, never an empty/expanded line.
        self.assertGreater(literal.width, 100)

    def test_temp_files_are_cleaned_up(self):
        tmp = tempfile.gettempdir()
        before = {n for n in os.listdir(tmp) if n.startswith("pai_text_")}
        text_render.render_text(FFMPEG, BURMESE, FONT, 40)
        text_render.render_text(FFMPEG, "", FONT, 40)
        text_render.render_text(FFMPEG, "x", os.path.join(ROOT, "no-such-font.ttf"), 40)
        after = {n for n in os.listdir(tmp) if n.startswith("pai_text_")}
        self.assertEqual(after - before, set())

    def test_unusable_input_returns_none(self):
        self.assertIsNone(text_render.render_text(FFMPEG, "", FONT, 40))
        self.assertIsNone(text_render.render_text(FFMPEG, "   \n ", FONT, 40))
        self.assertIsNone(text_render.render_text(FFMPEG, "x", os.path.join(ROOT, "no-such-font.ttf"), 40))
        self.assertIsNone(text_render.render_text("", "x", FONT, 40))
        self.assertIsNone(text_render.render_text(os.path.join(ROOT, "no-such-ffmpeg.exe"), "x", FONT, 40))


@unittest.skipUnless(CAN_RENDER, "FFmpeg drawtext or the Padauk font is not available")
class DrawTextAndLinesTests(unittest.TestCase):
    def test_draw_text_falls_back_to_pillow_when_ffmpeg_is_broken(self):
        img = text_render.draw_text(os.path.join(ROOT, "no-such-ffmpeg.exe"), "Pai AI", FONT, 40)
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 20)

    def test_draw_text_on_empty_text_is_none(self):
        self.assertIsNone(text_render.draw_text(FFMPEG, "  ", FONT, 40))

    def test_render_lines_stacks_and_centres(self):
        one = text_render.draw_text(FFMPEG, "Like & Follow လုပ်ထားပေးကြပါဦးခင်ဗျား", FONT, 32)
        two = text_render.draw_text(FFMPEG, "နောက်ထပ်", FONT, 32)
        stacked = text_render.render_lines(
            FFMPEG, ["နောက်ထပ်", "Like & Follow လုပ်ထားပေးကြပါဦးခင်ဗျား"], FONT, 32, spacing=10
        )
        self.assertEqual(stacked.width, max(one.width, two.width))
        self.assertEqual(stacked.height, one.height + two.height + 10)
        # Centre alignment: the narrow first line has empty margins either side.
        first_row = stacked.crop((0, 0, stacked.width, two.height)).getchannel("A").getbbox()
        self.assertGreater(first_row[0], 0)
        self.assertLess(first_row[2], stacked.width)

    def test_render_lines_skips_blank_lines_and_returns_none_when_nothing_to_draw(self):
        self.assertIsNone(text_render.render_lines(FFMPEG, ["", "  "], FONT, 32))
        single = text_render.render_lines(FFMPEG, ["", "Pai"], FONT, 32)
        self.assertEqual(single.height, text_render.draw_text(FFMPEG, "Pai", FONT, 32).height)


@unittest.skipUnless(CAN_RENDER, "FFmpeg drawtext or the Padauk font is not available")
class WatermarkIntegrationTests(unittest.TestCase):
    """The watermark builder must use the shaped renderer for the user's Burmese text."""

    def setUp(self):
        self._cwd = os.getcwd()
        self.work = tempfile.mkdtemp(prefix="pai_wm_")
        os.chdir(ROOT)  # the merger resolves assets/fonts relative to the working directory
        from agents.video_merger_agent import VideoMergerAgent
        self.agent = VideoMergerAgent(output_dir=self.work)

    def tearDown(self):
        os.chdir(self._cwd)
        import shutil
        shutil.rmtree(self.work, ignore_errors=True)
        wm = os.path.join(ROOT, "temp", "watermark.png")
        if os.path.exists(wm):
            os.remove(wm)

    def _build(self, **kw):
        from PIL import Image
        path = self.agent._create_watermark_image(text=BURMESE, font_size=30, **kw)
        self.assertTrue(os.path.exists(path))
        with Image.open(path) as im:
            return im.convert("RGBA")

    def test_badge_has_pill_background_and_white_text(self):
        img = self._build(style="badge", opacity=1.0)
        self.assertGreater(img.width, 100)
        pixels = list(img.getdata())
        self.assertTrue(any(p[:3] == (255, 255, 255) and p[3] == 255 for p in pixels), "white glyph pixels")
        self.assertTrue(any(p[:3] == (18, 18, 18) for p in pixels), "dark pill background")

    def test_transparent_style_has_shadow_and_no_background(self):
        img = self._build(style="transparent", opacity=1.0)
        pixels = list(img.getdata())
        self.assertTrue(any(p[:3] == (255, 255, 255) and p[3] == 255 for p in pixels), "white glyph pixels")
        self.assertTrue(any(p[:3] == (0, 0, 0) and 0 < p[3] <= 200 for p in pixels), "soft black shadow")
        self.assertEqual(img.getpixel((0, 0))[3], 0, "corner is transparent")

    def test_watermark_opacity_is_applied(self):
        img = self._build(style="transparent", opacity=0.5)
        self.assertLessEqual(max(p[3] for p in img.getdata()), 128)


if __name__ == "__main__":
    unittest.main()
