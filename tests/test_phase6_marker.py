"""
Switching a project from the CapCut pipeline back to the one-click render must really render.

The CapCut pipeline skips the video but still marks Phase 6 ("subtitles exported") as done. A video left on disk by an
earlier run - made with another voice, at another loudness - then passed the artifact check, and the next one-click run
"resumed" it: the user got the old video. Phase 6 now records that it rendered nothing, and a finished render that is
still valid is not thrown away by a CapCut run in between.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.master import MasterAgent, PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA  # noqa: E402

BASE = dict(translation_style="recap", video_format="16:9")
SCRIPT = [{"scene_id": "1", "narration": "စာကြောင်း", "start_sec": 0.0, "end_sec": 4.0}]


class Phase6MarkerTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_phase6_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import brain.config as cfg
        cfg._config_cache = None
        os.environ["CURRENT_JOB_CANCELLED"] = "0"
        self.addCleanup(self._restore)

        # a finished one-click run with the Thiha voice: a rendered video on disk
        first = MasterAgent("clip.mp4", tts_voice="my-MM-ThihaNeural", **BASE)
        first.state.duration = "00:00:15"
        first.state.generated_script = SCRIPT
        self.project = os.path.join("outputs", first.state.project_dir)
        os.makedirs(os.path.join(self.project, "voiceover"))
        with open(os.path.join(self.project, "voiceover", "scene_0001.mp3"), "wb") as fh:
            fh.write(b"\0" * 4000)
        with open(os.path.join(self.project, "final_recap.mp4"), "wb") as fh:
            fh.write(b"\0" * 60000)
        for phase in (PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA):
            first.save_checkpoint(phase, phase)

    def _restore(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        import brain.config as cfg
        cfg._config_cache = None

    def agent(self, voice, mode):
        agent = MasterAgent("clip.mp4", tts_voice=voice, delivery_mode=mode, **BASE)
        agent.video_merger = mock.MagicMock()
        agent.video_merger.merge_video.side_effect = lambda state, path: state
        return agent

    def test_a_valid_render_survives_a_capcut_run_with_the_same_options(self):
        capcut = self.agent("my-MM-ThihaNeural", "capcut")
        capcut._run_phase_6_merge()
        self.assertNotIn(PHASE_6_MERGE, capcut.state.phase_checkpoints)
        oneclick = self.agent("my-MM-ThihaNeural", "oneclick")
        self.assertFalse(oneclick._should_run_phase(PHASE_6_MERGE))       # the finished video is reused: nothing is rendered twice

    def test_a_capcut_run_with_another_voice_leaves_a_stale_video_that_is_not_reused(self):
        capcut = self.agent("gemini:Kore", "capcut")                       # the voice changed: Phase 6 was invalidated
        self.assertNotIn(PHASE_6_MERGE, capcut.state.completed_phases)
        capcut._run_phase_6_merge()
        self.assertIn(PHASE_6_MERGE, capcut.state.completed_phases)        # "done" for this run ...
        self.assertEqual(capcut.state.phase_checkpoints[PHASE_6_MERGE], {"rendered": False})   # ... but nothing was rendered
        self.assertTrue(os.path.exists(os.path.join(self.project, "final_recap.mp4")), "the old file is still there")

        oneclick = self.agent("gemini:Kore", "oneclick")
        self.assertIn(PHASE_6_MERGE, oneclick.state.completed_phases)
        self.assertTrue(oneclick._should_run_phase(PHASE_6_MERGE), "the old video must not be resumed")

    def test_the_marker_is_cleared_by_a_real_render(self):
        self.agent("gemini:Kore", "capcut")._run_phase_6_merge()
        oneclick = self.agent("gemini:Kore", "oneclick")
        oneclick._run_phase_6_merge()
        oneclick.video_merger.merge_video.assert_called_once()
        self.assertNotIn(PHASE_6_MERGE, oneclick.state.phase_checkpoints)
        again = self.agent("gemini:Kore", "oneclick")
        self.assertFalse(again._should_run_phase(PHASE_6_MERGE))           # now it is genuinely finished


if __name__ == "__main__":
    unittest.main()
