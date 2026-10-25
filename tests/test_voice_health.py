"""
The dashboard's health check knows about the voice safety net: Edge TTS being down is survivable when a working
Gemini key can stand in for it, and the check says so without spending any quota.
"""

import asyncio
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_ui import system_health_check  # noqa: E402


def run_check(edge_online: bool, key_online: bool, config=None, env=None):
    """The health check with a fake Edge service and a fake answer from Google for the one key."""
    async def list_voices():
        if not edge_online:
            raise ConnectionError("403")
        return [{"ShortName": "my-MM-ThihaNeural"}]

    fake_edge = types.SimpleNamespace(list_voices=list_voices)
    response = MagicMock()
    response.__enter__.return_value.status = 200            # the key probe reads ``with urlopen(...) as resp: resp.status``
    settings = {"gemini": {"api_keys": ["dummy_key_123"]}}
    settings.update(config or {})
    environment = {"PAI_TTS_FALLBACK": ""}
    environment.update(env or {})

    def urlopen(*args, **kwargs):
        if not key_online:
            import urllib.error
            raise urllib.error.HTTPError("https://example.test", 400, "API key not valid", {}, None)
        return response

    with patch("brain.config.load_config", return_value=settings), patch.dict(sys.modules, {"edge_tts": fake_edge}), \
            patch.dict(os.environ, environment), patch("urllib.request.urlopen", side_effect=urlopen):
        return asyncio.run(system_health_check())["checks"]["voice"]


class VoiceHealthTests(unittest.TestCase):
    def test_edge_online_is_simply_ok(self):
        voice = run_check(edge_online=True, key_online=True)
        self.assertEqual((voice["status"], voice["healthy"], voice["edge_tts"]), ("ok", True, True))

    def test_edge_down_with_a_working_key_is_degraded_but_healthy(self):
        voice = run_check(edge_online=False, key_online=True)
        self.assertEqual((voice["status"], voice["healthy"], voice["gemini_fallback_ready"]), ("degraded", True, True))
        self.assertIn("Gemini voice would narrate", voice["details"])

    def test_edge_down_and_no_working_key_is_an_error(self):
        voice = run_check(edge_online=False, key_online=False)
        self.assertEqual((voice["status"], voice["healthy"], voice["gemini_fallback_ready"]), ("error", False, False))
        self.assertIn("no Gemini stand-in", voice["details"])

    def test_the_stand_in_can_be_switched_off(self):
        by_environment = run_check(edge_online=False, key_online=True, env={"PAI_TTS_FALLBACK": "off"})
        by_config = run_check(edge_online=False, key_online=True, config={"voice": {"gemini_tts": {"fallback": False}}})
        for voice in (by_environment, by_config):
            self.assertEqual((voice["status"], voice["gemini_fallback_ready"]), ("error", False))

    def test_the_dashboard_says_when_a_gemini_voice_stands_in(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "templates", "index.html"), encoding="utf-8").read()
        self.assertIn("gemini_fallback_ready", html)
        self.assertIn("a Gemini voice stands in", html)


if __name__ == "__main__":
    unittest.main()
