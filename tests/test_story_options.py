"""
story_length / narrative_tone / tts_rate: from the option model and the dashboard/CLI down to the prompt, the
cache signatures and the voice. The defaults must change nothing for projects made before these options existed.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.writer_agent import WriterAgent  # noqa: E402
from brain.memory import MovieState  # noqa: E402
from core import cli  # noqa: E402
from core import options as O  # noqa: E402


class OptionModelTests(unittest.TestCase):
    def test_options_exist_for_the_recap_engine_only_with_neutral_defaults(self):
        for name, default in (("story_length", "standard"), ("narrative_tone", "natural"), ("tts_rate", None)):
            opt = O.BY_NAME[name]
            self.assertEqual(opt.default, default)
            self.assertEqual(sorted(opt.engines), [O.RECAP])
        self.assertEqual(set(O.BY_NAME["story_length"].choices), {"standard", "compact", "detailed", "scene"})
        self.assertEqual(set(O.BY_NAME["narrative_tone"].choices), {"natural", "brisk", "calm", "dramatic"})

    def test_other_engines_never_see_them(self):
        for engine in (O.SUBTITLE, O.HARDSUB, O.DOWNLOAD):
            names = {o.name for o in O.options_for(engine)}
            self.assertFalse(names & {"story_length", "narrative_tone", "tts_rate"}, engine)

    def test_command_line_flags(self):
        parser = cli_parser()
        args = parser.parse_args(["--story-length", "compact", "--tone", "calm", "--tts-rate", "20"])
        self.assertEqual((args.story_length, args.narrative_tone, args.tts_rate), ("compact", "calm", 20))
        with self.assertRaises(SystemExit):
            cli_parser().parse_args(["--story-length", "huge"])
        resolved = cli.resolve_args(cli.MAIN, args)
        self.assertEqual((resolved["story_length"], resolved["narrative_tone"], resolved["tts_rate"]), ("compact", "calm", 20))
        untouched = cli.resolve_args(cli.MAIN, cli_parser().parse_args([]))
        self.assertEqual((untouched["story_length"], untouched["narrative_tone"], untouched["tts_rate"]), ("standard", "natural", None))


def cli_parser():
    import argparse
    parser = argparse.ArgumentParser()
    cli.add_option_arguments(parser, cli.MAIN)
    return parser


class WriterPromptTests(unittest.TestCase):
    def _writer(self, engine="recap"):
        return WriterAgent(language="burmese", script_engine=engine)

    def _state(self, **kw):
        return MovieState(movie_name="Film", **kw)

    def test_defaults_add_nothing(self):
        writer, state = self._writer(), self._state()
        self.assertEqual(writer._style_directive(state), "")
        plain = writer._build_batch_prompt([{"id": 1, "text": "Hi"}], state)[1]
        self.assertNotIn("NARRATION STYLE", plain)

    def test_each_length_and_tone_has_its_own_instruction(self):
        writer = self._writer()
        seen = set()
        for length in ("compact", "detailed", "scene"):
            text = writer._style_directive(self._state(story_length=length))
            self.assertIn("LENGTH", text)
            seen.add(text)
        for tone in ("brisk", "calm", "dramatic"):
            text = writer._style_directive(self._state(narrative_tone=tone))
            self.assertIn("TONE", text)
            seen.add(text)
        self.assertEqual(len(seen), 6)

    def test_length_and_tone_combine_and_reach_the_batch_prompt(self):
        writer, state = self._writer(), self._state(story_length="compact", narrative_tone="dramatic")
        directive = writer._style_directive(state)
        self.assertIn("COMPACT", directive)
        self.assertIn("DRAMATIC", directive)
        prompt = writer._build_batch_prompt([{"id": 1, "text": "Hi"}], state)[1]
        self.assertIn("NARRATION STYLE (follow strictly):", prompt)
        self.assertLess(prompt.index("NARRATION STYLE"), prompt.index("Write suspenseful"))   # before the task text

    def test_only_the_recap_storyteller_uses_them(self):
        state = self._state(story_length="detailed", narrative_tone="brisk")
        self.assertEqual(self._writer("translate")._style_directive(state), "")

    def test_unknown_values_are_ignored(self):
        self.assertEqual(self._writer()._style_directive(self._state(story_length="huge", narrative_tone="wild")), "")

    def test_the_single_line_fallback_prompt_carries_the_style_too(self):
        from agents import writer_agent
        captured = []

        def fake_gemini(system, prompt, key, model, **kw):
            captured.append((kw.get("response_mime_type"), prompt))
            return "မြန်မာဘာသာပြန်", model

        state = self._state(story_length="scene")
        with patch.object(writer_agent, "call_gemini", fake_gemini):
            writer = self._writer()
            segs = [{"id": 1, "text": "Hello", "start_sec": 0, "end_sec": 3, "max_chars": 40}]
            with patch.object(writer, "_parse_script", return_value=None):          # force the per-line path
                writer._translate_batches(segs, state, "", "key", "model")
        line_prompts = [p for mime, p in captured if mime == "text/plain"]
        self.assertTrue(line_prompts)
        self.assertIn("SCENE BY SCENE", line_prompts[0])


class MasterSignatureTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_story_opts_")
        self.old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(self._restore)
        import brain.config as cfg
        cfg._config_cache = None

    def _restore(self):
        os.chdir(self.old)
        shutil.rmtree(self.work, ignore_errors=True)

    def master(self, **kw):
        from agents.master import MasterAgent
        return MasterAgent("clip.mp4", translation_style="recap", tts_voice="my-MM-ThihaNeural", context_hint="hero", **kw)

    def test_state_records_the_choices(self):
        m = self.master(story_length="Detailed", narrative_tone="CALM", tts_rate="15")
        self.assertEqual((m.state.story_length, m.state.narrative_tone, m.state.tts_rate), ("detailed", "calm", 15))
        d = self.master()
        self.assertEqual((d.state.story_length, d.state.narrative_tone, d.state.tts_rate), ("standard", "natural", None))

    def test_default_signatures_are_the_ones_projects_made_before_already_have(self):
        m = self.master()
        legacy_script = hashlib.sha1("|".join(["burmese", "recap", "recap", "hero"]).encode("utf-8")).hexdigest()[:16]
        legacy_voice = hashlib.sha1("|".join([str(m.tts_engine).lower(), str(m.tts_voice), str(m.audio_mode)]).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(m._script_signature(), legacy_script)       # an older project does not regenerate its script
        self.assertEqual(m._voice_signature(), legacy_voice)         # ... nor re-voice every line

    def test_choosing_a_length_tone_or_rate_changes_the_right_signature_only(self):
        base = self.master()
        self.assertNotEqual(self.master(story_length="compact")._script_signature(), base._script_signature())
        self.assertNotEqual(self.master(narrative_tone="brisk")._script_signature(), base._script_signature())
        self.assertEqual(self.master(story_length="compact")._voice_signature(), base._voice_signature())
        self.assertNotEqual(self.master(tts_rate=15)._voice_signature(), base._voice_signature())
        self.assertEqual(self.master(tts_rate=15)._script_signature(), base._script_signature())

    def test_changing_the_length_regenerates_the_script_on_resume(self):
        first = self.master()
        first.state.generated_script = [{"scene_id": "1", "narration": "က", "start_sec": 0.0, "end_sec": 2.0}]
        first.state.completed_phases = ["phase_4_script_seo_thumbnail", "phase_5_voice_generation"]
        first.save_state()
        same = self.master()
        self.assertTrue(same.state.generated_script)
        longer = self.master(story_length="scene")
        self.assertEqual(longer.state.generated_script, [])

    def test_changing_the_speed_keeps_the_script_but_revoices(self):
        first = self.master()
        first.state.generated_script = [{"scene_id": "1", "narration": "က", "start_sec": 0.0, "end_sec": 2.0}]
        first.state.completed_phases = ["phase_4_script_seo_thumbnail", "phase_5_voice_generation"]
        first.save_state()
        faster = self.master(tts_rate=30)
        self.assertTrue(faster.state.generated_script)
        self.assertNotIn("phase_5_voice_generation", faster.state.completed_phases)

    def test_the_voice_agent_gets_the_speed(self):
        self.assertEqual(self.master(tts_rate=15).voice_agent.rate_mm, "+15%")


class VoiceRateTests(unittest.TestCase):
    def _agent(self, rate, tmp):
        from agents.voice_agent import VoiceAgent
        return VoiceAgent(voice="my-MM-ThihaNeural", output_dir=tmp, rate=rate)

    def test_rate_overrides_config_and_environment_and_is_clamped(self):
        tmp = tempfile.mkdtemp(prefix="pai_rate_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with patch.dict(os.environ, {"EDGE_TTS_RATE_MM": "+99%", "EDGE_TTS_RATE_EN": "+99%"}):
            self.assertEqual((self._agent(15, tmp).rate_mm, self._agent(15, tmp).rate_en), ("+15%", "+15%"))
            self.assertEqual(self._agent(-15, tmp).rate_mm, "-15%")
            self.assertEqual(self._agent(0, tmp).rate_mm, "+0%")
            self.assertEqual(self._agent(-80, tmp).rate_mm, "-50%")
            self.assertEqual(self._agent(500, tmp).rate_mm, "+100%")
            self.assertEqual(self._agent(None, tmp).rate_mm, "+99%")          # nothing chosen: environment / config rules


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_story_worker_")
        self.old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(lambda: (os.chdir(self.old), shutil.rmtree(self.work, ignore_errors=True)))

    def _run(self, *args, **kwargs):
        from services import job_runtime as jr
        from services.job_manager import jobs, jobs_lock
        job_id = kwargs.pop("job_id", "story-worker")
        with jobs_lock:
            jobs[job_id] = {"status": "running", "phase": "x", "buffer": None}
        master = MagicMock()
        master.output_dir = "outputs"
        master.state = types.SimpleNamespace(project_dir="Film")
        with patch.object(jr, "MasterAgent", return_value=master) as factory, \
                patch.object(jr, "_resolve_input_source", side_effect=lambda s: s):
            jr.pipeline_worker(job_id, *args, **kwargs)
        with jobs_lock:
            jobs.pop(job_id, None)
        return factory.call_args.kwargs

    def test_pipeline_worker_passes_the_options_to_the_master_agent(self):
        kw = self._run("movies/a.mp4", story_length="compact", narrative_tone="calm", tts_rate=20)
        self.assertEqual((kw["story_length"], kw["narrative_tone"], kw["tts_rate"]), ("compact", "calm", 20))

    def test_a_job_queued_before_the_options_existed_still_runs_with_defaults(self):
        """Queued jobs are stored positionally; the new parameters are last, so an old argument list is still valid."""
        from services import job_runtime as jr
        from services.job_args import worker_option_names
        self.assertEqual(worker_option_names(jr.pipeline_worker)[-3:], ["story_length", "narrative_tone", "tts_rate"])
        kw = self._run("movies/a.mp4")                                      # an old queue entry: leading parameters only
        self.assertEqual((kw["story_length"], kw["narrative_tone"], kw["tts_rate"]), ("standard", "natural", None))

    def test_the_job_options_saved_for_re_render_include_them(self):
        self._run("movies/a.mp4", story_length="detailed", tts_rate=-15)
        import json
        saved = json.load(open("outputs/Film/job_options.json", encoding="utf-8"))["options"]
        self.assertEqual((saved["story_length"], saved["narrative_tone"], saved["tts_rate"]), ("detailed", "natural", -15))

    def test_batch_worker_hands_them_to_the_batch_processor(self):
        from services import job_runtime as jr
        from services.job_manager import jobs, jobs_lock
        with jobs_lock:
            jobs["story-batch"] = {"status": "running", "phase": "x", "buffer": None}
        with patch("brain.planner.BatchProcessor") as processor, patch.object(jr, "_resolve_input_source", side_effect=lambda s: s):
            jr.batch_worker("story-batch", ["movies/a.mp4"], story_length="scene", narrative_tone="brisk", tts_rate=10)
        kw = processor.call_args.kwargs
        self.assertEqual((kw["story_length"], kw["narrative_tone"], kw["tts_rate"]), ("scene", "brisk", 10))
        with jobs_lock:
            jobs.pop("story-batch", None)


class ApiTests(unittest.TestCase):
    def test_start_accepts_the_options_and_validates_the_type_of_the_rate(self):
        import web_ui
        from fastapi.testclient import TestClient
        client = TestClient(web_ui.app)
        captured = {}

        def fake_enqueue(engine, opts):
            captured.update(engine=engine, opts=opts)
            return {"job_id": "j", "status": "running"}

        with patch.object(web_ui, "_enqueue_job", fake_enqueue):
            res = client.post("/api/start", json={"input": "movies/a.mp4", "story_length": "compact",
                                                  "narrative_tone": "dramatic", "tts_rate": 15})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((captured["opts"]["story_length"], captured["opts"]["narrative_tone"], captured["opts"]["tts_rate"]),
                         ("compact", "dramatic", 15))
        with patch.object(web_ui, "_enqueue_job", fake_enqueue):
            bad = client.post("/api/start", json={"input": "movies/a.mp4", "tts_rate": "fast"})
        self.assertEqual(bad.status_code, 422)


if __name__ == "__main__":
    unittest.main()
