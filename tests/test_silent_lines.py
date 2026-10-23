"""
Lines that could not be translated are left silent instead of being read aloud in the wrong language.

The writer marks them (``untranslated``); one rule, ``block_is_silent``, decides - the voice agent, the resume check,
the subtitles-only export, the Script Editor, the quality report and the CapCut package all follow it. A human edit
that gives the line a Burmese text makes it speakable again.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.translation_quality import UNSPEAKABLE_CODES, block_is_silent, is_unspeakable  # noqa: E402

BURMESE = "ဒီနေ့ ရာသီဥတု ကောင်းပါတယ်"


def block(text=BURMESE, **extra):
    return dict({"scene_id": "1", "narration": text, "start_sec": 0.0, "end_sec": 4.0, "gender": "male", "emotion": "normal"}, **extra)


class RuleTests(unittest.TestCase):
    def test_which_problems_leave_a_line_without_usable_narration(self):
        self.assertEqual(UNSPEAKABLE_CODES, {"missing", "placeholder", "untranslated", "copied_source"})
        for code in UNSPEAKABLE_CODES:
            self.assertTrue(is_unspeakable([code]), code)
        for code in ("latin_words", "meta_text", "foreign_script", "duplicate", "repeat_boundary", "over_budget"):
            self.assertFalse(is_unspeakable([code]), code)     # imperfect, but still a Burmese line worth speaking
        self.assertTrue(is_unspeakable(["latin_words", "untranslated"]))
        self.assertFalse(is_unspeakable([]))
        self.assertFalse(is_unspeakable(None))

    def test_a_block_is_silent_when_marked_or_still_flagged_without_narration(self):
        self.assertTrue(block_is_silent(block("English", untranslated=True)))
        self.assertTrue(block_is_silent(block("English", needs_review=True, issues=["untranslated"])))      # made before the mark existed
        self.assertFalse(block_is_silent(block(needs_review=True, issues=["latin_words"])))
        self.assertFalse(block_is_silent(block(issues=["untranslated"])))        # a human reviewed it (needs_review cleared)
        self.assertFalse(block_is_silent(block()))
        for bad in (None, "text", 5, []):
            self.assertFalse(block_is_silent(bad))


class WriterTests(unittest.TestCase):
    def test_the_writer_marks_only_the_lines_it_could_not_translate(self):
        from tests.test_writer_flow import FakeGemini, WriterFlowTestCase, mm

        class Case(WriterFlowTestCase):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)

        def handler(ids, prompt, kwargs):
            return json.dumps([{"id": i, "narration": ("Still English words only" if i == 3 else mm(i))} for i in ids])

        def gemini(system, prompt, key, model, **kw):
            if kw.get("response_mime_type") == "text/plain":
                return "More English words only", model
            return FakeGemini(handler)(system, prompt, key, model, **kw)

        state, _ = case.run_writer(gemini, 6)
        marked = [b["scene_id"] for b in state.generated_script if b.get("untranslated")]
        self.assertEqual(marked, ["3"])
        self.assertTrue(all(block_is_silent(b) == (b["scene_id"] == "3") for b in state.generated_script))


class VoiceTests(unittest.TestCase):
    def setUp(self):
        from agents.voice_agent import VoiceAgent
        from brain.memory import MovieState
        self.tmp = tempfile.mkdtemp(prefix="pai_silent_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "voiceover")
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.engine, agent.voice, agent.language = "edge_tts", "my-MM-ThihaNeural", "burmese"
        agent.rate_mm = agent.rate_en = "+0%"
        agent.output_dir = self.out
        agent.f5_cfg, agent.f5_engine = {}, None
        self.agent = agent
        self.spoken = []

        async def speak(this, text, out_file, scene_id, emotion, **kwargs):
            self.spoken.append(text)
            with open(out_file, "wb") as fh:
                fh.write(b"\0" * 2048)
            return out_file

        patcher = mock.patch.object(VoiceAgent, "_speak_with_retry", speak)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = MovieState(movie_name="Film")
        self.state.generated_script = [
            block("ပထမစာကြောင်း ဖြစ်ပါတယ်", scene_id="1"),
            block("Chinese text left behind", scene_id="2", untranslated=True, needs_review=True, issues=["untranslated"]),
            block("Another untranslated line", scene_id="3", needs_review=True, issues=["untranslated"]),     # no mark: an older project
            block("စတုတ္ထစာကြောင်း ဖြစ်ပါတယ်", scene_id="4", needs_review=True, issues=["latin_words"]),     # imperfect but Burmese
        ]

    def clips(self):
        return sorted(n for n in os.listdir(self.out) if n.endswith(".mp3"))

    def test_untranslated_lines_get_no_clip_and_are_never_spoken(self):
        self.agent.generate_voiceover(self.state)
        self.assertEqual(self.clips(), ["scene_0001.mp3", "scene_0004.mp3"])
        self.assertEqual(len(self.spoken), 2)
        self.assertFalse(any("Chinese" in s or "untranslated" in s for s in self.spoken))
        manifest = json.load(open(os.path.join(self.out, "voice_manifest.json"), encoding="utf-8"))
        self.assertEqual(sorted(manifest["clips"]), ["scene_0001.mp3", "scene_0004.mp3"])

    def test_a_second_run_reuses_the_clips_and_does_not_wait_for_silent_ones(self):
        self.agent.generate_voiceover(self.state)
        spoken = len(self.spoken)
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.spoken), spoken)
        self.assertEqual(self.clips(), ["scene_0001.mp3", "scene_0004.mp3"])

    def test_the_clip_of_a_line_that_became_silent_is_removed(self):
        os.makedirs(self.out)
        for name in ("scene_0001.mp3", "scene_0002.mp3"):
            with open(os.path.join(self.out, name), "wb") as fh:
                fh.write(b"\0" * 2048)
        self.state.generated_script[1].pop("untranslated")
        self.agent.generate_voiceover(self.state)         # line 2 is voiced once (no flag, no review issue to silence it)
        self.assertIn("scene_0002.mp3", self.clips())
        self.state.generated_script[1].update(untranslated=True, needs_review=True, issues=["untranslated"])
        self.agent.generate_voiceover(self.state)
        self.assertNotIn("scene_0002.mp3", self.clips())

    def test_translating_a_silent_line_voices_it(self):
        self.agent.generate_voiceover(self.state)
        line = self.state.generated_script[1]
        for key in ("untranslated", "needs_review", "issues"):
            line.pop(key, None)
        line["narration"] = "ဒုတိယစာကြောင်း ဘာသာပြန်ပြီးပါပြီ"
        self.agent.generate_voiceover(self.state)
        self.assertIn("scene_0002.mp3", self.clips())
        self.assertTrue(any("ဒုတိယစာကြောင်း ဘာသာပြန်ပြီးပါပြီ" in text for text in self.spoken))        # (the TTS text gets its own punctuation)


class MasterTests(unittest.TestCase):
    def setUp(self):
        from agents.master import MasterAgent
        self.out = tempfile.mkdtemp(prefix="pai_silent_master_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.master = MasterAgent("dummy_movie.mp4")
        self.master.output_dir = self.out
        self.master.state.generated_script = [block("ပထမ ဖြစ်ပါတယ်"), block("English", untranslated=True), block("တတိယ ဖြစ်ပါတယ်")]
        self.voice_dir = os.path.join(self.out, self.master.state.project_dir, "voiceover")
        os.makedirs(self.voice_dir)

    def clip(self, number):
        with open(os.path.join(self.voice_dir, f"scene_{number:04d}.mp3"), "wb") as fh:
            fh.write(b"\0" * 2048)

    def test_the_voice_phase_is_complete_when_every_spoken_line_has_its_clip(self):
        from agents.master import PHASE_5_VOICE
        self.master.tts_enabled = True
        self.clip(1)
        self.assertFalse(self.master._validate_artifacts(PHASE_5_VOICE))
        self.clip(3)
        self.assertTrue(self.master._validate_artifacts(PHASE_5_VOICE))       # the silent line has nothing to wait for

    def test_the_subtitles_only_export_leaves_silent_lines_out(self):
        self.master.render_video = False
        captured = []
        self.master.video_merger = types.SimpleNamespace(_export_standalone_srt=lambda timings, out: captured.extend(timings))
        self.master.save_checkpoint = lambda *a, **k: None
        self.master.state.subtitle_timings = []
        self.master._run_phase_6_merge()
        self.assertEqual([text for _s, _d, text in captured], ["ပထမ ဖြစ်ပါတယ်", "တတိယ ဖြစ်ပါတယ်"])


class ProjectTests(unittest.TestCase):
    def setUp(self):
        from services import projects
        self.pj = projects
        self.dir = tempfile.mkdtemp(prefix="pai_silent_pj_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        script = [block("ပထမ ဖြစ်ပါတယ်", scene_id="1"),
                  block("Left in English", scene_id="2", untranslated=True, needs_review=True, issues=["untranslated"]),
                  block("တတိယ ဖြစ်ပါတယ်", scene_id="3")]
        self.write_state(script)
        os.makedirs(os.path.join(self.dir, "voiceover"))
        for number in (1, 3):
            with open(os.path.join(self.dir, "voiceover", f"scene_{number:04d}.mp3"), "wb") as fh:
                fh.write(b"\0" * 2048)

    def write_state(self, script):
        with open(os.path.join(self.dir, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"movie_name": "Film", "language": "burmese", "generated_script": script,
                       "completed_phases": ["phase_5_voice_generation", "phase_6_video_merge"]}, fh, ensure_ascii=False)

    def script(self):
        return json.load(open(os.path.join(self.dir, "state.json"), encoding="utf-8"))["generated_script"]

    def test_the_editor_sees_which_lines_are_silent(self):
        view = self.pj.load_script(self.dir, "Film")
        self.assertEqual([b["untranslated"] for b in view["blocks"]], [False, True, False])
        self.assertEqual(view["summary"]["silent"], 1)

    def test_a_burmese_translation_makes_the_line_speakable_again(self):
        self.pj.save_script(self.dir, [{"index": 1, "narration": "ဒုတိယစာကြောင်း ဘာသာပြန်ပြီးပါပြီ"}])
        line = self.script()[1]
        self.assertNotIn("untranslated", line)
        self.assertFalse(block_is_silent(line))

    def test_a_text_that_is_still_not_burmese_keeps_the_line_silent(self):
        self.pj.save_script(self.dir, [{"index": 1, "narration": "Still English, just reworded"}])
        line = self.script()[1]
        self.assertTrue(line["untranslated"])
        self.assertTrue(block_is_silent(line))
        self.pj.save_script(self.dir, [{"index": 0, "narration": "Oops an English edit"}])      # also a normally spoken line
        self.assertTrue(block_is_silent(self.script()[0]))

    def test_changing_only_the_voice_does_not_un_silence_a_line(self):
        self.pj.save_script(self.dir, [{"index": 1, "gender": "female"}])
        self.assertTrue(block_is_silent(self.script()[1]))

    def test_the_quality_report_counts_silent_lines_and_does_not_call_them_missing_clips(self):
        with mock.patch.object(self.pj, "_clip_durations", return_value={0: 2.0, 2: 2.0}):     # the two spoken lines have clips
            report = self.pj.quality_report(self.dir, "Film")
        self.assertEqual(report["script"]["silent"], 1)
        self.assertEqual(report["voice"]["missing_clips"], 0)
        self.assertEqual(report["voice"]["clips"], 2)
        self.assertIn(report["verdict"], ("review", "attention"))

    def test_a_silent_line_that_a_human_confirmed_still_counts(self):
        script = self.script()
        script[1].pop("needs_review")
        script[1].pop("issues")                      # confirmed with "mark as checked": the mark stays, the line stays silent
        self.write_state(script)
        with mock.patch.object(self.pj, "_clip_durations", return_value={0: 2.0, 2: 2.0}):
            report = self.pj.quality_report(self.dir, "Film")
        self.assertEqual(report["script"]["silent"], 1)
        self.assertNotEqual(report["verdict"], "ready")


class CapcutTests(unittest.TestCase):
    def test_a_package_without_a_voice_over_carries_no_untranslated_text(self):
        from services import capcut_export
        base = tempfile.mkdtemp(prefix="pai_silent_cc_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        script = [block("ပထမ ဖြစ်ပါတယ်", scene_id="1"),
                  dict(block("Left in English", scene_id="2", untranslated=True), start_sec=5.0, end_sec=9.0),
                  dict(block("တတိယ ဖြစ်ပါတယ်", scene_id="3"), start_sec=10.0, end_sec=14.0)]
        with open(os.path.join(base, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"movie_name": "Film", "generated_script": script, "completed_phases": []}, fh, ensure_ascii=False)
        info = capcut_export.export_capcut_package(base, "Film")
        self.assertFalse(info["narration"])
        with zipfile.ZipFile(os.path.join(base, "capcut_package.zip")) as zf:
            srt = zf.read("subtitles.srt").decode("utf-8-sig")
            script_txt = zf.read("script.txt").decode("utf-8")
        for text in (srt, script_txt):
            self.assertIn("ပထမ ဖြစ်ပါတယ်", text)
            self.assertIn("တတိယ ဖြစ်ပါတယ်", text)
            self.assertNotIn("Left in English", text)
        self.assertEqual(info["lines"], 2)


if __name__ == "__main__":
    unittest.main()
