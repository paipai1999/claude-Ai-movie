"""
Tests for job runtime infrastructure:
  - persistent queue (store / load / restore after a restart)
  - per-job child-process registry and job-scoped Stop
  - shared Whisper settings policy
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import process_registry


def _sleeper():
    """A harmless long-running child process."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


class TestProcessRegistry(unittest.TestCase):

    def setUp(self):
        self.procs = []
        self.current = {"job": None}
        process_registry.install(lambda: self.current["job"])

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
        for job in ("job-a", "job-b", "job-c"):
            process_registry.release(job)

    def _spawn(self, job_id):
        self.current["job"] = job_id
        proc = _sleeper()
        self.procs.append(proc)
        self.current["job"] = None
        return proc

    def test_popen_is_attributed_to_the_resolved_job(self):
        a = self._spawn("job-a")
        b = self._spawn("job-b")
        self.assertIn(a.pid, process_registry.tracked_pids("job-a"))
        self.assertNotIn(a.pid, process_registry.tracked_pids("job-b"))
        self.assertIn(b.pid, process_registry.tracked_pids("job-b"))
        self.assertIn(b.pid, process_registry.pids_of_other_jobs("job-a"))

    def test_kill_only_touches_the_requested_job(self):
        a = self._spawn("job-a")
        b = self._spawn("job-b")
        self.assertGreaterEqual(process_registry.kill_job_processes("job-a"), 1)
        a.wait(timeout=10)
        self.assertIsNotNone(a.poll())          # job-a's process is gone
        self.assertIsNone(b.poll())             # job-b keeps running
        self.assertIn(b.pid, process_registry.tracked_pids("job-b"))

    def test_recycled_pid_with_other_creation_time_is_never_killed(self):
        victim = _sleeper()
        self.procs.append(victim)
        # pretend job-c once owned this PID, but the recorded creation time is different
        with process_registry._lock:
            process_registry._registry["job-c"] = {(victim.pid, 12345.0)}
        self.assertEqual(process_registry.kill_job_processes("job-c"), 0)
        self.assertIsNone(victim.poll())

    def test_untracked_when_no_job_resolves(self):
        self.current["job"] = None
        proc = _sleeper()
        self.procs.append(proc)
        for job in ("job-a", "job-b"):
            self.assertNotIn(proc.pid, process_registry.tracked_pids(job))


class TestPersistentQueue(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_queue_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import web_ui
        self.web_ui = web_ui
        self.ran = []

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        with self.web_ui.jobs_lock:
            for jid in ("restore-1", "restore-run", "restore-2"):
                self.web_ui.jobs.pop(jid, None)
        with self.web_ui.queue_lock:
            self.web_ui.job_queue.clear()

    def test_store_and_load_roundtrip_in_fifo_order(self):
        from brain.sqlite_store import create_job, store_queued_job, load_queued_jobs
        create_job("restore-2", "b.mp4", phase="Queued", status="queued")
        time.sleep(0.01)
        create_job("restore-1", "a.mp4", phase="Queued", status="queued")
        self.assertTrue(store_queued_job("restore-2", "pipeline_worker", ["restore-2", "b.mp4", {"x": True}], {"name": "b"}))
        self.assertTrue(store_queued_job("restore-1", "hardsub_worker", ["restore-1", "a.mp4"], {"name": "a"}))
        items = load_queued_jobs()
        self.assertEqual([i["job_id"] for i in items], ["restore-2", "restore-1"])
        self.assertEqual(items[0]["args"], ["restore-2", "b.mp4", {"x": True}])
        self.assertEqual(items[1]["worker"], "hardsub_worker")

    def test_non_serialisable_arguments_are_skipped_not_crashed(self):
        from brain.sqlite_store import create_job, store_queued_job, load_queued_jobs
        create_job("restore-1", "a.mp4", phase="Queued", status="queued")
        self.assertFalse(store_queued_job("restore-1", "pipeline_worker", [object()], {}))
        self.assertEqual(load_queued_jobs(), [])

    def test_only_queued_rows_are_restored_and_they_run(self):
        from brain.sqlite_store import create_job, store_queued_job
        create_job("restore-1", "a.mp4", phase="Queued", status="queued")
        store_queued_job("restore-1", "dummy_worker", ["restore-1", "a.mp4"], {"name": "a.mp4", "source": "a.mp4"})
        create_job("restore-run", "c.mp4", phase="Running", status="running")      # was mid-render: not restored
        store_queued_job("restore-run", "dummy_worker", ["restore-run", "c.mp4"], {"name": "c.mp4"})

        done = threading.Event()

        def dummy_worker(job_id, source):
            self.ran.append((job_id, source))
            self.web_ui._set_job_state(job_id, "done", "Done")
            done.set()

        with patch.object(self.web_ui, "_worker_registry", return_value={"dummy_worker": dummy_worker}):
            restored = self.web_ui._restore_queued_jobs()
            self.assertEqual(restored, 1)
            self.assertTrue(done.wait(timeout=15), "dispatcher did not start the restored job")

        self.assertEqual(self.ran, [("restore-1", "a.mp4")])
        self.assertNotIn("restore-run", [r[0] for r in self.ran])

    def test_enqueue_helper_persists_the_job(self):
        from brain.sqlite_store import create_job, load_queued_jobs
        create_job("restore-1", "a.mp4", phase="Queued", status="queued")
        entry = {
            "job_id": "restore-1", "target": self.web_ui.pipeline_worker,
            "args": ("restore-1", "a.mp4", "burmese"), "name": "a.mp4", "source": "a.mp4",
            "language": "burmese", "tts_engine": "edge_tts", "created_at": time.time(),
        }
        self.web_ui._persist_queued_job(entry)
        items = load_queued_jobs()
        self.assertEqual(items[0]["worker"], "pipeline_worker")
        self.assertEqual(items[0]["args"], ["restore-1", "a.mp4", "burmese"])


class TestJobLifecycle(unittest.TestCase):
    """_run_job is the single place that records done / error / cancelled for every worker."""

    def setUp(self):
        from services import job_runtime
        self.rt = job_runtime
        self.created = []
        self.work_dir = tempfile.mkdtemp(prefix="pai_life_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work_dir)
        os.environ["CURRENT_JOB_CANCELLED"] = "0"

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        with self.rt.jobs_lock:
            for jid in self.created:
                self.rt.jobs.pop(jid, None)
        os.environ["CURRENT_JOB_CANCELLED"] = "0"

    def _job(self, job_id):
        self.created.append(job_id)
        with self.rt.jobs_lock:
            self.rt.jobs[job_id] = {"status": "running", "phase": "Starting...", "buffer": None}
        return job_id

    def test_success_records_done_and_project_folder(self):
        jid = self._job("life-ok")
        os.makedirs(os.path.join("outputs", "My_Project"), exist_ok=True)
        self.rt._run_job(jid, "movie.mp4", "Starting...", lambda: {"project_dir": "My_Project"})
        job = self.rt.jobs[jid]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["phase"], "Done")
        self.assertEqual(job["project_dir"], "My_Project")

    def test_exception_records_error_with_message(self):
        jid = self._job("life-err")

        def boom():
            raise RuntimeError("ffmpeg exploded")

        self.rt._run_job(jid, "movie.mp4", "Starting...", boom)
        job = self.rt.jobs[jid]
        self.assertEqual(job["status"], "error")
        self.assertIn("ffmpeg exploded", job["error"])
        self.assertTrue(job["phase"].startswith("Error:"))

    def test_engine_failure_result_is_an_error_not_done(self):
        jid = self._job("life-failed")
        self.rt._run_job(jid, "m.mp4", "Starting...", lambda: {"status": "failed", "error": "render failed"})
        self.assertEqual(self.rt.jobs[jid]["status"], "error")
        self.assertIn("render failed", self.rt.jobs[jid]["error"])

    def test_interrupted_error_is_cancelled(self):
        jid = self._job("life-stop")

        def stopped():
            raise InterruptedError("user pressed stop")

        self.rt._run_job(jid, "m.mp4", "Starting...", stopped)
        self.assertEqual(self.rt.jobs[jid]["status"], "cancelled")

    def test_stop_flag_during_a_successful_run_is_cancelled(self):
        jid = self._job("life-flag")

        def work():
            os.environ["CURRENT_JOB_CANCELLED"] = "1"
            return {}

        self.rt._run_job(jid, "m.mp4", "Starting...", work)
        self.assertEqual(self.rt.jobs[jid]["status"], "cancelled")

    def test_console_output_is_captured_per_job_and_released_afterwards(self):
        jid = self._job("life-log")
        seen = {}

        def work():
            self.rt.thread_stdout.write("hello from the worker thread\n")
            seen["buffer"] = self.rt.thread_stdout.buffers[jid].getvalue()
            return {}

        self.rt._run_job(jid, "m.mp4", "Starting...", work)
        self.assertIn("hello from the worker thread", seen["buffer"])
        self.assertNotIn(jid, self.rt.thread_stdout.buffers)       # freed on job end

    def test_download_worker_reports_the_saved_file_name(self):
        jid = self._job("life-dl")

        class FakeDownloader:
            def __init__(self, output_dir="movies"):
                pass

            def download_video(self, url, resolution="1080p", custom_filename=None):
                return os.path.join("movies", "clip_saved.mp4")

        with patch.object(self.rt, "DownloaderAgent", FakeDownloader):
            self.rt.downloader_worker(jid, "https://example.com/v", "720p", "clip")
        self.assertEqual(self.rt.jobs[jid]["status"], "done")
        self.assertEqual(self.rt.jobs[jid]["phase"], "Downloaded: clip_saved.mp4")

    def test_project_dir_extraction(self):
        outputs = os.path.abspath("outputs")
        os.makedirs(os.path.join(outputs, "Proj A"), exist_ok=True)
        f = self.rt._project_dir_from_result
        self.assertEqual(f({"project_dir": "Proj A"}), "Proj A")
        self.assertEqual(f({"project_dir": os.path.join(outputs, "Proj A")}), "Proj A")
        self.assertEqual(f({"srt": os.path.join(outputs, "Proj A", "05.srt")}), "Proj A")
        self.assertIsNone(f({"project_dir": os.path.join(self.work_dir, "elsewhere")}))   # outside outputs/
        self.assertIsNone(f({"project_dir": outputs}))                                    # the root itself
        self.assertIsNone(f(None))


class TestVoiceMapping(unittest.TestCase):

    def test_language_and_voice_aliases(self):
        from services.job_runtime import _resolve_voice_mapping as m
        self.assertEqual(m("burmese", None), ("burmese", "my-MM-ThihaNeural"))
        self.assertEqual(m("nilar", None), ("burmese", "my-MM-NilarNeural"))
        self.assertEqual(m("english", None), ("english", "en-US-GuyNeural"))
        self.assertEqual(m("jenny", None), ("english", "en-US-JennyNeural"))
        self.assertEqual(m("burmese", "female"), ("burmese", "my-MM-NilarNeural"))   # explicit voice wins
        self.assertEqual(m("thai", None), ("thai", None))                            # unknown language untouched
        self.assertEqual(m("burmese", "custom-Voice"), ("burmese", "custom-Voice"))


class TestWhisperPolicy(unittest.TestCase):

    def test_auto_model_depends_on_gpu(self):
        from core.whisper_settings import resolve_whisper_model
        self.assertEqual(resolve_whisper_model("auto", cuda=True), "medium")
        self.assertEqual(resolve_whisper_model("auto", cuda=False), "base")
        self.assertEqual(resolve_whisper_model("", cuda=False), "base")
        self.assertEqual(resolve_whisper_model("Large-V3", cuda=False), "large-v3")   # explicit choice wins

    def test_beam_size_from_config_and_env(self):
        from core.whisper_settings import resolve_beam_size
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHISPER_BEAM_SIZE", None)
            self.assertEqual(resolve_beam_size({"whisper_beam_size": 5}), 5)
            self.assertEqual(resolve_beam_size({}), 1)
            self.assertEqual(resolve_beam_size({"whisper_beam_size": "junk"}), 1)
            self.assertEqual(resolve_beam_size({"whisper_beam_size": 0}), 1)
            os.environ["WHISPER_BEAM_SIZE"] = "3"
            self.assertEqual(resolve_beam_size({"whisper_beam_size": 5}), 3)

    def test_runtime_selection(self):
        from core.whisper_settings import runtime_for
        self.assertEqual(runtime_for(cuda=True), ("cuda", "float16"))
        self.assertEqual(runtime_for(cuda=False), ("cpu", "int8"))

    def test_gpu_init_failure_falls_back_to_cpu(self):
        import core.whisper_settings as ws
        calls = []

        class FakeModel:
            def __init__(self, name, device, compute_type, cpu_threads):
                calls.append((name, device, compute_type))
                if device == "cuda":
                    raise RuntimeError("cuDNN not found")

        fake_module = type(sys)("faster_whisper")
        fake_module.WhisperModel = FakeModel
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            with patch.object(ws, "cuda_available", return_value=True):
                model, name, device = ws.load_faster_whisper("auto")
        self.assertEqual(device, "cpu")
        self.assertEqual(name, "base")
        self.assertEqual(calls, [("medium", "cuda", "float16"), ("base", "cpu", "int8")])


if __name__ == "__main__":
    unittest.main()
