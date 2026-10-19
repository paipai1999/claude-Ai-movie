"""core.pronunciation, its use by VoiceAgent (spoken text only) and the /api/dictionary endpoints."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pronunciation as pr  # noqa: E402


class EntryTests(unittest.TestCase):
    def test_clean_entries_dedupes_case_insensitively_and_drops_noise(self):
        entries = pr.clean_entries([
            {"from": "  John ", "to": "ဂျွန်း"}, {"from": "john", "to": "ဂျွန်"},   # later wins
            {"from": "", "to": "x"}, {"from": "same", "to": "same"}, {"from": "Mary", "to": " မေရီ  "},
        ])
        self.assertEqual(entries, [{"from": "john", "to": "ဂျွန်"}, {"from": "Mary", "to": "မေရီ"}])

    def test_invalid_input_is_rejected(self):
        for bad in ("text", [1], [{"from": "a" * 81, "to": "x"}]):
            with self.subTest(bad=str(bad)[:20]):
                with self.assertRaises(pr.DictionaryError):
                    pr.clean_entries(bad)
        with self.assertRaises(pr.DictionaryError):
            pr.clean_entries([{"from": f"t{i}", "to": "x"} for i in range(pr.MAX_ENTRIES + 1)])
        self.assertEqual(pr.clean_entries(None), [])

    def test_parse_and_format_round_trip_with_every_separator(self):
        text = "# my names\nJohn => ဂျွန်း\nMary -> မေရီ\nBob = ဘော့ဘ်\nAnn\t→\tအန်န်\n\n"
        entries = pr.parse_lines(text)
        self.assertEqual([(e["from"], e["to"]) for e in entries],
                         [("John", "ဂျွန်း"), ("Mary", "မေရီ"), ("Bob", "ဘော့ဘ်"), ("Ann", "အန်န်")])
        self.assertEqual(pr.parse_lines(pr.format_lines(entries)), entries)

    def test_a_line_without_a_separator_is_an_error_naming_the_line(self):
        with self.assertRaises(pr.DictionaryError) as caught:
            pr.parse_lines("John => ဂျွန်း\njust a word")
        self.assertIn("just a word", str(caught.exception))


class ApplyTests(unittest.TestCase):
    def apply(self, text, *pairs):
        return pr.Pronouncer([{"from": a, "to": b} for a, b in pairs]).apply(text)

    def test_latin_terms_match_whole_words_case_insensitively(self):
        self.assertEqual(self.apply("John met Johnny. JOHN smiled.", ("john", "ဂျွန်း")), "ဂျွန်း met Johnny. ဂျွန်း smiled.")

    def test_burmese_terms_match_as_substrings(self):
        self.assertEqual(self.apply("ဂျွန်ကလာတယ်၊ ဂျွန်ပြန်သွားတယ်", ("ဂျွန်", "ဂျွန်း")), "ဂျွန်းကလာတယ်၊ ဂျွန်းပြန်သွားတယ်")

    def test_longest_entry_wins_and_replacements_are_not_replaced_again(self):
        self.assertEqual(self.apply("New York", ("New York", "နယူးယောက်"), ("York", "ယောက်")), "နယူးယောက်")
        self.assertEqual(self.apply("a b", ("a", "b"), ("b", "c")), "b c")          # a->b is final, never b->c

    def test_regex_characters_in_terms_are_literal(self):
        self.assertEqual(self.apply("Dr. X (2) costs $5", ("Dr. X", "ဒေါက်တာ အက်စ်"), ("(2)", "နှစ်"), ("$5", "ငါးဒေါ်လာ")),
                         "ဒေါက်တာ အက်စ် နှစ် costs ငါးဒေါ်လာ")

    def test_empty_dictionary_and_text_are_untouched(self):
        self.assertEqual(self.apply("hello"), "hello")
        self.assertEqual(self.apply("", ("a", "b")), "")
        self.assertFalse(pr.Pronouncer([]))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pai_dict_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = patch.dict(os.environ, {"PAI_DICTIONARY_FILE": os.path.join(self.tmp, "dictionary.json")})
        env.start()
        self.addCleanup(env.stop)

    def test_save_and_load(self):
        self.assertEqual(pr.load_entries(), [])
        saved = pr.save_entries([{"from": "John", "to": "ဂျွန်း"}])
        self.assertEqual(pr.load_entries(), saved)
        with open(os.path.join(self.tmp, "dictionary.json"), encoding="utf-8") as fh:
            self.assertIn("ဂျွန်း", fh.read())                       # real Burmese, not \\u escapes
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dictionary.json.tmp")))

    def test_a_corrupt_file_reads_as_empty(self):
        with open(os.path.join(self.tmp, "dictionary.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        self.assertEqual(pr.load_entries(), [])

    def test_invalid_save_writes_nothing(self):
        with self.assertRaises(pr.DictionaryError):
            pr.save_entries("nope")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dictionary.json")))

    def test_colab_drive_folder_is_used_when_present(self):
        from core import user_files
        drive = tempfile.mkdtemp(prefix="pai_drive_")
        self.addCleanup(shutil.rmtree, drive, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k != "PAI_DICTIONARY_FILE"}
        with patch.dict(os.environ, env, clear=True), patch.object(user_files, "DRIVE_DIR", drive):
            pr.save_entries([{"from": "a", "to": "b"}])
        self.assertTrue(os.path.exists(os.path.join(drive, "dictionary.json")))


class VoiceAgentTests(unittest.TestCase):
    def _agent(self, entries):
        from agents.voice_agent import VoiceAgent
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.engine, agent.voice = "edge_tts", "my-MM-ThihaNeural"
        agent.pronouncer = pr.Pronouncer(entries)
        return agent

    def test_spoken_text_uses_the_dictionary(self):
        agent = self._agent([{"from": "ဂျွန်", "to": "ဂျွန်းနီ"}])
        self.assertIn("ဂျွန်းနီ", agent._prepare_tts_text("ဂျွန်က မြို့ထဲကို ပြန်လာခဲ့တယ်"))

    def test_only_lines_the_dictionary_changes_get_a_new_fingerprint(self):
        plain = self._agent([])
        with_dict = self._agent([{"from": "ဂျွန်", "to": "ဂျွန်းနီ"}])
        mentions = {"narration": "ဂျွန်က ပြန်လာတယ်", "gender": "male"}
        unrelated = {"narration": "မိုးရွာနေတယ်", "gender": "male"}
        self.assertNotEqual(plain._clip_fingerprint(mentions), with_dict._clip_fingerprint(mentions))
        self.assertEqual(plain._clip_fingerprint(unrelated), with_dict._clip_fingerprint(unrelated))    # cache kept

    def test_without_a_dictionary_fingerprints_are_the_ones_users_already_have(self):
        from agents.voice_agent import VoiceAgent
        legacy = VoiceAgent.__new__(VoiceAgent)            # no pronouncer attribute at all (older state / tests)
        legacy.engine, legacy.voice = "edge_tts", "my-MM-ThihaNeural"
        item = {"narration": "ဂျွန်က ပြန်လာတယ်", "gender": "male", "emotion": "normal"}
        self.assertEqual(legacy._clip_fingerprint(item), self._agent([])._clip_fingerprint(item))
        self.assertEqual(legacy._apply_pronunciation("x"), "x")

    def test_subtitles_keep_the_original_spelling(self):
        """The dictionary is applied to the voice text only; the script block itself is never rewritten."""
        agent = self._agent([{"from": "ဂျွန်", "to": "ဂျွန်းနီ"}])
        block = {"narration": "ဂျွန်က ပြန်လာတယ်"}
        agent._clip_fingerprint(block)
        agent._prepare_tts_text(block["narration"])
        self.assertEqual(block["narration"], "ဂျွန်က ပြန်လာတယ်")


class ApiTests(unittest.TestCase):
    def setUp(self):
        import web_ui
        from fastapi.testclient import TestClient
        self.client = TestClient(web_ui.app)
        self.tmp = tempfile.mkdtemp(prefix="pai_dict_api_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = patch.dict(os.environ, {"PAI_DICTIONARY_FILE": os.path.join(self.tmp, "dictionary.json")})
        env.start()
        self.addCleanup(env.stop)

    def test_text_round_trip(self):
        self.assertEqual(self.client.get("/api/dictionary").json()["count"], 0)
        res = self.client.put("/api/dictionary", json={"text": "John => ဂျွန်း\nMary -> မေရီ"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["text"], "John => ဂျွန်း\nMary => မေရီ")
        self.assertEqual(self.client.get("/api/dictionary").json()["entries"], body["entries"])

    def test_structured_entries_and_clearing(self):
        self.client.put("/api/dictionary", json={"entries": [{"from": "a", "to": "b"}]})
        self.assertEqual(self.client.get("/api/dictionary").json()["count"], 1)
        self.client.put("/api/dictionary", json={"entries": []})
        self.assertEqual(self.client.get("/api/dictionary").json()["count"], 0)

    def test_invalid_input_is_400_and_changes_nothing(self):
        self.client.put("/api/dictionary", json={"text": "a => b"})
        self.assertEqual(self.client.put("/api/dictionary", json={"text": "no separator"}).status_code, 400)
        self.assertEqual(self.client.put("/api/dictionary", json={"entries": ["x"]}).status_code, 400)
        self.assertEqual(self.client.get("/api/dictionary").json()["count"], 1)

    def test_login_and_origin_rules_apply(self):
        res = self.client.put("/api/dictionary", headers={"Origin": "https://evil.example"}, json={"text": "a => b"})
        self.assertEqual(res.status_code, 403)
        from fastapi.testclient import TestClient
        import web_ui
        with patch.dict(os.environ, {"WEB_UI_PASSWORD": "s3cret"}):
            self.assertEqual(TestClient(web_ui.app).get("/api/dictionary").status_code, 401)


if __name__ == "__main__":
    unittest.main()
