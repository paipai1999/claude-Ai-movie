"""
config.example.json is what a new user copies: it must be valid JSON and must agree with the defaults the code ships
for the settings it documents (a stale example silently misleads).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.config as cfg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as fh:
            cls.example = json.load(fh)

    def lookup(self, source, path):
        for key in path:
            source = source[key]
        return source

    def test_the_settings_added_with_the_voice_and_sync_work_are_documented_and_agree_with_the_defaults(self):
        for path in (("voice", "gemini_tts"), ("sync", "smart")):
            self.assertEqual(self.lookup(self.example, path), self.lookup(cfg.DEFAULT_CONFIG, path), path)
        ducking = self.example["audio_ducking"]
        for key in ("normalize_loudness", "target_lufs"):
            self.assertEqual(ducking[key], cfg.DEFAULT_CONFIG["audio_ducking"][key], key)

    def test_every_section_of_the_example_is_known_to_the_program(self):
        self.assertEqual(sorted(set(self.example) - set(cfg.DEFAULT_CONFIG)), [])

    def test_the_example_holds_no_key(self):
        keys = self.example.get("gemini", {}).get("api_keys", [])
        for key in keys:
            self.assertTrue(key.lower().startswith("aizasyyour") or "your" in key.lower() or key == "", key)


if __name__ == "__main__":
    unittest.main()
