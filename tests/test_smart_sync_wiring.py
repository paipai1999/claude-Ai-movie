"""
Smart Sync, the way it is wired into the rest of the studio: the option (CLI, API, workers), the master agent (when it
is allowed, what a change of mode invalidates), the voice step (clips are not squeezed into their scene), the QA step
(the schedule uses the times of the re-timed film) and the project's quality report.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.master import MasterAgent, PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA  # noqa: E402
from agents.qa_agent import QAAgent  # noqa: E402
from agents.voice_agent import VoiceAgent  # noqa: E402
from brain.memory import MovieState  # noqa: E402
from core import options as option_model  # noqa: E402
from core import smart_sync  # noqa: E402
from core.cli import MAIN, HARDSUB, SPLITTER, SUBTITLE, add_option_arguments, resolve_args  # noqa: E402
from services import projects  # noqa: E402
from tests import test_tts_guard as guard  # noqa: E402

BASE = dict(translation_style="recap", tts_voice="my-MM-ThihaNeural")


class OptionTests(unittest.TestCase):
    def test_the_option_is_in_the_model_for_the_recap_only(self):
        spec = next(o for o in option_model.OPTIONS if o.name == "sync_mode")
        self.assertEqual(tuple(spec.choices), smart_sync.MODES)
        self.assertEqual(spec.default, "audio_fit")
        self.assertEqual(set(spec.engines), {"recap"})
        self.assertIn("sync_mode", [o.name for o in option_model.options_for("recap")])
        self.assertNotIn("sync_mode", [o.name for o in option_model.options_for("hardsub")])

    def test_the_command_line_takes_it_for_the_recap_program_only(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("input")
        add_option_arguments(parser, MAIN)
        self.assertEqual(resolve_args(MAIN, parser.parse_args(["film.mp4", "--sync-mode", "smart"]))["sync_mode"], "smart")
        self.assertEqual(resolve_args(MAIN, parser.parse_args(["film.mp4"]))["sync_mode"], "audio_fit")
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            parser.parse_args(["film.mp4", "--sync-mode", "nonsense"])
        for program in (HARDSUB, SUBTITLE, SPLITTER):
            other = argparse.ArgumentParser()
            other.add_argument("input")
            add_option_arguments(other, program)
            self.assertNotIn("--sync-mode", other.format_help())

    def test_the_new_worker_parameter_is_last_so_queued_jobs_still_load(self):
        import inspect
        from services import job_runtime as rt
        for worker in (rt.pipeline_worker, rt.batch_worker):
            names = list(inspect.signature(worker).parameters)
            self.assertEqual(names[-1], "sync_mode", worker.__name__)
            self.assertEqual(names[-2], "delivery_mode", worker.__name__)

    def test_the_pipeline_worker_hands_it_to_the_master_agent(self):
        from services import job_runtime as rt
        seen = {}

        class Master:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)
                self.output_dir, self.state, self.package_info = "outputs", SimpleNamespace(project_dir="p"), None

            def run_pipeline(self):
                pass

        folder = tempfile.mkdtemp(prefix="pai_smart_worker_")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        film = os.path.join(folder, "film.mp4")
        open(film, "wb").close()
        with mock.patch.object(rt, "MasterAgent", Master), mock.patch.object(rt, "_run_job", side_effect=lambda j, s, t, work: work()), \
                mock.patch.object(rt, "_set_job_state"), mock.patch.object(rt, "project_store"):
            rt.pipeline_worker("job1", film, sync_mode="smart")
        self.assertEqual(seen["sync_mode"], "smart")


class MasterTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_smart_master_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import brain.config as cfg
        cfg._config_cache = None
        os.environ["CURRENT_JOB_CANCELLED"] = "0"
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        import brain.config as cfg
        cfg._config_cache = None

    def master(self, **kwargs):
        return MasterAgent("clip.mp4", **dict(BASE, **kwargs))

    def test_the_default_is_audio_fit(self):
        agent = self.master()
        self.assertEqual(agent.sync_mode, "audio_fit")
        self.assertEqual(agent.state.sync_mode, "audio_fit")
        self.assertEqual(agent.voice_agent.sync_mode, "audio_fit")
        self.assertEqual(agent.state.warnings, [])

    def test_smart_is_accepted_for_the_one_click_video(self):
        agent = self.master(sync_mode="smart", delivery_mode="oneclick")
        self.assertEqual(agent.sync_mode, "smart")
        self.assertEqual(agent.state.sync_mode, "smart")
        self.assertEqual(agent.voice_agent.sync_mode, "smart")
        self.assertEqual(agent.state.warnings, [])

    def test_smart_is_switched_off_for_the_capcut_pipeline_with_a_reason(self):
        for mode in ("capcut", "both"):
            agent = self.master(sync_mode="smart", delivery_mode=mode)
            self.assertEqual(agent.sync_mode, "audio_fit", mode)
            self.assertEqual(agent.voice_agent.sync_mode, "audio_fit", mode)
            self.assertTrue(any("Smart Sync was switched off" in w for w in agent.state.warnings), mode)

    def test_smart_is_switched_off_when_the_video_is_not_rendered(self):
        agent = self.master(sync_mode="smart", render_video=False)
        self.assertEqual(agent.sync_mode, "audio_fit")
        self.assertTrue(agent.state.warnings)

    def test_an_unknown_mode_is_audio_fit(self):
        self.assertEqual(self.master(sync_mode="turbo").sync_mode, "audio_fit")
        self.assertEqual(self.master(sync_mode=None).sync_mode, "audio_fit")

    def test_the_voice_signature_changes_only_for_smart(self):
        plain = self.master()._voice_signature()
        self.assertEqual(self.master(sync_mode="audio_fit")._voice_signature(), plain)
        self.assertNotEqual(self.master(sync_mode="smart")._voice_signature(), plain)

    def test_changing_the_mode_voices_the_film_again(self):
        first = self.master()
        first.state.duration = "00:00:15"
        first.state.generated_script = [{"scene_id": "1", "narration": "စာကြောင်း", "start_sec": 0.0, "end_sec": 4.0}]
        for phase in (PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE, PHASE_7_QA):
            first.save_checkpoint(phase, phase)
        again = self.master()
        self.assertIn(PHASE_5_VOICE, again.state.completed_phases)           # the same options: everything is reused
        smart = self.master(sync_mode="smart")
        self.assertNotIn(PHASE_5_VOICE, smart.state.completed_phases)        # clips fitted to scenes are not the clips of a smart run
        self.assertNotIn(PHASE_6_MERGE, smart.state.completed_phases)
        self.assertIn(PHASE_4_SCRIPT, smart.state.completed_phases)          # the script is not touched

    def test_the_sync_plan_of_a_finished_smart_run_comes_back_on_resume_and_not_for_another_mode(self):
        first = self.master(sync_mode="smart")
        first.state.duration = "00:00:15"
        first.state.generated_script = [{"scene_id": "1", "narration": "စာကြောင်း", "start_sec": 0.0, "end_sec": 4.0}]
        first.state.sync_plan = smart_sync.plan([(1.0, 3.0, 4.0)], 10.0).to_dict()
        for phase in (PHASE_1_ANALYSIS, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE):
            first.save_checkpoint(phase, phase)
        again = self.master(sync_mode="smart")
        self.assertEqual(again.state.sync_plan["summary"]["scenes"], 1)
        self.assertIn(PHASE_6_MERGE, again.state.completed_phases)
        self.assertIsNone(self.master().state.sync_plan)                     # back to audio_fit: the old plan is not carried over


class VoiceTests(guard.GuardCase):
    """The voice step with the guard tests' fake Edge service and real FFmpeg."""

    def duration(self, name):
        import imageio_ffmpeg
        shown = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", os.path.join(self.out, name), "-hide_banner"],
                               capture_output=True, text=True).stderr
        h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", shown).groups()
        return int(h) * 3600 + int(m) * 60 + float(s)

    def test_a_clip_is_fitted_to_its_scene_by_default_and_left_natural_under_smart_sync(self):
        self.state.generated_script = self.state.generated_script[:1]       # a 1 s clip for a 4 s slot
        self.agent.sync_mode = "audio_fit"
        self.agent.generate_voiceover(self.state)
        fitted = self.duration("scene_0001.mp3")
        shutil.rmtree(self.out)
        voice_agent_module = sys.modules["agents.voice_agent"]
        voice_agent_module._tts_verified_at.clear()
        self.agent.sync_mode = "smart"
        self.agent.generate_voiceover(self.state)
        natural = self.duration("scene_0001.mp3")
        self.assertGreater(fitted, natural + 0.08)                           # stretched slower to 1/1.15 = 0.87x
        self.assertAlmostEqual(natural, 1.0, delta=0.12)                     # the tone as the service made it

    def test_the_fingerprint_of_a_smart_clip_differs_and_the_default_is_unchanged(self):
        import hashlib
        item = {"narration": "စာကြောင်း", "gender": "Male", "emotion": "Sad"}
        self.agent.sync_mode = "audio_fit"
        plain = self.agent._clip_fingerprint(item)
        expected = hashlib.sha1("|".join(["edge_tts", "my-MM-ThihaNeural", "male", "sad", "စာကြောင်း"]).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(plain, expected)
        self.agent.sync_mode = "smart"
        self.assertNotEqual(self.agent._clip_fingerprint(item), plain)

    def test_changing_the_mode_re_voices_the_clips(self):
        self.agent.sync_mode = "audio_fit"
        self.agent.generate_voiceover(self.state)
        self.fake.calls.clear()
        self.agent.sync_mode = "smart"
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.speak_calls()), 4)

    def test_an_agent_made_without_init_defaults_to_audio_fit(self):
        self.assertEqual(VoiceAgent.__new__(VoiceAgent).sync_mode, "audio_fit")


class QATests(unittest.TestCase):
    def schedule(self, state):
        seen = {}

        def ask(file_name, system, prompt, key, **kwargs):
            seen["prompt"] = prompt
            return "{}"

        with mock.patch("agents.qa_agent.ask_gemini_with_video", side_effect=ask):
            QAAgent(output_dir=tempfile.gettempdir())._run_sync_check_with_file("files/x", "k", state, "m")
        return seen["prompt"]

    def state(self, plan=None):
        state = MovieState(movie_name="Film")
        state.generated_script = [{"scene_id": "1", "narration": "one", "start_sec": 10.0, "end_sec": 14.0},
                                  {"scene_id": "2", "narration": "two", "start_sec": 30.0, "end_sec": 34.0}]
        state.sync_plan = plan.to_dict() if plan else None
        return state

    def test_without_smart_sync_the_schedule_has_the_scripts_own_times(self):
        text = self.schedule(self.state())
        self.assertIn("[10.0s-14.0s] one", text)
        self.assertIn("[30.0s-34.0s] two", text)

    def test_under_smart_sync_it_has_the_times_of_the_film_the_reviewer_watches(self):
        plan = smart_sync.plan([(10.0, 14.0, 6.0), (30.0, 34.0, 4.0)], 60.0)       # the first scene is slowed: 6 s for 4 s
        text = self.schedule(self.state(plan))
        self.assertIn(f"[{plan.to_output(10.0):.1f}s-{plan.to_output(14.0):.1f}s] one", text)
        self.assertIn(f"[{plan.to_output(30.0):.1f}s-{plan.to_output(34.0):.1f}s] two", text)
        self.assertNotIn("[30.0s-34.0s]", text)                                    # the second scene moved 2 s later


class QualityReportTests(unittest.TestCase):
    def findings(self, state):
        script = [{"scene_id": "1", "narration": "ဒီစာကြောင်း", "start_sec": 0.0, "end_sec": 4.0, "source_text": "x"}]
        with mock.patch.object(projects, "_clip_durations", return_value={0: 20.0}):        # a 20 s clip for a 4 s scene
            return projects._recap_findings(tempfile.gettempdir(), script, {"warnings": [], "errors": []}, state)

    def test_a_clip_longer_than_its_scene_is_an_overflow_without_smart_sync(self):
        report = self.findings({})
        self.assertEqual(report["voice"]["overflowing_clips"], 1)
        self.assertNotIn("sync", report)

    def test_under_smart_sync_it_is_not_and_the_summary_is_shown(self):
        plan = smart_sync.plan([(0.0, 4.0, 20.0)], 30.0)
        report = self.findings({"sync_mode": "smart", "sync_plan": plan.to_dict()})
        self.assertEqual(report["voice"]["overflowing_clips"], 0)
        self.assertEqual(report["sync"]["mode"], "smart")
        self.assertEqual(report["sync"]["scenes"], 1)

    def test_a_smart_state_without_a_plan_keeps_the_old_check(self):
        self.assertEqual(self.findings({"sync_mode": "smart"})["voice"]["overflowing_clips"], 1)


if __name__ == "__main__":
    unittest.main()
