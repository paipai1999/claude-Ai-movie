"""Translation-accuracy guards for the WriterAgent batch translator and the Gemini client."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from brain.gemini_client import TruncatedResponseError, _extract_text_from_gemini_response, call_gemini
from brain.memory import MovieState
from agents.writer_agent import WriterAgent


def _segments(n, text="你好 {i}"):
    return [
        {"id": i + 1, "start_sec": float(i * 3), "end_sec": float(i * 3 + 2.5), "duration_sec": 2.5,
         "max_chars": 27, "acoustic_gender": "unknown", "text": text.format(i=i + 1)}
        for i in range(n)
    ]


def _reply_for(batch, names=None):
    """A well-formed Gemini batch reply translating every id in the batch."""
    return json.dumps([
        {"id": s["id"], "narration": f"စာကြောင်း {s['id']}", "emotion": "normal",
         "character": "Hero", "gender": "male", "names": names or {}}
        for s in batch
    ], ensure_ascii=False)


def _ids_in_prompt(prompt):
    start = prompt.index("[")
    end = prompt.index("]\n\nOutput a JSON array")
    return [item["id"] for item in json.loads(prompt[start:end + 1])]


class TestTruncationDetection(unittest.TestCase):

    def _resp(self, finish, text='[{"id": 1, "narration": "abc"'):
        return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}]}

    def test_json_cut_off_at_max_tokens_raises(self):
        with self.assertRaises(TruncatedResponseError):
            _extract_text_from_gemini_response(self._resp("MAX_TOKENS"), expect_json=True)

    def test_plain_text_keeps_partial_output(self):
        self.assertEqual(_extract_text_from_gemini_response(self._resp("MAX_TOKENS", "partial"), expect_json=False), "partial")

    def test_normal_finish_is_returned(self):
        self.assertEqual(_extract_text_from_gemini_response(self._resp("STOP", "[]"), expect_json=True), "[]")

    @patch("brain.tracker.reserve_model_usage", return_value=True)
    @patch("brain.gemini_client._record_api_usage")
    @patch("urllib.request.urlopen")
    def test_call_gemini_does_not_burn_other_keys_on_truncation(self, mock_urlopen, _rec, _reserve):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self._resp("MAX_TOKENS")).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp
        with self.assertRaises(TruncatedResponseError):
            call_gemini("sys", "user", ["key-a", "key-b"], "gemini-x")
        self.assertEqual(mock_urlopen.call_count, 1)


class TestWriterBatchTranslation(unittest.TestCase):

    def setUp(self):
        self.agent = WriterAgent(language="burmese", script_engine="translate")
        self.state = MovieState(movie_name="Test")

    def _run(self, segments, fake_call):
        with patch("agents.writer_agent.call_gemini", side_effect=fake_call):
            return self.agent._translate_batches(segments, self.state, "", "dummy-key", "gemini-x")

    def test_failed_line_is_left_silent_not_voiced_in_source_language(self):
        segs = _segments(2)

        def fake_call(sys_prompt, user_prompt, *a, **kw):
            if kw.get("response_mime_type") == "text/plain":
                raise RuntimeError("all keys exhausted")
            return json.dumps([{"id": 1, "narration": "မင်္ဂလာပါ"}], ensure_ascii=False), "gemini-x"

        out = self._run(segs, fake_call)
        self.assertEqual(out[0]["narration"], "မင်္ဂလာပါ")
        self.assertEqual(out[1]["narration"], "")
        self.assertTrue(out[1]["untranslated"])
        self.assertNotIn("你好", out[1]["narration"])
        self.assertEqual([u["scene_id"] for u in self.state.untranslated_lines], ["2"])

    def test_dropped_line_recovered_by_single_line_retry(self):
        segs = _segments(2)

        def fake_call(sys_prompt, user_prompt, *a, **kw):
            if kw.get("response_mime_type") == "text/plain":
                return "နှုတ်ဆက်ပါတယ်", "gemini-x"
            return json.dumps([{"id": 1, "narration": "မင်္ဂလာပါ"}], ensure_ascii=False), "gemini-x"

        out = self._run(segs, fake_call)
        self.assertEqual(out[1]["narration"], "နှုတ်ဆက်ပါတယ်")
        self.assertNotIn("untranslated", out[1])
        self.assertEqual(self.state.untranslated_lines, [])

    def test_truncated_batch_is_split_and_every_line_translated(self):
        segs = _segments(20)
        calls = []

        def fake_call(sys_prompt, user_prompt, *a, **kw):
            ids = _ids_in_prompt(user_prompt)
            calls.append(len(ids))
            if len(ids) > 10:
                raise TruncatedResponseError("cut off")
            return _reply_for([s for s in segs if s["id"] in ids]), "gemini-x"

        out = self._run(segs, fake_call)
        self.assertEqual(calls, [20, 10, 10])
        self.assertEqual(len(out), 20)
        self.assertTrue(all(b["narration"] for b in out))
        self.assertEqual(self.state.untranslated_lines, [])

    def test_names_from_first_batch_reach_second_batch_prompt(self):
        segs = _segments(25)
        prompts = []

        def fake_call(sys_prompt, user_prompt, *a, **kw):
            prompts.append(user_prompt)
            ids = _ids_in_prompt(user_prompt)
            names = {"Li Wei": "လီဝေ"} if 1 in ids else {}
            return _reply_for([s for s in segs if s["id"] in ids], names), "gemini-x"

        self._run(segs, fake_call)
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("Li Wei -> လီဝေ", prompts[0])
        self.assertIn("Li Wei -> လီဝေ", prompts[1])
        self.assertIn("PREVIOUS LINES", prompts[1])
        self.assertEqual(self.state.name_glossary, {"Li Wei": "လီဝေ"})

    def test_first_spelling_of_a_name_wins(self):
        glossary = {}
        self.agent._absorb_names({"names": {"Li Wei": "လီဝေ"}}, glossary)
        self.agent._absorb_names({"names": {"Li Wei": "လီဝိုင်"}}, glossary)
        self.assertEqual(glossary, {"Li Wei": "လီဝေ"})

    def test_context_hint_seeds_glossary(self):
        hint = "Li Wei -> လီဝေ\nThe hero is a doctor.\nMei: မေ"
        self.assertEqual(self.agent._glossary_from_hint(hint), {"Li Wei": "လီဝေ", "Mei": "မေ"})


if __name__ == "__main__":
    unittest.main()
