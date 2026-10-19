"""services.key_store and POST /api/keys/check - testing pasted keys without saving them."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.key_store import mask_key, resolve_key_lines, split_key_tokens  # noqa: E402

# Obviously fake keys: the tests must never need (or contain) a real one.
KEY_A = "AIzaFakeKeyNumberAAAA1111"
KEY_B = "AIzaFakeKeyNumberBBBB2222"


class ParsingTests(unittest.TestCase):
    def test_masks_have_both_ends_and_never_reveal_the_middle(self):
        self.assertEqual(mask_key(KEY_A), "AIzaFa...1111")
        self.assertEqual(mask_key("short"), "***")
        self.assertNotIn("KeyNumber", mask_key(KEY_A))

    def test_tokens_split_on_commas_and_newlines(self):
        self.assertEqual(split_key_tokens(["a, b\nc", "  ", "d"]), ["a", "b", "c", "d"])

    def test_full_keys_are_deduplicated_in_order(self):
        keys, unresolved = resolve_key_lines([KEY_A + "," + KEY_B, KEY_A], [])
        self.assertEqual((keys, unresolved), ([KEY_A, KEY_B], []))

    def test_a_mask_resolves_to_exactly_one_stored_key(self):
        keys, unresolved = resolve_key_lines([mask_key(KEY_A), KEY_B], [KEY_A])
        self.assertEqual((keys, unresolved), ([KEY_A, KEY_B], []))

    def test_unknown_ambiguous_and_wildcard_masks_are_refused(self):
        same_ends_1, same_ends_2 = "AIzaFa" + "x" * 12 + "1111", "AIzaFa" + "y" * 12 + "1111"
        for token in (mask_key(KEY_B), "AIzaFa...1111", "AIza...", "...1111"):
            with self.subTest(token=token):
                keys, unresolved = resolve_key_lines([token], [KEY_A, same_ends_1, same_ends_2])
                self.assertEqual((keys, unresolved), ([], [token]))


class CheckEndpointTests(unittest.TestCase):
    def setUp(self):
        import web_ui
        from fastapi.testclient import TestClient
        self.web_ui = web_ui
        self.client = TestClient(web_ui.app)
        self._env = {k: os.environ.pop(k, None) for k in ("WEB_UI_PASSWORD", "WEB_UI_TOKEN")}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in self._env.items() if v is not None])
        stored = patch("brain.config.load_config", return_value={"gemini": {"api_keys": [KEY_A]}})
        stored.start()
        self.addCleanup(stored.stop)

    def test_reports_each_key_masked_and_saves_nothing(self):
        with patch.object(self.web_ui, "_ping_gemini_keys", return_value=[("ONLINE", 200), ("INVALID KEY", 400)]) as ping, \
                patch("brain.config.save_config") as save:
            res = self.client.post("/api/keys/check", json={"keys": [KEY_A, KEY_B]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["results"], [
            {"key": "AIzaFa...1111", "state": "ONLINE", "http_code": 200, "ok": True},
            {"key": "AIzaFa...2222", "state": "INVALID KEY", "http_code": 400, "ok": False},
        ])
        ping.assert_called_once()
        save.assert_not_called()
        self.assertNotIn("KeyNumber", res.text)          # no raw key in the response

    def test_a_mask_of_a_stored_key_is_checked_as_that_key(self):
        with patch.object(self.web_ui, "_ping_gemini_keys", return_value=[("ONLINE", 200)]) as ping:
            res = self.client.post("/api/keys/check", json={"keys": [mask_key(KEY_A)]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(ping.call_args[0][0], [KEY_A])

    def test_bad_input_is_rejected_without_calling_google(self):
        with patch.object(self.web_ui, "_ping_gemini_keys") as ping:
            self.assertEqual(self.client.post("/api/keys/check", json={"keys": []}).status_code, 400)
            self.assertEqual(self.client.post("/api/keys/check", json={"keys": ["  \n "]}).status_code, 400)
            self.assertEqual(self.client.post("/api/keys/check", json={"keys": ["AIza..."]}).status_code, 400)
            many = ",".join(f"AIzaFakeKeyNumber{i:08d}" for i in range(self.web_ui.MAX_KEYS_PER_CHECK + 1))
            self.assertEqual(self.client.post("/api/keys/check", json={"keys": [many]}).status_code, 400)
        ping.assert_not_called()

    def test_requires_login_and_blocks_cross_site_calls(self):
        res = self.client.post("/api/keys/check", headers={"Origin": "https://evil.example"}, json={"keys": [KEY_A]})
        self.assertEqual(res.status_code, 403)
        with patch.dict(os.environ, {"WEB_UI_PASSWORD": "s3cret"}):
            from fastapi.testclient import TestClient
            self.assertEqual(TestClient(self.web_ui.app).post("/api/keys/check", json={"keys": [KEY_A]}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
