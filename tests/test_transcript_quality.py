"""
core.transcript_quality - Whisper repetition-loop / artefact filter, and its wiring into Engine 2/3's
``transcribe_media``.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import transcript_quality as tq  # noqa: E402


def seg(text, start=0.0):
    return {"start": start, "end": start + 1.0, "text": text}


def texts(items):
    return [i["text"] for i in items]


class CleanTranscriptTests(unittest.TestCase):
    def test_normal_dialogue_is_untouched(self):
        lines = [seg(f"This is distinct line number {n} of the film.", n) for n in range(30)]
        kept, report = tq.clean_transcript(lines)
        self.assertEqual(kept, lines)
        self.assertEqual((report.dropped, report.unusable), (0, False))

    def test_three_copies_in_a_row_keeps_the_first_two_only(self):
        kept, report = tq.clean_transcript([seg("I will find you wherever you hide.")] * 3)
        self.assertEqual(len(kept), 2)
        self.assertEqual(report.looped, 1)

    def test_natural_double_repeat_is_kept(self):
        kept, _ = tq.clean_transcript([seg("Run, run for your life!"), seg("Run, run for your life!")])
        self.assertEqual(len(kept), 2)

    def test_loop_is_matched_ignoring_case_and_punctuation(self):
        kept, report = tq.clean_transcript([seg("Thank you so much."), seg("thank you so much!"), seg("THANK YOU SO MUCH")])
        self.assertEqual((len(kept), report.looped), (2, 1))

    def test_alternating_loop_is_caught(self):
        a, b = seg("We have to leave this place now."), seg("Not without the others, no way.")
        kept, report = tq.clean_transcript([a, b, a, b, a, b, a, b])
        self.assertEqual(len(kept), 4)
        self.assertEqual(report.looped, 4)

    def test_short_answers_are_never_a_loop(self):
        kept, report = tq.clean_transcript([seg("Yes."), seg("No."), seg("Yes."), seg("No."), seg("Yes."), seg("Yes!")])
        self.assertEqual(len(kept), 6)
        self.assertEqual(report.looped, 0)

    def test_same_line_far_apart_is_a_chorus_not_a_loop(self):
        line = seg("I will always love you, my darling.")
        filler = [seg(f"Completely different sentence number {n} here.") for n in range(10)]
        kept, report = tq.clean_transcript([line, line, *filler, line, line])
        self.assertEqual(report.looped, 0)
        self.assertEqual(len(kept), 14)

    def test_long_loop_stays_suppressed(self):
        kept, report = tq.clean_transcript([seg("Please subscribe to the channel now")] * 40)
        self.assertEqual(len(kept), 2)
        self.assertEqual(report.looped, 38)

    def test_credit_artefacts_are_dropped(self):
        lines = [
            seg("Subtitles by the Amara.org community"),
            seg("We meet again, Detective."),
            seg("字幕由Amara.org社区提供"),
            seg("Visit www.example.com for more"),
        ]
        kept, report = tq.clean_transcript(lines)
        self.assertEqual(texts(kept), ["We meet again, Detective."])
        self.assertEqual(report.artefacts, 3)

    def test_long_line_mentioning_a_site_is_real_speech(self):
        line = seg("I read about it on www.nytimes.com yesterday and the whole story is far worse than they admit publicly.")
        kept, report = tq.clean_transcript([line])
        self.assertEqual(kept, [line])
        self.assertEqual(report.artefacts, 0)

    def test_all_loop_transcript_is_unusable(self):
        kept, report = tq.clean_transcript([seg("Thanks for watching this video")] * 60)
        self.assertTrue(report.unusable)
        self.assertEqual(report.kept, 2)

    def test_short_transcript_is_never_judged_unusable(self):
        _, report = tq.clean_transcript([seg("Thanks for watching this video")] * 5)
        self.assertFalse(report.unusable)

    def test_mostly_real_speech_with_a_short_loop_stays_usable(self):
        lines = [seg(f"Unique sentence number {n} in this scene.", n) for n in range(40)]
        lines[10:10] = [seg("Thanks for watching this video")] * 8
        kept, report = tq.clean_transcript(lines)
        self.assertFalse(report.unusable)
        self.assertEqual(len(kept), 42)

    def test_works_on_objects_with_a_text_attribute_and_does_not_mutate_input(self):
        items = [SimpleNamespace(text="Same line again and again")] * 4
        snapshot = list(items)
        kept, _ = tq.clean_transcript(items)
        self.assertEqual(len(kept), 2)
        self.assertIs(kept[0], items[0])
        self.assertEqual(items, snapshot)

    def test_empty_input(self):
        kept, report = tq.clean_transcript([])
        self.assertEqual((kept, report.total, report.unusable), ([], 0, False))


class CleanAndReportTests(unittest.TestCase):
    def test_logs_only_when_something_was_removed(self):
        logs = []
        tq.clean_and_report([seg("A perfectly ordinary sentence here.")], logs.append)
        self.assertEqual(logs, [])
        tq.clean_and_report([seg("Looping sentence goes here again")] * 3, logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn("repeated", logs[0])

    def test_unusable_transcript_returns_empty_with_a_bilingual_warning(self):
        logs = []
        out = tq.clean_and_report([seg("Thanks for watching this video")] * 50, logs.append)
        self.assertEqual(out, [])
        warning = "\n".join(logs)
        self.assertIn("looped", warning)
        self.assertIn("Whisper", warning)
        self.assertTrue(any("က" <= ch <= "႟" for ch in warning), "Burmese hint expected")


class TranscribeMediaWiringTests(unittest.TestCase):
    """Engine 2/3's shared transcribe_media must apply the filter and the anti-loop settings."""

    def _run(self, whisper_segments):
        from core import transcribe as tr

        captured = {}

        class FakeModel:
            def transcribe(self, path, **kwargs):
                captured.update(kwargs)
                return iter(whisper_segments), SimpleNamespace(language="en")

        def fake_extract(cmd, **_kw):
            open(cmd[-1], "wb").write(b"\0" * 400)   # pretend ffmpeg produced a WAV
            return SimpleNamespace(returncode=0)

        with patch.object(tr.subprocess, "run", side_effect=fake_extract), \
             patch.object(tr, "load_faster_whisper", return_value=(FakeModel(), "base", "cpu")):
            out = tr.transcribe_media("ffmpeg", "movie.mp4", "en", {}, temp_prefix="tq_test", log=lambda _m: None)
        return out, captured

    @staticmethod
    def _w(text, start):
        return SimpleNamespace(text=text, start=start, end=start + 1.0, words=None)

    def test_loop_removed_and_anti_hallucination_kwargs_passed(self):
        heard = [self._w("Please like and subscribe now", n) for n in range(6)] + [self._w("The real dialogue starts here.", 10)]
        out, kwargs = self._run(heard)
        self.assertEqual([s["text"] for s in out].count("Please like and subscribe now"), 2)
        self.assertEqual(out[-1]["text"], "The real dialogue starts here.")
        self.assertIs(kwargs["condition_on_previous_text"], False)
        self.assertEqual(kwargs["hallucination_silence_threshold"], 2.0)
        self.assertTrue(kwargs["word_timestamps"])  # required by hallucination_silence_threshold

    def test_pure_loop_returns_no_transcript(self):
        out, _ = self._run([self._w("Thanks for watching this video", n) for n in range(40)])
        self.assertEqual(out, [])



class OlderFasterWhisperTests(unittest.TestCase):
    """requirements.txt allows faster-whisper>=1.0.0; an older install must not crash on newer options."""

    def _run(self, model):
        from core import transcribe as tr

        def fake_extract(cmd, **_kw):
            open(cmd[-1], "wb").write(b"\0" * 400)
            return SimpleNamespace(returncode=0)

        logs = []
        with patch.object(tr.subprocess, "run", side_effect=fake_extract), \
             patch.object(tr, "load_faster_whisper", return_value=(model, "base", "cpu")):
            out = tr.transcribe_media("ffmpeg", "movie.mp4", "en", {}, temp_prefix="tq_old", log=logs.append)
        return out, logs

    def test_unknown_options_are_dropped_with_a_hint_instead_of_failing(self):
        seen = {}

        class OldModel:
            # no **kwargs and no hallucination_silence_threshold, like an early 1.0.x release
            def transcribe(self, path, language=None, beam_size=5, vad_filter=False, word_timestamps=False,
                           condition_on_previous_text=True, vad_parameters=None):
                seen.update(language=language, condition=condition_on_previous_text)
                return iter([SimpleNamespace(text="A real sentence of dialogue here.", start=0.0, end=2.0, words=None)]), None

        out, logs = self._run(OldModel())
        self.assertEqual([s["text"] for s in out], ["A real sentence of dialogue here."])
        self.assertIs(seen["condition"], False)                       # the supported option still applies
        self.assertTrue(any("hallucination_silence_threshold" in m and "faster-whisper" in m for m in logs))

    def test_models_that_accept_everything_get_everything(self):
        got = {}

        class NewModel:
            def transcribe(self, path, **kwargs):
                got.update(kwargs)
                return iter([]), None

        _out, logs = self._run(NewModel())
        self.assertEqual(got["hallucination_silence_threshold"], 2.0)
        self.assertFalse(any("does not support" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
