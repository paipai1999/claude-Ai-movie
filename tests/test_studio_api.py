"""
HTTP tests of the studio endpoints (projects, script editor, quality, profiles, history, re-render),
including the app-wide security middleware. Runs against a scratch working directory.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_ui  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PHASES = ["phase_4_script_seo_thumbnail", "phase_5_voice_generation", "phase_6_video_merge", "phase_7_qa"]


def write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class StudioApiCase(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in ("WEB_UI_PASSWORD", "WEB_UI_TOKEN")}
        self.work = tempfile.mkdtemp(prefix="pai_studio_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(self._restore)
        self.client = TestClient(web_ui.app)
        env = patch.dict(os.environ, {"PAI_PROFILES_FILE": os.path.join(self.work, "profiles.json")})
        env.start()
        self.addCleanup(env.stop)

        self.project = os.path.join("outputs", "Film One")
        os.makedirs(self.project)
        write(os.path.join(self.project, "state.json"), {
            "movie_name": "Film One", "project_dir": "Film One", "pipeline_status": "COMPLETED",
            "completed_phases": PHASES, "phase_checkpoints": {}, "phase_statuses": {},
            "generated_script": [
                {"scene_id": "1", "narration": "ပထမစာကြောင်း ဒီလိုပါပဲ", "start_sec": 0.0, "end_sec": 4.0,
                 "gender": "male", "source_text": "First line"},
                {"scene_id": "2", "narration": "Left in English", "start_sec": 5.0, "end_sec": 9.0,
                 "gender": "male", "source_text": "Second line", "needs_review": True, "issues": ["untranslated"]},
            ],
        })
        write(os.path.join(self.project, "checkpoint.json"), {"completed_phases": PHASES})

    def _restore(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v


class ProjectEndpointTests(StudioApiCase):
    def test_listing(self):
        body = self.client.get("/api/projects").json()
        self.assertEqual([p["name"] for p in body["projects"]], ["Film One"])
        self.assertEqual(body["projects"][0]["needs_review"], 1)

    def test_get_script(self):
        body = self.client.get("/api/projects/Film One/script").json()
        self.assertTrue(body["editable"])
        self.assertEqual([b["needs_review"] for b in body["blocks"]], [False, True])
        self.assertEqual(body["summary"]["total"], 2)

    def test_put_script_saves_and_invalidates(self):
        res = self.client.put("/api/projects/Film One/script",
                              json={"edits": [{"index": 1, "narration": "ဒုတိယစာကြောင်း ပြင်ပြီးပါပြီ"}]})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["changed"], 1)
        self.assertIn("phase_5_voice_generation", body["invalidated"])
        self.assertEqual(body["script"]["needs_review"], 0)
        state = read(os.path.join(self.project, "state.json"))
        self.assertEqual(state["generated_script"][1]["narration"], "ဒုတိယစာကြောင်း ပြင်ပြီးပါပြီ")
        self.assertEqual(state["completed_phases"], PHASES[:1])
        self.assertEqual(read(os.path.join(self.project, "checkpoint.json"))["completed_phases"], PHASES[:1])

    def test_put_only_touches_the_fields_that_were_sent(self):
        self.client.put("/api/projects/Film One/script", json={"edits": [{"index": 0, "gender": "female"}]})
        block = read(os.path.join(self.project, "state.json"))["generated_script"][0]
        self.assertEqual(block["gender"], "female")
        self.assertEqual(block["narration"], "ပထမစာကြောင်း ဒီလိုပါပဲ")      # not blanked by the missing field

    def test_invalid_edits_are_400_and_unknown_projects_404(self):
        bad = self.client.put("/api/projects/Film One/script", json={"edits": [{"index": 9, "narration": "က"}]})
        self.assertEqual(bad.status_code, 400)
        empty = self.client.put("/api/projects/Film One/script", json={"edits": [{"index": 0, "narration": " "}]})
        self.assertEqual(empty.status_code, 400)
        none = self.client.put("/api/projects/Film One/script", json={"edits": []})
        self.assertEqual(none.status_code, 400)
        self.assertEqual(self.client.get("/api/projects/Nope/script").status_code, 404)
        self.assertEqual(self.client.put("/api/projects/Nope/script", json={"edits": [{"index": 0, "narration": "က"}]}).status_code, 404)
        self.assertEqual(self.client.get("/api/projects/Nope/quality").status_code, 404)

    def test_path_traversal_is_not_found(self):
        os.makedirs("secret")
        write("secret/state.json", {"generated_script": [{"narration": "x"}]})
        for name in ("..%2Fsecret", "%2E%2E", "..%5Csecret"):
            with self.subTest(name=name):
                self.assertIn(self.client.get(f"/api/projects/{name}/script").status_code, (404, 400))
        self.assertIn("secret", os.listdir("."))
        self.assertEqual(read("secret/state.json")["generated_script"][0]["narration"], "x")

    def test_quality_report_is_returned_and_saved(self):
        body = self.client.get("/api/projects/Film One/quality").json()
        self.assertEqual(body["script"]["needs_review"], 1)
        self.assertIn(body["verdict"], ("attention", "review"))
        self.assertTrue(os.path.exists(os.path.join(self.project, "quality_report.json")))

    def test_requests_from_another_site_cannot_edit(self):
        res = self.client.put("/api/projects/Film One/script", headers={"Origin": "https://evil.example"},
                              json={"edits": [{"index": 0, "narration": "hacked"}]})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(read(os.path.join(self.project, "state.json"))["generated_script"][0]["narration"],
                         "ပထမစာကြောင်း ဒီလိုပါပဲ")

    def test_password_protects_the_studio_endpoints(self):
        with patch.dict(os.environ, {"WEB_UI_PASSWORD": "s3cret"}):
            client = TestClient(web_ui.app)
            for method, url in (("get", "/api/projects"), ("get", "/api/profiles"), ("get", "/api/storage"),
                                ("get", "/api/jobs/history"), ("put", "/api/projects/Film One/script"),
                                ("post", "/api/projects/Film One/rerender")):
                with self.subTest(url=url):
                    self.assertEqual(getattr(client, method)(url).status_code, 401)


class RerenderTests(StudioApiCase):
    def test_without_saved_settings_the_user_is_told_what_to_do(self):
        res = self.client.post("/api/projects/Film One/rerender")
        self.assertEqual(res.status_code, 409)
        self.assertIn("saved settings", res.json()["detail"])

    def test_with_saved_settings_the_job_is_enqueued_with_resume_on(self):
        write(os.path.join(self.project, "job_options.json"), {"options": {
            "input": "movies/film.mp4", "language": "burmese", "translation_style": "recap", "resume": False,
            "context_hint": "hero is John"}})
        captured = {}

        def fake_enqueue(engine, opts):
            captured.update(engine=engine, opts=opts)
            return {"job_id": "j1", "status": "running"}

        with patch.object(web_ui, "_enqueue_job", fake_enqueue):
            res = self.client.post("/api/projects/Film One/rerender")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(captured["engine"], "recap")
        self.assertTrue(captured["opts"]["resume"])                       # forced on: that is the whole point
        self.assertEqual(captured["opts"]["input"], "movies/film.mp4")
        self.assertEqual(captured["opts"]["context_hint"], "hero is John")   # script options kept -> edits survive

    def test_unknown_project(self):
        self.assertEqual(self.client.post("/api/projects/Nope/rerender").status_code, 404)

    def test_start_still_goes_through_the_shared_enqueue_helper(self):
        captured = {}

        def fake_enqueue(engine, opts):
            captured.update(engine=engine, opts=opts)
            return {"job_id": "j2", "status": "running"}

        with patch.object(web_ui, "_enqueue_job", fake_enqueue):
            res = self.client.post("/api/start", json={"input": "movies/a.mp4", "engine_mode": "hardsub"})
        self.assertEqual(res.json()["job_id"], "j2")
        self.assertEqual(captured["engine"], "hardsub")
        self.assertEqual(captured["opts"]["input"], "movies/a.mp4")
        self.assertEqual(captured["opts"]["translation_style"], "persona")    # engine default still resolved


class ProfileEndpointTests(StudioApiCase):
    def test_full_lifecycle(self):
        self.assertEqual(self.client.get("/api/profiles").json(), {"profiles": []})
        made = self.client.post("/api/profiles", json={
            "name": "Channel A", "engine_mode": "recap", "options": {"subtitle_style": "yellow_pop", "outro_card": True}})
        self.assertEqual(made.status_code, 200)
        pid = made.json()["id"]
        self.assertEqual(len(self.client.get("/api/profiles").json()["profiles"]), 1)

        upd = self.client.put(f"/api/profiles/{pid}", json={"name": "Channel A2", "engine_mode": "recap", "options": {}})
        self.assertEqual(upd.json()["name"], "Channel A2")
        self.assertEqual(self.client.delete(f"/api/profiles/{pid}").json(), {"status": "deleted"})
        self.assertEqual(self.client.delete(f"/api/profiles/{pid}").status_code, 404)

    def test_validation_errors_map_to_http_codes(self):
        self.client.post("/api/profiles", json={"name": "Dup", "engine_mode": "recap", "options": {}})
        self.assertEqual(self.client.post("/api/profiles", json={"name": "dup", "engine_mode": "recap", "options": {}}).status_code, 409)
        self.assertEqual(self.client.post("/api/profiles", json={"name": "x", "engine_mode": "recap", "options": {"input": "a"}}).status_code, 400)
        self.assertEqual(self.client.post("/api/profiles", json={"name": "x", "engine_mode": "recap", "options": {"nope": 1}}).status_code, 400)
        self.assertEqual(self.client.put("/api/profiles/missing", json={"name": "x", "engine_mode": "recap", "options": {}}).status_code, 404)


class HistoryAndStorageTests(StudioApiCase):
    def test_job_history_lists_newest_first_and_respects_limit(self):
        from brain.sqlite_store import create_job, update_job
        import time
        for i in range(3):
            create_job(f"job-{i}", f"movie_{i}.mp4", phase="Starting...")
            time.sleep(0.01)
        update_job("job-1", status="done", phase="Done")
        body = self.client.get("/api/jobs/history?limit=2").json()
        self.assertEqual([j["job_id"] for j in body["jobs"]], ["job-2", "job-1"])
        self.assertEqual(body["jobs"][1]["status"], "done")
        self.assertEqual(self.client.get("/api/jobs/history?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/jobs/history?limit=999").status_code, 422)

    def test_storage_summary(self):
        with open(os.path.join(self.project, "big.bin"), "wb") as fh:
            fh.write(b"0" * 1_500_000)
        body = self.client.get("/api/storage").json()
        self.assertGreaterEqual(body["outputs"], 1.5)
        self.assertEqual(body["movies"], 0)
        self.assertAlmostEqual(body["total_mb"], body["outputs"] + body["movies"] + body["temp"], places=1)


if __name__ == "__main__":
    unittest.main()
