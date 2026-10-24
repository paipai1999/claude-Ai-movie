"""Story bible display (parse_bible) and find & replace across a project's script (rename a character everywhere)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.story_memory import parse_bible, render_bible  # noqa: E402
from services import projects as pj  # noqa: E402

BIBLE_DATA = {
    "summary": "A detective returns to his home town.", "tone": "gritty thriller",
    "characters": [
        {"name": "John", "target_name": "ဂျွန်", "gender": "male", "role": "detective", "address": "sir"},
        {"name": "Mary", "target_name": "မေရီ", "gender": "female"},
    ],
    "places": [{"name": "Harbor", "target_name": "ဆိပ်ကမ်း"}],
    "terms": [{"term": "the Syndicate", "target": "အဖွဲ့ကြီး"}],
    "relationships": ["John is Mary's brother", "Mary hides a secret"],
}


class ParseBibleTests(unittest.TestCase):
    def test_round_trip_of_the_rendered_bible(self):
        parsed = parse_bible(render_bible(BIBLE_DATA))
        self.assertEqual(parsed["summary"], "A detective returns to his home town.")
        self.assertEqual(parsed["tone"], "gritty thriller")
        self.assertEqual(parsed["characters"][0], {"name": "John", "target": "ဂျွန်", "details": 'male, detective, addressed as "sir"'})
        self.assertEqual(parsed["characters"][1], {"name": "Mary", "target": "မေရီ", "details": "female"})
        self.assertEqual(parsed["places"], [{"name": "Harbor", "target": "ဆိပ်ကမ်း"}])
        self.assertEqual(parsed["terms"], [{"name": "the Syndicate", "target": "အဖွဲ့ကြီး"}])
        self.assertEqual(parsed["relationships"], ["John is Mary's brother", "Mary hides a secret"])

    def test_partial_bibles_and_non_bibles(self):
        parsed = parse_bible(render_bible({"characters": [{"name": "A", "target_name": "အေ"}]}))
        self.assertEqual(parsed["characters"], [{"name": "A", "target": "အေ", "details": ""}])
        self.assertEqual(parsed["places"], [])
        for bad in (None, "", "just some text"):
            self.assertIsNone(parse_bible(bad))


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pai_replace_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.dir = os.path.join(self.base, "Film")
        os.makedirs(self.dir)
        self.script = [
            {"scene_id": "1", "narration": "ဂျွန်က မြို့ထဲကို ပြန်လာတယ်", "start_sec": 0.0, "end_sec": 4.0, "gender": "male"},
            {"scene_id": "2", "narration": "John met Johnny at the Harbor, and john smiled", "start_sec": 5.0, "end_sec": 9.0, "gender": "male"},
            {"scene_id": "3", "narration": "မေရီက ဘာမှ မပြောဘူး", "start_sec": 10.0, "end_sec": 14.0, "gender": "female"},
        ]
        self.phases = ["phase_4_script_seo_thumbnail", "phase_5_voice_generation", "phase_6_video_merge", "phase_7_qa"]
        self.bible = render_bible(BIBLE_DATA)
        self.write_state(dict(movie_name="Film", generated_script=self.script, completed_phases=self.phases,
                              story_context=self.bible, story_context_key="abc123"))

    def write_state(self, state):
        with open(os.path.join(self.dir, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)

    def state(self):
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def narrations(self):
        return [b["narration"] for b in self.state()["generated_script"]]


class ReplaceTests(ProjectCase):
    def test_load_script_carries_the_parsed_story(self):
        story = pj.load_script(self.dir, "Film")["story"]
        self.assertEqual([c["name"] for c in story["characters"]], ["John", "Mary"])
        write = self.state()
        write.pop("story_context")
        self.write_state(write)
        self.assertIsNone(pj.load_script(self.dir, "Film")["story"])

    def test_preview_changes_nothing_and_reports_samples(self):
        before = open(os.path.join(self.dir, "state.json"), "rb").read()
        res = pj.replace_in_script(self.dir, "ဂျွန်", "ဂျွန်နီ", preview=True)
        self.assertEqual((res["lines"], res["occurrences"], res["line_numbers"]), (1, 1, [0]))
        self.assertEqual(res["samples"][0]["after"], "ဂျွန်နီက မြို့ထဲကို ပြန်လာတယ်")
        self.assertTrue(res["preview"])
        self.assertEqual(open(os.path.join(self.dir, "state.json"), "rb").read(), before)

    def test_apply_edits_only_matching_lines_and_uncompletes_the_render_phases(self):
        res = pj.replace_in_script(self.dir, "ဂျွန်", "ဂျွန်နီ")
        self.assertEqual((res["lines"], res["changed"]), (1, 1))
        state = self.state()
        self.assertEqual(self.narrations()[0], "ဂျွန်နီက မြို့ထဲကို ပြန်လာတယ်")
        self.assertEqual(self.narrations()[2], self.script[2]["narration"])        # untouched
        self.assertTrue(state["generated_script"][0]["edited"])
        self.assertEqual(state["completed_phases"], self.phases[:1])               # voice / merge / QA redone

    def test_latin_matching_is_case_insensitive_by_default_and_can_be_made_exact(self):
        res = pj.replace_in_script(self.dir, "john", "Jon", preview=True)
        self.assertEqual(res["occurrences"], 3)                                    # John, Johnny and john
        res = pj.replace_in_script(self.dir, "john", "Jon", preview=True, whole_word=True)
        self.assertEqual(res["occurrences"], 2)                                    # Johnny is a different word
        res = pj.replace_in_script(self.dir, "john", "Jon", preview=True, match_case=True)
        self.assertEqual(res["occurrences"], 1)                                    # only the lower-case one
        res = pj.replace_in_script(self.dir, "john", "Jon", preview=True, match_case=True, whole_word=True)
        self.assertEqual(res["occurrences"], 1)

    def test_replacement_text_is_literal(self):
        pj.replace_in_script(self.dir, "Harbor", r"\1 $& Port")
        self.assertIn(r"\1 $& Port", self.narrations()[1])

    def test_regex_characters_in_find_are_literal(self):
        self.write_state(dict(self.state(), generated_script=[dict(self.script[0], narration="Dr. X (2) costs $5. Dr. X")]))
        res = pj.replace_in_script(self.dir, "Dr. X", "ဒေါက်တာ")
        self.assertEqual(res["occurrences"], 2)
        self.assertEqual(self.narrations()[0], "ဒေါက်တာ (2) costs $5. ဒေါက်တာ")

    def test_the_story_bible_is_renamed_too_and_keeps_its_cache_key(self):
        pj.replace_in_script(self.dir, "ဂျွန်", "ဂျွန်နီ")
        state = self.state()
        self.assertIn("ဂျွန်နီ", state["story_context"])
        self.assertNotIn("=> ဂျွန် (", state["story_context"])
        self.assertEqual(state["story_context_key"], "abc123")                     # a later run reuses the edited bible

    def test_no_match_is_a_quiet_no_op(self):
        before = open(os.path.join(self.dir, "state.json"), "rb").read()
        res = pj.replace_in_script(self.dir, "Nobody", "x")
        self.assertEqual((res["lines"], res["occurrences"]), (0, 0))
        self.assertEqual(open(os.path.join(self.dir, "state.json"), "rb").read(), before)

    def test_invalid_requests(self):
        for find, repl in (("", "x"), ("   ", "x"), ("a" * (pj.MAX_FIND_CHARS + 1), "x"), ("same", "same")):
            with self.subTest(find=find[:10]):
                with self.assertRaises(pj.ProjectError):
                    pj.replace_in_script(self.dir, find, repl)
        with self.assertRaises(pj.ProjectError):                                   # would make a line too long
            pj.replace_in_script(self.dir, "Harbor", "x" * (pj.MAX_NARRATION_CHARS + 1))
        write = self.state()
        write.pop("generated_script")
        self.write_state(write)
        with self.assertRaises(pj.ProjectError) as caught:
            pj.replace_in_script(self.dir, "a", "b")
        self.assertEqual(caught.exception.code, "no_script")

    def test_an_emptying_replacement_is_rejected_rather_than_blanking_a_line(self):
        self.write_state(dict(self.state(), generated_script=[dict(self.script[0], narration="ဂျွန်")]))
        with self.assertRaises(pj.ProjectError):
            pj.replace_in_script(self.dir, "ဂျွန်", "")


class ReplaceApiTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        os.chdir(self.base)
        os.makedirs("outputs")
        shutil.move(self.dir, os.path.join("outputs", "Film"))
        self.addCleanup(os.chdir, self._cwd)
        import web_ui
        from fastapi.testclient import TestClient
        self.client = TestClient(web_ui.app)

    def test_preview_then_apply(self):
        pre = self.client.post("/api/projects/Film/script/replace", json={"find": "ဂျွန်", "replace": "ဂျွန်နီ", "preview": True})
        self.assertEqual((pre.status_code, pre.json()["lines"]), (200, 1))
        done = self.client.post("/api/projects/Film/script/replace", json={"find": "ဂျွန်", "replace": "ဂျွန်နီ"})
        self.assertEqual(done.json()["changed"], 1)
        script = self.client.get("/api/projects/Film/script").json()
        self.assertEqual(script["blocks"][0]["narration"], "ဂျွန်နီက မြို့ထဲကို ပြန်လာတယ်")
        self.assertEqual(script["story"]["characters"][0]["target"], "ဂျွန်နီ")

    def test_errors_map_to_http_codes(self):
        self.assertEqual(self.client.post("/api/projects/Film/script/replace", json={"find": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/projects/Nope/script/replace", json={"find": "a", "replace": "b"}).status_code, 404)
        res = self.client.post("/api/projects/Film/script/replace", headers={"Origin": "https://evil.example"},
                               json={"find": "ဂျွန်", "replace": "x"})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
