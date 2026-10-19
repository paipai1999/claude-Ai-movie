"""
brain.gemini_client.call_gemini - retry policy for transient failures.

An overnight job must survive a few minutes of Gemini "503 high demand" instead of dying on the
first busy spell, while a dead connection or a bad request must still fail fast.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import gemini_client as gc  # noqa: E402


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(body))


def _endless(make):
    """Fresh exception per call (an HTTPError body can only be read once)."""
    while True:
        yield make()


def _ok_response(text="done"):
    resp = MagicMock()
    resp.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.waits = []
        patches = [
            patch.object(gc, "_interruptible_sleep", side_effect=self.waits.append),
            patch.object(gc, "_record_api_usage"),
            patch("brain.tracker.reserve_model_usage", return_value=True),
            patch("brain.config.load_config", return_value={"gemini": {}}),
            patch.object(gc, "_build_model_list", return_value=["m-a", "m-b", "m-c"]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _call(self, side_effect):
        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen:
            try:
                result = gc.call_gemini("sys", "user", "fake-key", "m-a")
            except Exception as exc:   # the tests inspect the failure, not just its type
                result = exc
        return result, urlopen.call_count

    def test_overload_backs_off_then_recovers(self):
        # Round 1: all three models answer 503; round 2: the first model works.
        effects = [_http_error(503) for _ in range(3)] + [_ok_response("translated")]
        result, calls = self._call(effects)
        self.assertEqual(result, ("translated", "m-a"))
        self.assertEqual(calls, 4)
        self.assertEqual([w for w in self.waits if w >= 10], [15])

    def test_persistent_overload_backs_off_escalating_for_five_rounds_then_raises(self):
        result, calls = self._call(_endless(lambda: _http_error(503)))
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(result.code, 503)
        self.assertEqual(calls, 3 * gc._MAX_ROUNDS)
        self.assertEqual([w for w in self.waits if w >= 10], [15, 30, 60, 120])

    def test_bad_request_is_not_retried(self):
        result, calls = self._call(_endless(lambda: _http_error(400)))
        self.assertEqual(result.code, 400)
        self.assertEqual(calls, 3)           # one pass over the models, then stop
        self.assertEqual([w for w in self.waits if w >= 10], [])

    def test_dead_network_retries_once_not_five_times(self):
        result, calls = self._call(_endless(lambda: urllib.error.URLError("no route")))
        self.assertIsInstance(result, urllib.error.URLError)
        self.assertEqual(calls, 3 * 2)
        self.assertEqual(self.waits, [gc._TRANSIENT_NETWORK_WAIT_SEC])

    def test_rate_limit_waits_for_the_quota_window(self):
        effects = [_http_error(429, b'{"error": "per minute"}') for _ in range(3)] + [_ok_response()]
        result, _ = self._call(effects)
        self.assertEqual(result[0], "done")
        self.assertIn(gc._RPM_WAIT_SEC, self.waits)

    def test_daily_quota_gets_one_short_retry_only(self):
        result, calls = self._call(_endless(lambda: _http_error(429, b'{"error": "daily limit"}')))
        self.assertEqual(result.code, 429)
        self.assertEqual(calls, 3 * 2)
        self.assertEqual([w for w in self.waits if w >= 10], [30])


class RoundWaitTests(unittest.TestCase):
    def test_nothing_transient_means_no_retry(self):
        self.assertIsNone(gc._round_wait_seconds(0, overloaded=False, rpm=False, quota=False, network=False))

    def test_overload_schedule_caps_at_the_last_entry(self):
        waits = [gc._round_wait_seconds(i, overloaded=True, rpm=False, quota=False, network=False) for i in range(6)]
        self.assertEqual(waits, [15, 30, 60, 120, 120, 120])

    def test_mixed_failures_wait_for_the_slowest_cause(self):
        self.assertEqual(gc._round_wait_seconds(0, overloaded=True, rpm=True, quota=False, network=False), gc._RPM_WAIT_SEC)


if __name__ == "__main__":
    unittest.main()
