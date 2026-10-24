"""
Output quality presets: the 480p *draft* quality is understood everywhere a preset is, and a resumed run
compares the quality the user chose (not the size of the source video) when it decides what to render again.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.master import MasterAgent, PHASE_6_MERGE, PHASE_7_QA, PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE  # noqa: E402
from core import options as option_model  # noqa: E402
from core import resolution as res  # noqa: E402
from core.short_ass import geometry  # noqa: E402
from core.subtitle_builder import build_ass_script  # noqa: E402
from hardsub_engine import HardsubEngine  # noqa: E402


class PresetTests(unittest.TestCase):
    def test_normalize_accepts_the_usual_spellings(self):
        for value in ("480p", "480P", " 480 ", 480):
            self.assertEqual(res.normalize(value), "480p")
        self.assertEqual(res.normalize("720"), "720p")
        self.assertEqual(res.normalize(None), "1080p")
        self.assertEqual(res.normalize("4k"), "1080p")
        self.assertEqual(res.normalize("4k", default="720p"), "720p")

    def test_canvas_sizes_are_even_and_oriented(self):
        self.assertEqual(res.canvas("9:16", "1080p"), (1080, 1920))
        self.assertEqual(res.canvas("16:9", "720p"), (1280, 720))
        self.assertEqual(res.canvas("9:16", "480p"), (480, 854))
        self.assertEqual(res.canvas("16:9", "480p"), (854, 480))
        self.assertEqual(res.canvas("both", "480p"), (854, 480))        # anything but 9:16 is landscape
        for preset in res.PRESETS:
            for fmt in ("9:16", "16:9"):
                width, height = res.canvas(fmt, preset)
                self.assertEqual((width % 2, height % 2), (0, 0), "H.264 needs even dimensions")

    def test_the_draft_has_the_fewest_pixels(self):
        pixels = {p: res.canvas("16:9", p)[0] * res.canvas("16:9", p)[1] for p in res.PRESETS}
        self.assertLess(pixels["480p"], pixels["720p"])
        self.assertLess(pixels["720p"], pixels["1080p"])
        self.assertGreater(pixels["1080p"] / pixels["480p"], 4.5)
        self.assertTrue(res.is_draft("480p"))
        self.assertFalse(res.is_draft("720p"))

    def test_subtitle_metrics_cover_every_preset(self):
        for preset in res.PRESETS:
            for fmt in ("9:16", "16:9"):
                width, height, font, margin = res.subtitle_metrics(fmt, preset)
                self.assertEqual((width, height), res.canvas(fmt, preset))
                self.assertGreater(font, 0)
                self.assertLess(margin, height / 4)
        # the text keeps its share of the picture, within a small band, whatever the preset
        shares = [res.subtitle_metrics("9:16", p)[2] / res.canvas("9:16", p)[0] for p in res.PRESETS]
        self.assertLess(max(shares) - min(shares), 0.012)

    def test_unchanged_presets_keep_their_numbers(self):
        self.assertEqual(res.subtitle_metrics("9:16", "1080p"), (1080, 1920, 48, 140))
        self.assertEqual(res.subtitle_metrics("16:9", "720p"), (1280, 720, 36, 40))

    def test_the_option_model_offers_the_draft(self):
        spec = next(o for o in option_model.OPTIONS if o.name == "resolution")
        self.assertEqual(tuple(spec.choices), res.PRESETS)
        self.assertEqual(spec.default, "1080p")

    def test_short_ass_and_subtitle_builder_know_480p(self):
        self.assertEqual(geometry("9:16", "480p")[:2], (480, 854))
        self.assertEqual(geometry("16:9", "480p")[:2], (854, 480))
        ass = build_ass_script([], video_w=480, video_h=854, resolution="480p")
        self.assertIn("PlayResX: 480", ass)
        self.assertIn(",20,", ass)       # the 480p portrait font size, smaller than the 720p one (28)


class HardsubCanvasTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.engine = HardsubEngine(output_base_dir=self.temp_dir)
        self.video_in = os.path.join(self.temp_dir, "in.mp4")
        self.ass = os.path.join(self.temp_dir, "sub.ass")
        for path in (self.video_in, self.ass):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")

    def filter_for(self, aspect, resolution):
        meta = {"duration": 10.0, "fps": 30.0, "width": 1920, "height": 1080, "has_audio": True}
        with patch.object(self.engine, "_probe_video_metadata", return_value=meta), patch("subprocess.run") as run:
            self.engine._render_hardsub_video(
                video_path=self.video_in, output_path=os.path.join(self.temp_dir, "out.mp4"), ass_path=self.ass,
                blur_info=(0.82, 0.18, False), aspect_ratio=aspect, resolution=resolution, color_grading=False)
        cmd = run.call_args[0][0]
        return cmd[cmd.index("-filter_complex") + 1]

    def test_the_draft_renders_a_small_canvas(self):
        self.assertIn("scale=854:480", self.filter_for("16:9", "480p"))
        self.assertIn("scale=480:854", self.filter_for("9:16", "480p"))

    def test_the_other_presets_are_untouched(self):
        self.assertIn("scale=1920:1080", self.filter_for("16:9", "1080p"))
        self.assertIn("scale=720:1280", self.filter_for("9:16", "720p"))


class ResumeComparesThePresetTests(unittest.TestCase):
    """A finished run is resumed: only a real change of quality may throw the finished render away."""

    OPTIONS = dict(translation_style="recap", tts_voice="my-MM-ThihaNeural")

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_resolution_resume_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import brain.config as cfg
        cfg._config_cache = None
        os.environ["CURRENT_JOB_CANCELLED"] = "0"
        self.addCleanup(self._restore)

        first = MasterAgent("clip.mp4", resolution="720p", **self.OPTIONS)
        first.state.duration = "00:00:15"
        first.state.resolution = "720x1280"          # what Phase 1 stores: the size of the SOURCE video
        first.state.generated_script = [{"scene_id": "1", "narration": "စာကြောင်း", "start_sec": 0.0, "end_sec": 4.0}]
        for phase in (PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA):
            first.save_checkpoint(phase, phase)

    def _restore(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        import brain.config as cfg
        cfg._config_cache = None

    def test_the_chosen_preset_is_stored_apart_from_the_source_size(self):
        again = MasterAgent("clip.mp4", resolution="720p", **self.OPTIONS)
        self.assertEqual(again.state.output_resolution, "720p")

    def test_resuming_with_the_same_quality_keeps_the_finished_render(self):
        again = MasterAgent("clip.mp4", resolution="720p", **self.OPTIONS)
        self.assertIn(PHASE_6_MERGE, again.state.completed_phases)
        self.assertIn(PHASE_7_QA, again.state.completed_phases)

    def test_choosing_another_quality_renders_again(self):
        again = MasterAgent("clip.mp4", resolution="480p", **self.OPTIONS)
        self.assertNotIn(PHASE_6_MERGE, again.state.completed_phases)
        self.assertNotIn(PHASE_7_QA, again.state.completed_phases)
        self.assertIn(PHASE_5_VOICE, again.state.completed_phases)      # the voice is not touched by the quality

    def test_a_state_from_before_the_fix_is_not_thrown_away(self):
        import json
        path = os.path.join("outputs", MasterAgent("clip.mp4", **self.OPTIONS).state.project_dir, "state.json")
        data = json.load(open(path, encoding="utf-8"))
        data.pop("output_resolution", None)
        json.dump(data, open(path, "w", encoding="utf-8"))
        again = MasterAgent("clip.mp4", resolution="720p", **self.OPTIONS)
        self.assertIn(PHASE_6_MERGE, again.state.completed_phases)


if __name__ == "__main__":
    unittest.main()
