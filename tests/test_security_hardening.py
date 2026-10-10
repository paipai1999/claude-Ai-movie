"""
Regression tests for the security hardening pass:
  - path safety helpers, project-name sanitising, delete-root protection
  - same-origin / auth policy, constant-time secrets, digest cookies, login endpoint
  - API-key handling (no raw keys echoed, strict mask resolution, env keys never persisted)
  - cancel isolation, ZIP job_id, upload limits
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.paths import resolve_inside, sanitize_project_name
from services import security


class TestPathHelpers(unittest.TestCase):

    def test_sanitize_project_name_blocks_traversal_and_absolute_paths(self):
        for hostile in ["../../etc/passwd", "..\\..\\Windows\\System32", "C:\\Windows\\evil", "/etc/cron.d/x", "a/b/c"]:
            slug = sanitize_project_name(hostile)
            self.assertNotIn("/", slug)
            self.assertNotIn("\\", slug)
            self.assertNotIn("..", slug)
            self.assertNotIn(":", slug)
            self.assertTrue(slug)

    def test_sanitize_project_name_keeps_unicode_and_supports_ascii_only(self):
        # combining marks (medials, vowel signs, asat) must survive – only unsafe characters are replaced
        self.assertEqual(sanitize_project_name("မြန်မာ ရုပ်ရှင်"), "မြန်မာ ရုပ်ရှင်")
        self.assertEqual(sanitize_project_name("ရုပ်ရှင်/အပိုင်း:၁"), "ရုပ်ရှင်_အပိုင်း_၁")
        ascii_slug = sanitize_project_name("မြန်မာ", fallback="fallback_name", ascii_only=True)
        self.assertEqual(ascii_slug, "fallback_name")

    def test_sanitize_project_name_empty_falls_back(self):
        self.assertTrue(sanitize_project_name("", fallback="hardsub_x").startswith("hardsub_x"))
        self.assertTrue(sanitize_project_name("...", fallback="").startswith("project_"))

    def test_resolve_inside_rejects_escape_base_and_absolute(self):
        base = tempfile.mkdtemp()
        try:
            self.assertEqual(resolve_inside(base, "a", "b.txt"), os.path.abspath(os.path.join(base, "a", "b.txt")))
            self.assertIsNone(resolve_inside(base, ".."))
            self.assertIsNone(resolve_inside(base, "../other"))
            self.assertIsNone(resolve_inside(base, "."))             # the base itself
            self.assertIsNone(resolve_inside(base, ""))
            self.assertIsNone(resolve_inside(base, os.path.abspath(os.sep)))   # absolute override
            self.assertEqual(resolve_inside(base, ".", allow_base=True), os.path.abspath(base))
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestSecurityPolicy(unittest.TestCase):

    def test_secrets_match_is_exact_and_rejects_empty(self):
        self.assertTrue(security.secrets_match("s3cret", "s3cret"))
        self.assertTrue(security.secrets_match("  s3cret ", "s3cret"))
        self.assertFalse(security.secrets_match("s3cre", "s3cret"))
        self.assertFalse(security.secrets_match("", "s3cret"))
        self.assertFalse(security.secrets_match(None, "s3cret"))
        self.assertFalse(security.secrets_match("anything", ""))

    def test_session_cookie_is_digest_not_secret(self):
        value = security.session_cookie_value("TopSecret")
        self.assertNotIn("TopSecret", value)
        self.assertEqual(len(value), 64)
        self.assertEqual(value, security.session_cookie_value("TopSecret"))
        self.assertNotEqual(value, security.session_cookie_value("Other"))

    def test_origin_policy(self):
        allow = security.origin_is_allowed
        self.assertTrue(allow(None, "127.0.0.1:5000", []))                       # curl / scripts
        self.assertTrue(allow("http://127.0.0.1:5000", "127.0.0.1:5000", []))    # the dashboard
        self.assertFalse(allow("https://evil.example", "127.0.0.1:5000", []))    # drive-by website
        self.assertFalse(allow("null", "127.0.0.1:5000", []))                    # sandboxed iframe
        self.assertTrue(allow("https://studio.example", "127.0.0.1:5000", ["https://studio.example"]))

    def test_host_header_guard_only_applies_to_loopback_binds(self):
        trusted = security.host_is_trusted
        self.assertTrue(trusted("localhost:5000", "127.0.0.1"))
        self.assertTrue(trusted("127.0.0.1:5000", "127.0.0.1"))
        self.assertTrue(trusted("[::1]:5000", "127.0.0.1"))
        self.assertFalse(trusted("rebind.attacker.example:5000", "127.0.0.1"))   # DNS rebinding
        self.assertTrue(trusted("anything.example", "0.0.0.0"))                  # public bind: auth is the guard
        self.assertTrue(trusted("anything.example", None))

    def test_remote_bind_without_secret_generates_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEB_UI_PASSWORD", None)
            os.environ.pop("WEB_UI_TOKEN", None)
            self.assertIsNone(security.ensure_remote_access_secret("127.0.0.1"))
            token = security.ensure_remote_access_secret("0.0.0.0")
            self.assertTrue(token and len(token) >= 16)
            self.assertEqual(os.environ.get("WEB_UI_TOKEN"), token)
            # already configured -> nothing new generated
            self.assertIsNone(security.ensure_remote_access_secret("0.0.0.0"))
            os.environ.pop("WEB_UI_TOKEN", None)


class TestWebApiHardening(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import web_ui
        cls.web_ui = web_ui

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(self.web_ui.app)   # fresh cookie jar per test
        self._saved_env = {k: os.environ.pop(k, None) for k in
                           ("WEB_UI_PASSWORD", "WEB_UI_TOKEN", "GEMINI_API_KEYS", "GEMINI_API_KEY")}
        self.work = tempfile.mkdtemp(prefix="pai_sec_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        os.makedirs("outputs/projA", exist_ok=True)
        open("outputs/projA/state.json", "w").write("{}")
        os.makedirs("movies", exist_ok=True)
        os.makedirs("temp", exist_ok=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        os.environ["CURRENT_JOB_CANCELLED"] = "0"
        for var, value in self._saved_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        import brain.config as cfg
        cfg._config_cache = None

    # ── CORS / cross-site ────────────────────────────────────────────────────────────────
    def test_default_cors_does_not_allow_foreign_origin(self):
        r = self.client.options("/api/keys/save", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        })
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), "https://evil.example")

    def test_cross_site_state_change_is_blocked_without_password(self):
        r = self.client.post("/api/cancel", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r2 = self.client.delete("/api/delete/outputs/projA", headers={"Origin": "https://evil.example"})
        self.assertEqual(r2.status_code, 403)
        self.assertTrue(os.path.exists("outputs/projA"))

    def test_same_origin_state_change_is_allowed(self):
        r = self.client.post("/api/cancel", headers={"Origin": "http://testserver"})
        self.assertEqual(r.status_code, 200)

    # ── delete root protection ───────────────────────────────────────────────────────────
    def test_delete_dot_cannot_wipe_a_whole_root_folder(self):
        for variant in ("/api/delete/outputs/%2e", "/api/delete/outputs/%2E", "/api/delete/movies/%2e"):
            r = self.client.delete(variant)
            self.assertIn(r.status_code, (400, 404))
        self.assertTrue(os.path.exists("outputs/projA/state.json"))

    def test_safe_child_path_rejects_root_and_traversal(self):
        f = self.web_ui._safe_child_path
        self.assertIsNone(f("outputs", "."))
        self.assertIsNone(f("outputs", ""))
        self.assertIsNone(f("outputs", "../movies"))
        self.assertIsNone(f("secrets", "x"))
        self.assertEqual(f("outputs", "projA"), os.path.abspath(os.path.join("outputs", "projA")))

    def test_delete_refused_while_a_job_runs(self):
        with self.web_ui.jobs_lock:
            self.web_ui.jobs["running_job_x"] = {"status": "running", "phase": "x", "created_at": time.time()}
        try:
            r = self.client.delete("/api/delete/outputs/projA")
            self.assertEqual(r.status_code, 409)
            self.assertTrue(os.path.exists("outputs/projA"))
        finally:
            with self.web_ui.jobs_lock:
                self.web_ui.jobs.pop("running_job_x", None)

    # ── authentication ───────────────────────────────────────────────────────────────────
    def test_password_mode_cookie_stores_digest_and_login_works(self):
        os.environ["WEB_UI_PASSWORD"] = "Pa55-w0rd"
        self.assertEqual(self.client.get("/api/config").status_code, 401)

        bad = self.client.post("/api/login", json={"token": "wrong"})
        self.assertEqual(bad.status_code, 401)

        ok = self.client.post("/api/login", json={"token": "Pa55-w0rd"})
        self.assertEqual(ok.status_code, 200)
        cookie = ok.cookies.get("web_ui_token")
        self.assertTrue(cookie)
        self.assertNotIn("Pa55-w0rd", cookie)
        self.assertEqual(cookie, security.session_cookie_value("Pa55-w0rd"))

        # the digest cookie authenticates later requests
        self.assertEqual(self.client.get("/api/config").status_code, 200)

    def test_login_form_post_redirects_and_sets_cookie(self):
        os.environ["WEB_UI_PASSWORD"] = "Pa55-w0rd"
        r = self.client.post("/api/login", content="token=Pa55-w0rd",
                             headers={"Content-Type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("web_ui_token", r.headers.get("set-cookie", ""))

    def test_auth_status_is_public(self):
        os.environ["WEB_UI_PASSWORD"] = "x1"
        r = self.client.get("/api/auth/status")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["auth_required"])
        self.assertFalse(r.json()["authenticated"])

    # ── API keys ─────────────────────────────────────────────────────────────────────────
    def test_save_keys_never_echoes_raw_keys_and_rejects_wildcard_mask(self):
        keys = ["KEYAAAAA-1111111111111111-ONE1", "KEYBBBBB-2222222222222222-TWO2"]
        json.dump({"gemini": {"api_keys": keys}}, open("config.json", "w"))
        import brain.config as cfg
        cfg._config_cache = None

        # a bare prefix is NOT a valid mask: it must not resolve (nor reveal) a stored key,
        # and the request is refused instead of silently deleting the stored keys.
        r = self.client.post("/api/keys/save", json={"keys": ["KEYAAA..."]})
        self.assertEqual(r.status_code, 400)
        for k in keys:
            self.assertNotIn(k, json.dumps(r.json()))
        self.assertEqual(json.load(open("config.json"))["gemini"]["api_keys"], keys)   # untouched

    def test_save_keys_full_mask_resolves_and_response_is_masked(self):
        keys = ["KEYAAAAA-1111111111111111-ONE1", "KEYBBBBB-2222222222222222-TWO2"]
        json.dump({"gemini": {"api_keys": keys}}, open("config.json", "w"))
        import brain.config as cfg
        cfg._config_cache = None
        mask = keys[0][:6] + "..." + keys[0][-4:]
        r = self.client.post("/api/keys/save", json={"keys": [mask]})
        self.assertEqual(r.json()["keys_count"], 1)
        self.assertNotIn(keys[0], json.dumps(r.json()))
        saved = json.load(open("config.json"))["gemini"]["api_keys"]
        self.assertEqual(saved, [keys[0]])

    # ── cancel isolation ─────────────────────────────────────────────────────────────────
    def test_cancelling_a_queued_job_does_not_stop_the_running_job(self):
        wu = self.web_ui
        os.environ["CURRENT_JOB_CANCELLED"] = "0"
        with wu.jobs_lock:
            wu.jobs["run_job"] = {"status": "running", "phase": "Phase 6", "created_at": time.time()}
            wu.jobs["queued_job"] = {"status": "queued", "phase": "Queued", "created_at": time.time()}
        with wu.queue_lock:
            wu.job_queue.append({"job_id": "queued_job", "target": None, "args": ()})
        try:
            r = self.client.post("/api/cancel/queued_job")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(os.environ.get("CURRENT_JOB_CANCELLED"), "0")   # running job untouched
            with wu.jobs_lock:
                self.assertEqual(wu.jobs["run_job"]["status"], "running")
                self.assertEqual(wu.jobs["queued_job"]["status"], "cancelled")
            # cancelling the running job itself DOES raise the flag
            self.client.post("/api/cancel/run_job")
            self.assertEqual(os.environ.get("CURRENT_JOB_CANCELLED"), "1")
        finally:
            with wu.jobs_lock:
                wu.jobs.pop("run_job", None)
                wu.jobs.pop("queued_job", None)
            with wu.queue_lock:
                wu.job_queue.clear()

    # ── ZIP bundle belongs to the requested job ──────────────────────────────────────────
    def test_zip_uses_the_project_recorded_for_the_job(self):
        wu = self.web_ui
        os.makedirs("outputs/older_proj", exist_ok=True)
        os.makedirs("outputs/newer_proj", exist_ok=True)
        open("outputs/older_proj/a.txt", "w").write("a")
        open("outputs/newer_proj/b.txt", "w").write("b")
        os.utime("outputs/older_proj", (1, 1))
        with wu.jobs_lock:
            wu.jobs["zip_job"] = {"status": "done", "phase": "Done", "created_at": time.time(), "project_dir": "older_proj"}
        try:
            r = self.client.get("/api/download/zip?job_id=zip_job")
            self.assertEqual(r.status_code, 200)
            self.assertIn("older_proj", r.headers.get("content-disposition", ""))
        finally:
            with wu.jobs_lock:
                wu.jobs.pop("zip_job", None)

    def test_zip_for_unknown_job_is_404(self):
        os.makedirs("outputs/some_proj", exist_ok=True)
        self.assertEqual(self.client.get("/api/download/zip?job_id=does-not-exist").status_code, 404)

    # ── upload limits ────────────────────────────────────────────────────────────────────
    def test_upload_over_limit_is_rejected_and_partial_file_removed(self):
        with patch.object(self.web_ui, "MAX_UPLOAD_BYTES", 1024):
            r = self.client.post("/api/upload", files={"video": ("big.mp4", b"x" * 4096, "video/mp4")})
        self.assertEqual(r.status_code, 413)
        self.assertFalse(os.path.exists(os.path.join("movies", "big.mp4")))

    def test_local_input_must_be_a_media_file(self):
        secret = os.path.join(self.work, "notes.txt")
        open(secret, "w").write("not a video")
        with self.assertRaises(ValueError):
            self.web_ui._resolve_input_source(secret)


class TestConfigSecretsPersistence(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_cfg_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import brain.config as cfg
        self.cfg = cfg
        cfg._config_cache = None
        self.old_env = os.environ.get("GEMINI_API_KEYS")

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        if self.old_env is None:
            os.environ.pop("GEMINI_API_KEYS", None)
        else:
            os.environ["GEMINI_API_KEYS"] = self.old_env
        self.cfg._config_cache = None

    def test_env_only_keys_are_never_written_to_config_json(self):
        json.dump({"gemini": {"api_keys": ["FILE-KEY-1"]}}, open("config.json", "w"))
        os.environ["GEMINI_API_KEYS"] = "ENV-KEY-1,ENV-KEY-2"
        loaded = self.cfg.load_config()
        self.assertIn("ENV-KEY-1", loaded["gemini"]["api_keys"])      # usable at runtime
        self.cfg.save_config(loaded)                                   # e.g. a UI setting change
        on_disk = json.load(open("config.json"))["gemini"]["api_keys"]
        self.assertEqual(on_disk, ["FILE-KEY-1"])
        # in-memory config still has everything
        self.assertIn("ENV-KEY-2", self.cfg.load_config()["gemini"]["api_keys"])

    def test_key_present_in_file_and_env_stays_in_file(self):
        json.dump({"gemini": {"api_keys": ["SHARED"]}}, open("config.json", "w"))
        os.environ["GEMINI_API_KEYS"] = "SHARED,ONLY-ENV"
        self.cfg.save_config(self.cfg.load_config())
        self.assertEqual(json.load(open("config.json"))["gemini"]["api_keys"], ["SHARED"])

    def test_deep_merge_does_not_share_default_sections(self):
        merged = self.cfg._deep_merge(self.cfg.DEFAULT_CONFIG, {})
        merged["qa"]["enabled"] = "MUTATED"
        self.assertNotEqual(self.cfg.DEFAULT_CONFIG["qa"]["enabled"], "MUTATED")


if __name__ == "__main__":
    unittest.main()
