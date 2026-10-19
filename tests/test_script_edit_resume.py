"""
The Script Editor's promise, checked against the real MasterAgent / VoiceAgent resume logic:

    edit a line -> run the project again (resume) -> the edited script is kept, the script phase is
    skipped, ONLY the voice / merge / QA work is redone, and only the edited line's clip is re-voiced.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.master import (  # noqa: E402
    MasterAgent, PHASE_1_ANALYSIS, PHASE_2_AUDIO, PHASE_3_SCENES, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA,
)
from agents.voice_agent import VOICE_MANIFEST_NAME, VoiceAgent  # noqa: E402
from services import projects as pj  # noqa: E402

OPTIONS = dict(translation_style="recap", tts_voice="my-MM-ThihaNeural", context_hint="hero is John")
ALL_PHASES = [PHASE_1_ANALYSIS, PHASE_2_AUDIO, PHASE_3_SCENES, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA]


class EditThenResume(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_edit_resume_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import brain.config as cfg
        cfg._config_cache = None
        os.environ["CURRENT_JOB_CANCELLED"] = "0"
        self.addCleanup(self._restore)

        # ── a finished first run ──
        first = MasterAgent("clip.mp4", **OPTIONS)
        first.state.generated_script = [
            {"scene_id": str(i + 1), "narration": f"စာကြောင်းနံပါတ် {i + 1} ဖြစ်ပါတယ်", "start_sec": i * 5.0, "end_sec": i * 5.0 + 4.0,
             "gender": "male", "emotion": "normal", "source_text": f"line {i + 1}"}
            for i in range(3)
        ]
        first.state.seo_metadata = {"title": "t"}
        first.state.duration = "00:00:15"
        first.state.resolution = "1920x1080"
        self.project = os.path.join("outputs", first.state.project_dir)
        self.vo_dir = os.path.join(self.project, "voiceover")
        os.makedirs(self.vo_dir)

        agent = VoiceAgent.__new__(VoiceAgent)
        agent.engine, agent.voice = "edge_tts", "my-MM-ThihaNeural"
        manifest = {}
        for i, block in enumerate(first.state.generated_script):
            with open(os.path.join(self.vo_dir, f"scene_{i + 1:04d}.mp3"), "wb") as fh:
                fh.write(b"\x00" * 4000)
            manifest[f"scene_{i + 1:04d}.mp3"] = agent._clip_fingerprint(block)
        with open(os.path.join(self.vo_dir, VOICE_MANIFEST_NAME), "w", encoding="utf-8") as fh:
            json.dump({"clips": manifest}, fh)
        with open(os.path.join(self.project, "final_recap.mp4"), "wb") as fh:
            fh.write(b"\x00" * 60000)
        for phase in ALL_PHASES:
            first.save_checkpoint(phase, phase)
        self.agent = agent

    def _restore(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        import brain.config as cfg
        cfg._config_cache = None

    def test_the_edit_survives_resume_and_only_voice_work_is_redone(self):
        # sanity: before the edit a resume skips the voice phase
        again = MasterAgent("clip.mp4", **OPTIONS)
        self.assertIn(PHASE_5_VOICE, again.state.completed_phases)

        result = pj.save_script(self.project, [{"index": 1, "narration": "ဒုတိယစာကြောင်းကို ပြင်လိုက်ပါပြီ"}])
        self.assertEqual(result["changed"], 1)

        resumed = MasterAgent("clip.mp4", **OPTIONS)
        # the edited script is what the new run starts from (not regenerated, not reverted)
        self.assertEqual(resumed.state.generated_script[1]["narration"], "ဒုတိယစာကြောင်းကို ပြင်လိုက်ပါပြီ")
        self.assertTrue(resumed.state.generated_script[1]["edited"])
        self.assertEqual(resumed.state.generated_script[0]["narration"], "စာကြောင်းနံပါတ် 1 ဖြစ်ပါတယ်")

        # phases: script work is kept, everything after it is redone
        for phase in (PHASE_1_ANALYSIS, PHASE_2_AUDIO, PHASE_3_SCENES, PHASE_4_SCRIPT):
            self.assertIn(phase, resumed.state.completed_phases)
        for phase in (PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA):
            self.assertNotIn(phase, resumed.state.completed_phases)
        self.assertTrue(resumed._should_run_phase(PHASE_5_VOICE))
        self.assertTrue(resumed._should_run_phase(PHASE_6_MERGE))
        # the render phases never come back through the checkpoint file
        self.assertNotIn(PHASE_6_MERGE, resumed.state.completed_phases)

    def test_only_the_edited_clip_is_resynthesized(self):
        pj.save_script(self.project, [{"index": 1, "narration": "ဒုတိယစာကြောင်းကို ပြင်လိုက်ပါပြီ"}])
        resumed = MasterAgent("clip.mp4", **OPTIONS)
        self.agent._purge_stale_clips(self.vo_dir, resumed.state.generated_script)
        left = sorted(n for n in os.listdir(self.vo_dir) if n.endswith(".mp3"))
        self.assertEqual(left, ["scene_0001.mp3", "scene_0003.mp3"])    # clip 2 is gone, the others are reused

    def test_a_gender_edit_also_revoices_that_line(self):
        pj.save_script(self.project, [{"index": 2, "gender": "female"}])
        resumed = MasterAgent("clip.mp4", **OPTIONS)
        self.agent._purge_stale_clips(self.vo_dir, resumed.state.generated_script)
        left = sorted(n for n in os.listdir(self.vo_dir) if n.endswith(".mp3"))
        self.assertEqual(left, ["scene_0001.mp3", "scene_0002.mp3"])

    def test_confirming_a_flag_without_changing_text_does_not_trigger_a_re_render(self):
        state_path = os.path.join(self.project, "state.json")
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        state["generated_script"][0].update(needs_review=True, issues=["duplicate"])
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        result = pj.save_script(self.project, [{"index": 0, "narration": state["generated_script"][0]["narration"]}])
        self.assertEqual((result["changed"], result["invalidated"]), (0, []))
        resumed = MasterAgent("clip.mp4", **OPTIONS)
        self.assertIn(PHASE_5_VOICE, resumed.state.completed_phases)

    def test_changed_script_options_would_discard_the_edit_which_is_why_options_are_saved(self):
        """Documents the hazard job_options.json exists for: a different context hint regenerates the script."""
        pj.save_script(self.project, [{"index": 1, "narration": "ဒုတိယစာကြောင်းကို ပြင်လိုက်ပါပြီ"}])
        other = MasterAgent("clip.mp4", **dict(OPTIONS, context_hint="hero is Mary"))
        self.assertEqual(other.state.generated_script, [])


if __name__ == "__main__":
    unittest.main()
