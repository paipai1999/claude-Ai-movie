"""
Every recap project remembers the settings it was started with (job_options.json), whether it came from the
single-video worker or the overnight batch, so "re-render" can reuse them exactly.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import job_runtime as jr  # noqa: E402
from services.job_args import build_worker_args, options_from_call  # noqa: E402
from services.job_manager import jobs, jobs_lock  # noqa: E402


class WorkDir(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_opts_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)


def fake_master(project="Film A"):
    master = MagicMock()
    master.output_dir = "outputs"
    master.state = types.SimpleNamespace(project_dir=project)
    return master


class OptionSnapshotTests(unittest.TestCase):
    def test_snapshot_uses_option_names_and_round_trips_into_worker_args(self):
        def worker(job_id, input_source, language="burmese", resume=True):
            return options_from_call(worker, locals())

        snapshot = worker("j1", "movies/a.mp4", "english", False)
        self.assertEqual(snapshot, {"input": "movies/a.mp4", "language": "english", "resume": False})
        self.assertEqual(build_worker_args(worker, "j2", dict(snapshot)), ("j2", "movies/a.mp4", "english", False))

    def test_job_id_is_not_part_of_the_snapshot(self):
        def worker(job_id, url, resolution="1080p"):
            return options_from_call(worker, locals())
        self.assertNotIn("job_id", worker("j", "http://x"))


class SingleVideoWorkerTests(WorkDir):
    def test_pipeline_worker_saves_the_options_before_the_run_and_a_report_after(self):
        job_id = "opts-single"
        with jobs_lock:
            jobs[job_id] = {"status": "running", "phase": "x", "buffer": None}
        master = fake_master("Film A")
        order = []
        master.run_pipeline.side_effect = lambda: order.append("run")
        os.makedirs("outputs/Film A")
        with open("outputs/Film A/state.json", "w", encoding="utf-8") as fh:
            json.dump({"movie_name": "Film A", "generated_script": [{"narration": "က", "start_sec": 0, "end_sec": 3}]}, fh)

        real_save = jr.project_store.save_job_options
        with patch.object(jr, "MasterAgent", return_value=master), \
                patch.object(jr, "_resolve_input_source", side_effect=lambda s: s), \
                patch.object(jr.project_store, "save_job_options",
                             side_effect=lambda *a, **k: (order.append("save"), real_save(*a, **k))[1]):
            jr.pipeline_worker(job_id, "movies/film.mp4", language="burmese", translation_style="recap",
                               context_hint="hero is John", resume=False, tts_voice="nilar")
        self.assertEqual(order, ["save", "run"])            # saved before the (possibly long) run
        saved = json.load(open("outputs/Film A/job_options.json", encoding="utf-8"))["options"]
        self.assertEqual(saved["input"], "movies/film.mp4")
        self.assertEqual((saved["translation_style"], saved["context_hint"], saved["tts_voice"]), ("recap", "hero is John", "nilar"))
        self.assertIs(saved["resume"], False)               # the endpoint forces resume on; the original is kept as is
        self.assertTrue(os.path.exists("outputs/Film A/quality_report.json"))
        with jobs_lock:
            jobs.pop(job_id, None)

    def test_no_api_key_or_secret_is_ever_in_the_saved_options(self):
        saved_keys = set(options_from_call(jr.pipeline_worker, {k: None for k in ("job_id", "input_source", "language")}))
        self.assertFalse([k for k in saved_keys if "key" in k or "token" in k or "password" in k])


class BatchWorkerTests(WorkDir):
    def test_batch_processor_hook_saves_per_item_options(self):
        from brain.planner import BatchProcessor
        masters = {"movies/a.mp4": fake_master("Film A"), "movies/b.mp4": fake_master("Film B")}
        with patch("brain.planner.MasterAgent", side_effect=lambda path, **kw: masters[path]):
            calls = []
            processor = BatchProcessor(skip_completed=False, on_project=lambda stage, master, path: calls.append((stage, path)))
            processor.process_all(local_paths=["movies/a.mp4", "movies/b.mp4"])
        self.assertEqual(calls, [("start", "movies/a.mp4"), ("done", "movies/a.mp4"),
                                 ("start", "movies/b.mp4"), ("done", "movies/b.mp4")])

    def test_a_failing_hook_never_breaks_the_batch(self):
        from brain.planner import BatchProcessor
        master = fake_master("Film A")
        with patch("brain.planner.MasterAgent", return_value=master):
            def boom(stage, m, path):
                raise RuntimeError("hook failed")
            processor = BatchProcessor(skip_completed=False, on_project=boom)
            processor.process_all(local_paths=["movies/a.mp4"])
        master.run_pipeline.assert_called_once()
        self.assertEqual(processor.results[0]["status"], "SUCCESS")

    def test_batch_worker_remembers_each_item_with_its_own_input(self):
        job_id = "opts-batch"
        with jobs_lock:
            jobs[job_id] = {"status": "running", "phase": "x", "buffer": None}
        masters = {"movies/a.mp4": fake_master("Film A"), "movies/b.mp4": fake_master("Film B")}
        with patch("brain.planner.MasterAgent", side_effect=lambda path, **kw: masters[path]), \
                patch.object(jr, "_resolve_input_source", side_effect=lambda s: s):
            jr.batch_worker(job_id, ["movies/a.mp4", "movies/b.mp4"], language="burmese", translation_style="recap",
                            context_hint="hero is John")
        for project, movie in (("Film A", "movies/a.mp4"), ("Film B", "movies/b.mp4")):
            saved = json.load(open(f"outputs/{project}/job_options.json", encoding="utf-8"))["options"]
            self.assertEqual(saved["input"], movie)
            self.assertNotIn("inputs", saved)
            self.assertEqual(saved["context_hint"], "hero is John")
        with jobs_lock:
            jobs.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
