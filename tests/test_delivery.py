"""
core.delivery - the delivery mode (finished video / CapCut pipeline / both) and how every studio honours it:
the pure rules, the ``delivers`` decorator, MasterAgent, the Hardsub engine end to end, the workers and the
job status the dashboard reads.
"""

import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import delivery  # noqa: E402


class RulesTests(unittest.TestCase):
    def test_modes_and_defaults(self):
        self.assertEqual(delivery.MODES, ("oneclick", "capcut", "both"))
        for raw in (None, "", "nonsense", 5, "ONECLICK"):
            self.assertEqual(delivery.normalize(raw), "oneclick", repr(raw))
        self.assertEqual(delivery.normalize(" CapCut "), "capcut")
        self.assertEqual(delivery.normalize("capcut-pipeline"), "capcut")
        self.assertEqual(delivery.normalize("one-click"), "oneclick")
        self.assertEqual(delivery.normalize("all"), "both")

    def test_what_each_mode_wants(self):
        self.assertEqual([(m, delivery.wants_video(m), delivery.wants_package(m)) for m in delivery.MODES],
                         [("oneclick", True, False), ("capcut", False, True), ("both", True, True)])
        self.assertTrue(delivery.wants_video(None))
        self.assertFalse(delivery.wants_package("garbage"))

    def test_the_option_model_offers_exactly_these_modes(self):
        from core import options as O
        option = O.BY_NAME["delivery_mode"]
        self.assertEqual(option.choices, delivery.MODES)
        self.assertEqual(option.default, "oneclick")
        for engine in ("recap", "subtitle", "hardsub", "shorts"):
            self.assertTrue(option.applies_to(engine), engine)
        for engine in ("download", "splitter"):
            self.assertFalse(option.applies_to(engine), engine)

    def test_project_dir_of_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            open(state, "w").close()
            self.assertEqual(delivery.project_dir_of({"project_dir": tmp}), os.path.abspath(tmp))
            self.assertEqual(delivery.project_dir_of({"state_json": state, "x": "relative"}), tmp)
            self.assertEqual(delivery.project_dir_of({"a": state}), tmp)          # any listed file will do
            self.assertIsNone(delivery.project_dir_of({"project_dir": os.path.join(tmp, "missing")}))
        for bad in (None, [], "text", {}):
            self.assertIsNone(delivery.project_dir_of(bad))


class BuildPackageTests(unittest.TestCase):
    INFO = {"zip": "capcut_package.zip", "size_mb": 1.2, "files": ["a", "b"]}

    def build(self, mode, effect):
        with mock.patch("services.capcut_export.export_capcut_package", side_effect=effect) as export:
            try:
                return delivery.build_package("/tmp/p", mode), export
            finally:
                pass

    def test_oneclick_builds_nothing(self):
        with mock.patch("services.capcut_export.export_capcut_package") as export:
            self.assertIsNone(delivery.build_package("/tmp/p", "oneclick"))
            self.assertIsNone(delivery.build_package("/tmp/p", None))
        export.assert_not_called()

    def test_capcut_and_both_build_it(self):
        for mode in ("capcut", "both"):
            info, export = self.build(mode, lambda *a, **k: self.INFO)
            self.assertEqual(info, self.INFO)
            export.assert_called_once()
            self.assertEqual(export.call_args[0][1], "p")          # the folder name is the project name

    def test_a_failure_is_fatal_only_in_the_capcut_pipeline(self):
        def boom(*args, **kwargs):
            raise ValueError("no voice")
        with mock.patch("services.capcut_export.export_capcut_package", side_effect=boom):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.build_package("/tmp/p", "capcut")
            self.assertIn("no voice", str(caught.exception))
            self.assertIsNone(delivery.build_package("/tmp/p", "both"))       # the finished video exists: just a warning


class DecoratorTests(unittest.TestCase):
    class Engine:
        def __init__(self, result):
            self.result = result
            self.seen = None

        @delivery.delivers
        def run(self, input_source, render_video=True, extra=None):
            self.seen = {"input_source": input_source, "render_video": render_video, "extra": extra}
            return self.result

    class NoRender:
        @delivery.delivers
        def run(self, input_source):
            return {"project_dir": input_source}

    def test_the_signature_shows_the_new_keyword(self):
        params = inspect.signature(self.Engine.run).parameters
        self.assertIn("delivery_mode", params)
        self.assertEqual(params["delivery_mode"].default, "oneclick")
        self.assertEqual(params["delivery_mode"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(self.Engine.run.__name__, "run")

    def test_the_capcut_pipeline_skips_the_render(self):
        engine = self.Engine({"project_dir": tempfile.gettempdir(), "status": "completed"})
        with mock.patch.object(delivery, "build_package", return_value={"zip": "capcut_package.zip"}):
            result = engine.run("movie.mp4", delivery_mode="capcut")
        self.assertFalse(engine.seen["render_video"])
        self.assertEqual(result["package"], "capcut_package.zip")

    def test_both_keeps_the_render_and_adds_the_package(self):
        engine = self.Engine({"project_dir": tempfile.gettempdir()})
        with mock.patch.object(delivery, "build_package", return_value={"zip": "capcut_package.zip"}):
            result = engine.run("movie.mp4", delivery_mode="both", render_video=True)
        self.assertTrue(engine.seen["render_video"])
        self.assertEqual(result["package"], "capcut_package.zip")

    def test_oneclick_changes_nothing(self):
        engine = self.Engine({"project_dir": tempfile.gettempdir()})
        with mock.patch.object(delivery, "build_package") as build:
            result = engine.run("movie.mp4")
            engine.run("movie.mp4", delivery_mode="oneclick")
        self.assertTrue(engine.seen["render_video"])
        self.assertNotIn("package", result)
        # build_package is still asked (it decides for itself that oneclick wants nothing) only when a package is wanted
        build.assert_not_called()

    def test_a_failed_run_gets_no_package(self):
        engine = self.Engine({"status": "failed", "error": "x", "project_dir": tempfile.gettempdir()})
        with mock.patch.object(delivery, "build_package") as build:
            result = engine.run("movie.mp4", delivery_mode="capcut")
        build.assert_not_called()
        self.assertEqual(result["status"], "failed")

    def test_an_unknown_project_folder_is_an_error_in_the_capcut_pipeline_only(self):
        engine = self.Engine({"phase": "x"})
        with self.assertRaises(delivery.DeliveryError):
            engine.run("movie.mp4", delivery_mode="capcut")
        self.assertEqual(self.Engine({"phase": "x"}).run("movie.mp4", delivery_mode="both"), {"phase": "x"})

    def test_engines_without_a_render_flag_still_work(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(delivery, "build_package", return_value={"zip": "z.zip"}):
            self.assertEqual(self.NoRender().run(tmp, delivery_mode="capcut")["package"], "z.zip")


class MasterAgentTests(unittest.TestCase):
    def setUp(self):
        from agents.master import MasterAgent
        self.MasterAgent = MasterAgent
        self.out = tempfile.mkdtemp(prefix="pai_master_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"PAI_DISABLE_AUTO_OPEN": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent(self, **kwargs):
        master = self.MasterAgent("dummy_movie.mp4", **kwargs)
        master.output_dir = self.out
        master.save_state = lambda: None
        master._cleanup_temp_files = lambda: None
        return master

    def test_the_default_is_the_finished_video(self):
        master = self.agent()
        self.assertEqual(master.delivery_mode, "oneclick")
        self.assertTrue(master.render_video)
        self.assertIsNone(master.package_info)

    def test_the_capcut_pipeline_skips_the_final_render(self):
        master = self.agent(delivery_mode="capcut", render_video=True)
        self.assertEqual(master.delivery_mode, "capcut")
        self.assertFalse(master.render_video)
        self.assertTrue(self.agent(delivery_mode="both").render_video)
        self.assertTrue(self.agent(delivery_mode="nonsense").render_video)

    def test_the_package_is_built_when_the_run_finishes(self):
        master = self.agent(delivery_mode="both")
        name = master.state.project_dir
        with mock.patch("agents.master.delivery.build_package", return_value={"zip": "capcut_package.zip"}) as build:
            master._finish_run(time.time())
        build.assert_called_once_with(os.path.join(self.out, name), "both", name)
        self.assertEqual(master.package_info, {"zip": "capcut_package.zip"})

    def test_a_failed_package_fails_the_capcut_pipeline(self):
        master = self.agent(delivery_mode="capcut")
        with mock.patch("services.capcut_export.export_capcut_package", side_effect=ValueError("nothing to export")):
            with self.assertRaises(delivery.DeliveryError):
                master._finish_run(time.time())

    def test_no_qa_is_pending_when_no_video_is_rendered(self):
        master = self.agent(delivery_mode="capcut")
        with mock.patch("agents.master.config.load_config", return_value={"qa": {"enabled": True}, "paths": {"clean_temp_after_merge": False}}), \
                mock.patch("agents.master.delivery.build_package", return_value=None):
            master._run_phase_7_qa()
            master._finish_run(time.time())
        self.assertEqual(master.state.pipeline_status, "COMPLETED")
        self.assertEqual(master.state.warnings, [])


class HardsubEndToEndTests(unittest.TestCase):
    """The real HardsubEngine.run (transcript and translation scripted): capcut skips the render and packages."""

    def setUp(self):
        import hardsub_engine
        self.module = hardsub_engine
        self.out = tempfile.mkdtemp(prefix="pai_hs_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        video = os.path.join(self.out, "movie.mp4")
        with open(video, "wb") as fh:
            fh.write(b"\0" * 2048)
        segments = [{"id": i + 1, "start": i * 3.0, "end": i * 3.0 + 2.0, "start_ts": f"00:00:{i * 3:02d},000",
                     "end_ts": f"00:00:{i * 3 + 2:02d},000", "original": f"Line {i}", "speaker_gender": "male", "burmese": ""}
                    for i in range(4)]

        def translate(this, segs, *a, **k):
            for s in segs:
                s["burmese"] = f"မြန်မာ {s['id']}"
            return segs

        for patcher in (
            mock.patch.object(hardsub_engine.HardsubEngine, "_ingest_video",
                              return_value=(video, None, "movie", {"duration": 12.0, "width": 320, "height": 240, "has_audio": True})),
            mock.patch.object(hardsub_engine.HardsubEngine, "_extract_transcript", return_value=(segments, "faster_whisper")),
            mock.patch.object(hardsub_engine.HardsubEngine, "_translate_dialogue", translate),
            mock.patch.object(hardsub_engine.HardsubEngine, "_render_hardsub_video", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = hardsub_engine.HardsubEngine(output_base_dir=self.out)

    def run_engine(self, **kwargs):
        return self.engine.run("movie.mp4", project_name="proj", blur_mode="no", **kwargs)

    def test_the_capcut_pipeline_renders_nothing_and_builds_the_package(self):
        render = self.module.HardsubEngine._render_hardsub_video
        result = self.run_engine(delivery_mode="capcut")
        render.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["package"], "capcut_package.zip")
        with zipfile.ZipFile(os.path.join(result["project_dir"], "capcut_package.zip")) as zf:
            self.assertIn("subtitles.srt", zf.namelist())
            srt = zf.read("subtitles.srt")
            self.assertTrue(srt.startswith(b"\xef\xbb\xbf"))
            self.assertIn("မြန်မာ 1", srt.decode("utf-8-sig"))

    def test_both_renders_and_packages(self):
        render = self.module.HardsubEngine._render_hardsub_video
        result = self.run_engine(delivery_mode="both", video_format="16:9")
        self.assertTrue(render.called)
        self.assertEqual(result["package"], "capcut_package.zip")

    def test_oneclick_is_unchanged(self):
        render = self.module.HardsubEngine._render_hardsub_video
        result = self.run_engine(video_format="16:9")
        self.assertTrue(render.called)
        self.assertNotIn("package", result)
        self.assertFalse(os.path.exists(os.path.join(result["project_dir"], "capcut_package.zip")))


class WorkerPlumbingTests(unittest.TestCase):
    def test_every_studio_worker_takes_delivery_mode_last(self):
        from services import job_runtime as jr
        for worker in (jr.pipeline_worker, jr.subtitle_worker, jr.hardsub_worker, jr.batch_worker, jr.shorts_worker):
            params = list(inspect.signature(worker).parameters)
            # Appended after everything that existed (queued jobs are positional); only options added LATER may follow it.
            later = {jr.pipeline_worker: ["sync_mode"], jr.batch_worker: ["sync_mode"], jr.shorts_worker: ["blur_mode"]}.get(worker, [])
            self.assertEqual(params[params.index("delivery_mode") + 1:], later, worker.__name__)
            self.assertEqual(params[-1 - len(later)], "delivery_mode", worker.__name__)
            self.assertEqual(inspect.signature(worker).parameters["delivery_mode"].default, "oneclick")
        for worker in (jr.downloader_worker, jr.splitter_worker):
            self.assertNotIn("delivery_mode", inspect.signature(worker).parameters)

    def test_build_worker_args_reads_the_option(self):
        from core import options as O
        from services import job_runtime as jr
        from services.job_args import build_worker_args
        for engine, worker in (("recap", jr.pipeline_worker), ("hardsub", jr.hardsub_worker), ("subtitle", jr.subtitle_worker),
                               ("shorts", jr.shorts_worker)):
            at = list(inspect.signature(worker).parameters).index("delivery_mode")      # the args tuple has the same order (job_id first)
            resolved = O.resolve(engine, {"input": "x.mp4", "delivery_mode": "capcut"})
            self.assertEqual(build_worker_args(worker, "J", resolved)[at], "capcut", engine)
            self.assertEqual(build_worker_args(worker, "J", O.resolve(engine, {"input": "x.mp4"}))[at], "oneclick", engine)

    def test_the_batch_processor_hands_the_mode_to_every_project(self):
        from brain.planner import BatchProcessor
        self.assertEqual(BatchProcessor(delivery_mode="capcut").delivery_mode, "capcut")
        params = list(inspect.signature(BatchProcessor.__init__).parameters)
        self.assertEqual(params[params.index("delivery_mode") + 1:], ["sync_mode"])      # only later options follow it


class JobStatusTests(unittest.TestCase):
    """What the dashboard reads when a job that built a package finishes."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_delivery_api_")
        self.old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(lambda: (os.chdir(self.old), shutil.rmtree(self.work, ignore_errors=True)))
        import web_ui
        from fastapi.testclient import TestClient
        from services import job_manager
        self.web_ui = web_ui
        self.client = TestClient(web_ui.app)
        self.jobs = job_manager.jobs

    def add_job(self, **fields):
        job_id = "job-" + str(len(self.jobs))
        self.jobs[job_id] = dict({"status": "done", "phase": "Done", "buffer": None, "created_at": time.time()}, **fields)
        self.addCleanup(self.jobs.pop, job_id, None)
        return job_id

    def test_the_status_carries_the_package_url(self):
        job_id = self.add_job(package="My Film", project_dir="My Film")
        body = self.client.get(f"/api/status/{job_id}").json()
        self.assertEqual(body["package_url"], "/api/projects/My%20Film/capcut")
        self.assertEqual(body["project_dir"], "My Film")

    def test_no_package_url_without_a_package_or_before_the_end(self):
        self.assertIsNone(self.client.get(f"/api/status/{self.add_job()}").json()["package_url"])
        running = self.add_job(status="running", package="My Film")
        self.assertIsNone(self.client.get(f"/api/status/{running}").json()["package_url"])

    def test_the_package_url_helper(self):
        self.assertEqual(self.web_ui._package_url({"status": "done", "package": "a b/c"}), "/api/projects/a%20b%2Fc/capcut")
        self.assertIsNone(self.web_ui._package_url({"status": "error", "package": "x"}))

    def test_run_job_remembers_that_a_package_was_built(self):
        from services import job_runtime as jr
        job_id = self.add_job(status="running")
        folder = os.path.join("outputs", "proj")
        os.makedirs(folder)
        jr._run_job(job_id, "src", "Starting", lambda: {"project_dir": os.path.abspath(folder), "package": "capcut_package.zip"})
        self.assertEqual(self.jobs[job_id]["status"], "done")
        self.assertEqual(self.jobs[job_id]["package"], "proj")
        plain = self.add_job(status="running")
        jr._run_job(plain, "src", "Starting", lambda: {"project_dir": os.path.abspath(folder)})
        self.assertNotIn("package", self.jobs[plain])

    def test_the_progress_marker_drives_the_bar_and_is_hidden_from_the_log(self):
        import io
        buffer = io.StringIO("--- [Phase 3: Choosing the best moments] ---\n[PROGRESS] 55\nsome log line\n[PROGRESS] 68\n")
        job_id = self.add_job(status="running", phase="Making Shorts...", buffer=buffer)
        body = self.client.get(f"/api/status/{job_id}").json()
        self.assertEqual(body["progress"], 68)
        self.assertFalse(any("[PROGRESS]" in line for line in body["log"]))
        self.assertIn("some log line", body["log"])


class OutputJsonTests(unittest.TestCase):
    def test_describe_lists_the_wizard_engines(self):
        from core import options as O
        data = json.loads(json.dumps(O.describe()))
        self.assertEqual(data["wizard_engines"], ["recap", "subtitle", "hardsub"])
        self.assertIn("shorts", data["engines"])
        self.assertIn("delivery_mode", data["options"])


if __name__ == "__main__":
    unittest.main()
