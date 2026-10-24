"""
WriterAgent wired to the translation flow: story bible, neighbouring-line context in the prompt,
truncated-answer recovery, validation retries, review flags and the "no API key" guard.
Gemini is replaced by a scripted fake - no network.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.writer_agent import WriterAgent  # noqa: E402
from brain.burmese_utils import (  # noqa: E402
    replace_numbers_with_burmese,
    sanitize_burmese_narration,
    transliterate_english_acronyms,
)
from brain.memory import MovieState, TranscriptSegment  # noqa: E402


def mm(n: int) -> str:
    words = ["မနက်ဖြန်", "ညနေခင်း", "တံခါးဝ", "ရထားဘူတာ", "ဆေးရုံကြီး", "ကျောင်းနား", "စျေးထဲ", "တောင်ကြော"]
    verbs = ["ပြေးသွားခဲ့တာပေါ့ဗျာ", "စောင့်နေခဲ့ပါတယ်", "ရောက်လာခဲ့ရတယ်", "ပျောက်သွားခဲ့တာပါ"]
    return f"{words[n % len(words)]}မှာ {words[(n * 3 + 1) % len(words)]}ကို {verbs[n % len(verbs)]} {n * 7 + 11}"


def done(n: int) -> str:
    """What mm(n) becomes after the writer's own clean-up (digits -> Burmese words, acronyms, stray scripts)."""
    return sanitize_burmese_narration(transliterate_english_acronyms(replace_numbers_with_burmese(mm(n))))


BIBLE_JSON = json.dumps({
    "summary": "A detective returns to his home town.",
    "tone": "gritty thriller",
    "characters": [{"name": "John", "target_name": "ဂျွန်", "gender": "male", "role": "detective", "address": "sir"}],
    "places": [{"name": "Harbor", "target_name": "ဆိပ်ကမ်း"}],
    "terms": [],
    "relationships": ["John is Mary's brother"],
})


def make_state(n_lines: int, **kw) -> MovieState:
    transcript = [
        TranscriptSegment(start=i * 5.0, end=i * 5.0 + 4.0, text=f"This is a fairly long spoken sentence number {i} in the film.")
        for i in range(n_lines)
    ]
    return MovieState(movie_name="Test Film", transcript=transcript, **kw)


class FakeGemini:
    """Stands in for call_gemini: answers the bible call, batch calls and single-line calls."""

    def __init__(self, batch_handler=None, bible=BIBLE_JSON):
        self.calls = []        # (kind, system, prompt, kwargs)
        self.bible = bible
        self.batch_handler = batch_handler or self.good_batch

    @staticmethod
    def ids_in(prompt: str):
        # ids of the main list = the JSON array embedded in the prompt that carries "max_chars"
        start = prompt.index('[\n  {\n    "id"')
        end = prompt.index("\n]", start) + 2
        return [item["id"] for item in json.loads(prompt[start:end])]

    def good_batch(self, ids, prompt, kwargs):
        return json.dumps([{"id": i, "narration": mm(i), "emotion": "normal", "character": "John", "gender": "male"} for i in ids])

    def __call__(self, system, prompt, key, model, **kwargs):
        if "script supervisor" in system:
            self.calls.append(("bible", system, prompt, kwargs))
            return self.bible, model
        if kwargs.get("response_mime_type") == "text/plain":
            self.calls.append(("line", system, prompt, kwargs))
            return mm(999), model
        self.calls.append(("batch", system, prompt, kwargs))
        return self.batch_handler(self.ids_in(prompt), prompt, kwargs), model

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def quiet_writer(**kw):
    return WriterAgent(language="burmese", script_engine=kw.pop("script_engine", "recap"), **kw)


class WriterFlowTestCase(unittest.TestCase):
    def setUp(self):
        for p in (
            patch("agents.writer_agent.cfg.load_config", return_value={"gemini": {"api_keys": ["fake-key"], "models": {}}}),
            patch.object(WriterAgent, "_resolve_video_path", return_value=""),
            patch.object(WriterAgent, "_build_raw_segments", side_effect=self._raw_segments),
        ):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _raw_segments(merged, state, movie_path=""):
        return [
            {"id": i + 1, "start_sec": s["start_sec"], "end_sec": s["end_sec"], "duration_sec": 4.0,
             "max_chars": 60, "acoustic_gender": "unknown", "text": s["text"]}
            for i, s in enumerate(merged)
        ]

    def run_writer(self, fake, n_lines=30, state=None, **writer_kw):
        state = state or make_state(n_lines)
        with patch("agents.writer_agent.call_gemini", fake), \
             patch.object(WriterAgent, "_bridge_action_narration", side_effect=lambda blocks, *a, **k: blocks):
            writer = quiet_writer(**writer_kw)
            return writer.generate_script(state), writer


class EndToEndTests(WriterFlowTestCase):
    def test_script_blocks_keep_their_shape_and_gain_the_source_text(self):
        fake = FakeGemini()
        state, _ = self.run_writer(fake, 30)
        blocks = state.generated_script
        self.assertEqual(len(blocks), 30)
        first = blocks[0]
        for key in ("scene_id", "narration", "start_sec", "end_sec", "gender", "character", "emotion", "visual_cue"):
            self.assertIn(key, first)
        self.assertEqual(first["narration"], done(1))
        self.assertIn("fairly long spoken sentence number 0", first["source_text"])
        self.assertTrue(all("needs_review" not in b for b in blocks))
        self.assertIn("John", state.speaker_profiles)

    def test_story_bible_is_built_once_cached_and_sent_with_every_batch(self):
        fake = FakeGemini()
        state, _ = self.run_writer(fake, 30)
        self.assertEqual(len(fake.of("bible")), 1)
        self.assertIn("ဂျွန်", state.story_context)
        self.assertTrue(state.story_context_key)
        for _kind, _sys, prompt, _kw in fake.of("batch"):
            self.assertIn("STORY BIBLE", prompt)
            self.assertIn("ဂျွန်", prompt)
        # A second run on the same state (e.g. after a failure) reuses the cached bible.
        self.run_writer(fake, 30, state=state)
        self.assertEqual(len(fake.of("bible")), 1)

    def test_bible_is_skipped_for_short_clips_and_survives_failure(self):
        fake = FakeGemini()
        state, _ = self.run_writer(fake, 8)
        self.assertEqual(fake.of("bible"), [])
        self.assertFalse(state.story_context)

        def broken(system, prompt, key, model, **kw):
            if "script supervisor" in system:
                raise RuntimeError("quota")
            return FakeGemini()(system, prompt, key, model, **kw)
        state2, _ = self.run_writer(broken, 30)
        self.assertEqual(len(state2.generated_script), 30)
        self.assertFalse(state2.story_context)

    def test_batch_prompts_carry_neighbour_context_and_a_generous_token_budget(self):
        fake = FakeGemini()
        self.run_writer(fake, 45)
        first, second = fake.of("batch")[0], fake.of("batch")[1]
        self.assertNotIn("PREVIOUS LINES", first[2])
        self.assertIn("UPCOMING SOURCE LINES", first[2])
        self.assertIn("PREVIOUS LINES", second[2])
        self.assertTrue(done(20) in second[2], "the translation of line 20 must be the context")
        self.assertGreater(first[3]["max_tokens"], 4096)       # Burmese is token hungry: 4096 truncated batches

    def test_style_variants_all_receive_the_context_block(self):
        for style in ("recap", "dialogue", "persona", "wuxia", "drama_novel"):
            with self.subTest(style=style):
                state = make_state(3, translation_style=style)
                writer = quiet_writer()
                _sys, prompt = writer._build_batch_prompt(
                    [{"id": 1, "text": "Hello there"}], state,
                    {"previous": [{"id": 0, "narration": "ကြိုတင်"}], "next": [{"id": 2, "text": "Next line"}]}, "- id 1: fix it",
                )
                self.assertIn("PREVIOUS LINES", prompt)
                self.assertIn("UPCOMING SOURCE LINES", prompt)
                self.assertIn("CORRECTIONS REQUIRED", prompt)
                self.assertIn("- id 1: fix it", prompt)
                self.assertTrue(prompt.rstrip().endswith("exactly the lines in the list above."))

    def test_prompt_without_context_is_unchanged_in_shape(self):
        state = make_state(3, context_hint="Mary is the heroine")
        _sys, prompt = quiet_writer()._build_batch_prompt([{"id": 1, "text": "Hi"}], state)
        self.assertNotIn("PREVIOUS LINES", prompt)
        self.assertNotIn("CORRECTIONS REQUIRED", prompt)
        self.assertIn("USER STORY & CHARACTER GUIDANCE:\nMary is the heroine", prompt)


class QualityLoopTests(WriterFlowTestCase):
    def test_english_leftover_is_corrected_by_a_targeted_retry(self):
        seen = []

        def handler(ids, prompt, kwargs):
            seen.append(ids)
            items = [{"id": i, "narration": mm(i)} for i in ids]
            if len(seen) == 1:
                items[4]["narration"] = "I will never give up on this plan"
            return json.dumps(items)
        fake = FakeGemini(handler)
        state, _ = self.run_writer(fake, 10)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1], [5])                                  # only the bad line is re-asked
        self.assertIn("CORRECTIONS REQUIRED", fake.of("batch")[1][2])
        self.assertIn("not in Burmese script", fake.of("batch")[1][2])
        self.assertEqual(state.generated_script[4]["narration"], done(5))
        self.assertFalse(any(b.get("needs_review") for b in state.generated_script))

    def test_truncated_answer_is_recovered_and_only_the_missing_tail_is_re_asked(self):
        calls = []

        def handler(ids, prompt, kwargs):
            calls.append(ids)
            full = json.dumps([{"id": i, "narration": mm(i)} for i in ids], ensure_ascii=False)
            if len(calls) == 1:
                return full[: int(len(full) * 0.6)]                     # cut off mid-array, like a max-token stop
            return full
        fake = FakeGemini(handler)
        state, _ = self.run_writer(fake, 20)
        self.assertEqual(len(state.generated_script), 20)
        self.assertEqual([b["narration"] for b in state.generated_script], [done(i) for i in range(1, 21)])
        self.assertEqual(len(calls), 2)
        self.assertLess(len(calls[1]), 20)                              # a retry for the lost tail, not the whole batch
        self.assertEqual(fake.of("line"), [])

    def test_unresolvable_line_is_flagged_for_review_and_keeps_its_source(self):
        def handler(ids, prompt, kwargs):
            return json.dumps([{"id": i, "narration": ("Still English words only" if i == 3 else mm(i))} for i in ids])

        def line_gemini(system, prompt, key, model, **kw):
            if kw.get("response_mime_type") == "text/plain":
                return "More English words only", model
            return FakeGemini(handler)(system, prompt, key, model, **kw)
        state, _ = self.run_writer(line_gemini, 6)
        block = state.generated_script[2]
        self.assertTrue(block["needs_review"])
        self.assertIn("untranslated", block["issues"])
        self.assertEqual(block["narration"], "Still English words only")
        self.assertEqual([b["scene_id"] for b in state.generated_script if b.get("needs_review")], ["3"])

    def test_numbers_and_acronyms_are_converted_before_validation(self):
        def handler(ids, prompt, kwargs):
            return json.dumps([{"id": i, "narration": mm(i) + " CCTV 100"} for i in ids])
        state, _ = self.run_writer(FakeGemini(handler), 4)
        self.assertFalse(any(b.get("needs_review") for b in state.generated_script))
        self.assertNotIn("CCTV", state.generated_script[0]["narration"])
        self.assertNotRegex(state.generated_script[0]["narration"], r"[A-Za-z]")


class GuardTests(WriterFlowTestCase):
    def test_missing_api_key_fails_early_with_a_clear_message(self):
        with patch("agents.writer_agent.cfg.load_config", return_value={"gemini": {}}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEYS", None)
            os.environ.pop("GEMINI_API_KEY", None)
            with self.assertRaises(RuntimeError) as caught:
                quiet_writer().generate_script(make_state(5))
        self.assertIn("Gemini API key", str(caught.exception))
        self.assertIn("GEMINI_API_KEYS", str(caught.exception))

    def test_service_outage_stops_the_phase_instead_of_caching_a_bad_script(self):
        def down(system, prompt, key, model, **kw):
            if "script supervisor" in system:
                return BIBLE_JSON, model
            raise TimeoutError("network down")
        state = make_state(60)
        with patch("agents.writer_agent.call_gemini", down):
            with self.assertRaises(RuntimeError) as caught:
                quiet_writer().generate_script(state)
        self.assertIn("Gemini", str(caught.exception))
        self.assertFalse(state.generated_script)

    def test_stop_button_interrupts_between_calls(self):
        fake = FakeGemini()
        with patch.dict(os.environ, {"CURRENT_JOB_CANCELLED": "1"}):
            with patch("agents.writer_agent.call_gemini", fake):
                with self.assertRaises(InterruptedError):
                    quiet_writer().generate_script(make_state(30))


class ParseScriptTests(unittest.TestCase):
    def setUp(self):
        self.writer = quiet_writer()

    def test_complete_array(self):
        out = self.writer._parse_script('[{"id": 1, "narration": "က"}, {"id": 2, "narration": "ခ"}]')
        self.assertEqual([b["id"] for b in out], [1, 2])

    def test_truncated_array_keeps_every_complete_object(self):
        raw = '[{"id": 1, "narration": "က", "emotion": "normal"}, {"id": 2, "narration": "ခ"}, {"id": 3, "narrat'
        out = self.writer._parse_script(raw)
        self.assertEqual([b["id"] for b in out], [1, 2])

    def test_truncated_array_inside_markdown_fence(self):
        raw = '```json\n[{"id": 7, "narration": "ဂ"}, {"id": 8, "narr'
        out = self.writer._parse_script(raw)
        self.assertEqual([b["id"] for b in out], [7])

    def test_wrapped_object(self):
        out = self.writer._parse_script('{"translations": [{"id": 4, "narration": "ဃ"}]}')
        self.assertEqual([b["id"] for b in out], [4])

    def test_truncated_wrapped_object(self):
        out = self.writer._parse_script('{"translations": [{"id": 4, "narration": "ဃ"}, {"id": 5, "narration": "င')
        self.assertEqual([b["id"] for b in out], [4])

    def test_garbage_is_none_not_a_crash(self):
        self.assertIsNone(self.writer._parse_script("```json\n```"))


if __name__ == "__main__":
    unittest.main()
