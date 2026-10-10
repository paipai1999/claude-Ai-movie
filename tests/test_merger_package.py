"""
Tests for the agents/merger package split and the merge_video orchestration:
  - public surface of agents.video_merger_agent is unchanged
  - every original method is still reachable on VideoMergerAgent
  - a failed FFmpeg single pass now falls back to MoviePy, and fails loudly when nothing is produced
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.video_merger_agent import VideoMergerAgent
from agents.merger.plan import MergePlan

ORIGINAL_METHODS = [
    "merge_video", "_prepend_thumbnail_intro_ffmpeg", "_append_outro_card_ffmpeg", "_legacy_moviepy_merge",
    "_create_watermark_image", "_sec_to_ass_ts", "_wrap_burmese_text", "_chunk_burmese_narration", "_write_ass",
    "_find_myanmar_font", "_build_ass_filter_str", "_export_standalone_srt",
    "_detect_subtitle_region_with_vision", "_blur_subtitle_region", "generate_reels_video",
]


class TestPackageSurface(unittest.TestCase):

    def test_public_helpers_are_still_importable_from_the_facade(self):
        import agents.video_merger_agent as facade
        for name in ("VideoMergerAgent", "detect_hardware_encoder", "_get_ffmpeg_bin", "_get_safe_ascii_id",
                     "_get_audio_duration", "_get_video_info", "_has_audio_stream", "_assemble_voiceover_track"):
            self.assertTrue(hasattr(facade, name), name)

    def test_every_original_method_is_reachable(self):
        for name in ORIGINAL_METHODS:
            self.assertTrue(callable(getattr(VideoMergerAgent, name, None)), name)

    def test_new_step_methods_exist(self):
        for name in ("_build_merge_plan", "_collect_voiceover_clips", "_layout_voiceover_clips",
                     "_resolve_render_duration", "_assemble_voiceover", "_select_background_audio",
                     "_prepare_subtitles", "_prepare_watermark", "_plan_thumbnail_intro", "_plan_subtitle_blur",
                     "_render_single_pass", "_apply_intro_outro"):
            self.assertTrue(callable(getattr(VideoMergerAgent, name, None)), name)

    def test_merge_plan_defaults_are_independent_per_instance(self):
        a, b = MergePlan(), MergePlan()
        a.audio_items.append(("x",))
        self.assertEqual(b.audio_items, [])


class TestMergeOrchestration(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_merge_")
        self.agent = VideoMergerAgent(output_dir=self.work)
        self.state = MagicMock()
        self.plan = MergePlan(output_dir=self.work, final_output=os.path.join(self.work, "final_recap.mp4"))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_successful_single_pass_skips_the_fallback_and_adds_intro_outro(self):
        with patch.object(self.agent, "_build_merge_plan", return_value=self.plan), \
             patch.object(self.agent, "_render_single_pass", return_value=True), \
             patch.object(self.agent, "_apply_intro_outro") as intro_outro, \
             patch.object(self.agent, "_legacy_moviepy_merge") as legacy:
            result = self.agent.merge_video(self.state, "movie.mp4")
        self.assertIs(result, self.state)
        intro_outro.assert_called_once()
        legacy.assert_not_called()

    def test_failed_single_pass_falls_back_to_moviepy(self):
        def fake_legacy(*args, **kwargs):
            open(self.plan.final_output, "wb").write(b"\0" * 5000)   # the fallback produced a video
            return self.state

        with patch.object(self.agent, "_build_merge_plan", return_value=self.plan), \
             patch.object(self.agent, "_render_single_pass", return_value=False), \
             patch.object(self.agent, "_apply_intro_outro") as intro_outro, \
             patch.object(self.agent, "_legacy_moviepy_merge", side_effect=fake_legacy) as legacy:
            result = self.agent.merge_video(self.state, "movie.mp4")
        legacy.assert_called_once()
        intro_outro.assert_not_called()
        self.assertIs(result, self.state)

    def test_no_video_from_any_renderer_fails_the_job(self):
        with patch.object(self.agent, "_build_merge_plan", return_value=self.plan), \
             patch.object(self.agent, "_render_single_pass", return_value=False), \
             patch.object(self.agent, "_legacy_moviepy_merge", return_value=self.state):
            with self.assertRaises(RuntimeError) as ctx:
                self.agent.merge_video(self.state, "movie.mp4")
        self.assertIn("final_recap.mp4", str(ctx.exception))

    def test_single_pass_without_ffmpeg_binary_reports_failure(self):
        with patch("agents.merger.single_pass._get_ffmpeg_bin", return_value=None):
            self.assertFalse(self.agent._render_single_pass(self.plan, self.state, "movie.mp4"))

    def test_stop_during_render_propagates_instead_of_falling_back(self):
        cancelled = MagicMock(returncode=0, stderr="")
        os.environ["CURRENT_JOB_CANCELLED"] = "1"
        try:
            with patch("agents.merger.single_pass._get_ffmpeg_bin", return_value="ffmpeg"), \
                 patch("agents.merger.single_pass.detect_hardware_encoder",
                       return_value={"codec": "libx264", "preset": "faster", "type": "cpu"}), \
                 patch("agents.merger.single_pass.subprocess.run", return_value=cancelled):
                with self.assertRaises(InterruptedError):
                    self.agent._render_single_pass(self.plan, self.state, "movie.mp4")
        finally:
            os.environ["CURRENT_JOB_CANCELLED"] = "0"


class TestFiltergraphBuilders(unittest.TestCase):
    """The filter chains are pure string builders: lock down the invariants the renderer relies on."""

    def setUp(self):
        self.agent = VideoMergerAgent()
        self.state = MagicMock(mirror=False, audio_mode="ai_voiceover", sfx_volume=None, audio_anti_copyright=False)

    def _plan(self, **kw):
        plan = MergePlan(effective_video_dur=60.0, video_fps=24.0)
        for key, value in kw.items():
            setattr(plan, key, value)
        return plan

    def test_video_chain_always_splits_a_clean_stream_before_watermark(self):
        parts, out = self.agent._video_filter_parts(self._plan(), self.state, include_blur=False, wm_input_idx=None, has_ass=False)
        joined = ";".join(parts)
        self.assertIn("[v_for_wm][v_for_clean]", joined)
        self.assertEqual(out, "[v_for_wm]")

    def test_video_chain_adds_blur_mirror_watermark_and_ass_on_demand(self):
        plan = self._plan(subtitle_found=True, wm_pos="top_left", wm_margin=10, target_ass_path="x.ass")
        self.state.mirror = True
        parts, out = self.agent._video_filter_parts(plan, self.state, include_blur=True, wm_input_idx=2, has_ass=True)
        joined = ";".join(parts)
        self.assertIn("hflip", joined)
        self.assertIn("boxblur", joined)
        self.assertIn("[v_for_wm][2:v]overlay=10:10[v_wm_done]", joined)
        self.assertEqual(out, "[v_subbed]")

    def test_audio_chain_always_ends_with_the_two_master_labels(self):
        cases = [
            dict(bg_source_type="none"),
            dict(bg_source_type="orig"),
            dict(bg_source_type="demucs"),
            dict(bg_source_type="bgm"),
            dict(bg_source_type="both"),
        ]
        for kw in cases:
            for vo_idx in (None, 1):
                parts = self.agent._audio_filter_parts(self._plan(**kw), self.state, vo_idx, 2, 3)
                self.assertIn("[a_master1][a_master2]", parts[-1], (kw, vo_idx))

    def test_anti_copyright_tempo_only_when_requested(self):
        self.state.audio_anti_copyright = True
        with_tempo = ";".join(self.agent._audio_filter_parts(self._plan(), self.state, 1, None, None))
        self.assertIn("atempo=1.008", with_tempo)
        self.state.audio_anti_copyright = False
        without = ";".join(self.agent._audio_filter_parts(self._plan(), self.state, 1, None, None))
        self.assertNotIn("atempo", without)


if __name__ == "__main__":
    unittest.main()
