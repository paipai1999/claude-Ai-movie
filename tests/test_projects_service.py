"""services.projects - script editor, quality report, saved job options, listing (no FastAPI)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import projects as pj  # noqa: E402


def block(i, narration="ဟုတ်ကဲ့ပါ ဒီလိုပါပဲ", start=None, **extra):
    start = float(i * 5 if start is None else start)
    data = {
        "scene_id": str(i + 1), "narration": narration, "start_sec": start, "end_sec": start + 4.0,
        "gender": "male", "character": "John", "emotion": "normal", "visual_cue": "x",
        "source_text": f"Source line {i + 1}",
    }
    data.update(extra)
    return data


ALL_PHASES = [
    "phase_1_video_analysis", "phase_2_audio_stt", "phase_3_scene_detection", "phase_4_script_seo_thumbnail",
    "phase_5_voice_generation", "phase_6_video_merge", "phase_7_qa",
]


def write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pai_projects_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.dir = os.path.join(self.base, "My Film")
        os.makedirs(os.path.join(self.dir, "voiceover"))
        self.script = [
            block(0),
            block(1, narration="Still English leftovers", needs_review=True, issues=["untranslated"]),
            block(2, narration="ဒီစာကြောင်းက အရမ်းရှည်လွန်းပါတယ် " * 8),   # far over its slot budget
            block(3),
        ]
        self.state = {
            "movie_name": "My Film", "project_dir": "My Film", "generated_script": self.script,
            "completed_phases": list(ALL_PHASES), "phase_checkpoints": {p: {"ok": 1} for p in ALL_PHASES},
            "phase_statuses": {p: "COMPLETED" for p in ALL_PHASES}, "pipeline_status": "COMPLETED",
            "clean_video_path": "x/clean.mp4", "reels_video_path": "x/reels.mp4",
            "total_duration_formatted": "03:10", "warnings": [], "errors": [],
        }
        write(os.path.join(self.dir, "state.json"), self.state)
        write(os.path.join(self.dir, "checkpoint.json"), {
            "completed_phases": list(ALL_PHASES), "phase_checkpoints": {p: {} for p in ALL_PHASES},
            "phase_statuses": {p: "COMPLETED" for p in ALL_PHASES}, "script_blocks_count": 4,
        })


class ResolveTests(ProjectCase):
    def test_resolves_inside_the_base_only(self):
        self.assertEqual(pj.resolve_project(self.base, "My Film"), os.path.abspath(self.dir))
        for bad in ("../etc", "", "..", "My Film/../../x", "missing"):
            with self.subTest(name=bad):
                with self.assertRaises(pj.ProjectError) as caught:
                    pj.resolve_project(self.base, bad)
                self.assertEqual(caught.exception.code, "not_found")

    def test_phase_ids_match_the_master_agent(self):
        from agents import master
        self.assertEqual(pj.PHASE_VOICE, master.PHASE_5_VOICE)
        self.assertEqual(pj.PHASE_MERGE, master.PHASE_6_MERGE)
        self.assertEqual(pj.PHASE_QA, master.PHASE_7_QA)


class LoadScriptTests(ProjectCase):
    def test_view_of_every_block(self):
        view = pj.load_script(self.dir, "My Film")
        self.assertTrue(view["editable"])
        self.assertEqual(len(view["blocks"]), 4)
        first, second, third = view["blocks"][:3]
        self.assertEqual((first["index"], first["source_text"], first["narration"]), (0, "Source line 1", self.script[0]["narration"]))
        self.assertTrue(second["needs_review"])
        self.assertEqual(second["issues"], ["untranslated"])
        self.assertTrue(third["over_budget"])
        self.assertEqual(first["max_chars"], 44)          # 4 s slot * 11 chars/s
        self.assertEqual(view["summary"], {"total": 4, "needs_review": 1, "edited": 0, "over_budget": 1})
        self.assertFalse(view["has_saved_options"])
        self.assertFalse(view["rendered"])

    def test_non_recap_projects_are_not_editable(self):
        write(os.path.join(self.dir, "state.json"), {"engine_type": "subtitle", "movie_name": "x"})
        view = pj.load_script(self.dir, "My Film")
        self.assertFalse(view["editable"])
        self.assertIn("recap", view["reason"])

    def test_unreadable_state(self):
        os.remove(os.path.join(self.dir, "state.json"))
        with self.assertRaises(pj.ProjectError):
            pj.load_script(self.dir)


class SaveScriptTests(ProjectCase):
    def test_edit_marks_the_block_clears_review_flags_and_uncompletes_the_render_phases(self):
        result = pj.save_script(self.dir, [{"index": 1, "narration": "  ဒီစာကြောင်းကို   ပြင်လိုက်ပါပြီ  "}])
        self.assertEqual((result["changed"], result["lines"]), (1, [1]))
        self.assertEqual(sorted(result["invalidated"]), sorted(pj.RERENDER_PHASES))

        state = read(os.path.join(self.dir, "state.json"))
        edited = state["generated_script"][1]
        self.assertEqual(edited["narration"], "ဒီစာကြောင်းကို ပြင်လိုက်ပါပြီ")     # whitespace collapsed
        self.assertTrue(edited["edited"])
        self.assertNotIn("needs_review", edited)
        self.assertNotIn("issues", edited)
        self.assertEqual(state["generated_script"][0]["narration"], self.script[0]["narration"])   # untouched
        self.assertEqual(state["completed_phases"], ALL_PHASES[:4])
        for phase in pj.RERENDER_PHASES:
            self.assertNotIn(phase, state["phase_checkpoints"])
            self.assertNotIn(phase, state["phase_statuses"])
        self.assertIsNone(state["clean_video_path"])
        self.assertIsNone(state["reels_video_path"])
        self.assertIn("script_edited_at", state)

        ckpt = read(os.path.join(self.dir, "checkpoint.json"))
        self.assertEqual(ckpt["completed_phases"], ALL_PHASES[:4])     # or the resume would bring the phases back
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state.json.tmp")))

    def test_script_text_file_is_regenerated(self):
        result = pj.save_script(self.dir, [{"index": 0, "narration": "စာသားအသစ်ပြောင်းလိုက်ပါပြီ"}])
        self.assertTrue(result["script_txt_updated"])
        with open(os.path.join(self.dir, "final_recap_script.txt"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("စာသားအသစ်ပြောင်းလိုက်ပါပြီ", text)

    def test_gender_change_alone_counts_as_an_edit(self):
        result = pj.save_script(self.dir, [{"index": 3, "gender": "FEMALE"}])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(read(os.path.join(self.dir, "state.json"))["generated_script"][3]["gender"], "female")

    def test_unchanged_text_changes_nothing(self):
        before = read(os.path.join(self.dir, "state.json"))
        result = pj.save_script(self.dir, [{"index": 0, "narration": self.script[0]["narration"]}])
        self.assertEqual((result["changed"], result["invalidated"]), (0, []))
        after = read(os.path.join(self.dir, "state.json"))
        self.assertEqual(after["completed_phases"], before["completed_phases"])
        self.assertNotIn("script_edited_at", after)

    def test_confirming_a_flagged_line_unchanged_clears_the_flag_without_a_re_render(self):
        result = pj.save_script(self.dir, [{"index": 1, "narration": "Still English leftovers"}])
        self.assertEqual(result["changed"], 0)
        state = read(os.path.join(self.dir, "state.json"))
        self.assertNotIn("needs_review", state["generated_script"][1])
        self.assertTrue(state["generated_script"][1]["edited"])
        self.assertEqual(state["completed_phases"], ALL_PHASES)         # nothing to redo

    def test_invalid_edits_are_rejected_and_nothing_is_written(self):
        before = open(os.path.join(self.dir, "state.json"), "rb").read()
        bad_calls = [
            [],
            "not a list",
            [{"index": 99, "narration": "က"}],
            [{"index": -1, "narration": "က"}],
            [{"index": True, "narration": "က"}],
            [{"index": "1", "narration": "က"}],
            [{"index": 0, "narration": "   "}],
            [{"index": 0, "narration": "က" * (pj.MAX_NARRATION_CHARS + 1)}],
            [{"index": 0, "gender": "robot"}],
            [{"index": 0}],
            [{"index": 0, "narration": "က"}, {"index": 0, "narration": "ခ"}],
            [{"index": 0, "narration": "က"}, "oops"],
        ]
        for edits in bad_calls:
            with self.subTest(edits=str(edits)[:40]):
                with self.assertRaises(pj.ProjectError):
                    pj.save_script(self.dir, edits)
        self.assertEqual(open(os.path.join(self.dir, "state.json"), "rb").read(), before)

    def test_control_characters_are_stripped(self):
        pj.save_script(self.dir, [{"index": 0, "narration": "က\x00ခ​ဂ\x07\nဃ"}])
        self.assertEqual(read(os.path.join(self.dir, "state.json"))["generated_script"][0]["narration"], "ကခဂ ဃ")

    def test_project_without_script_cannot_be_edited(self):
        write(os.path.join(self.dir, "state.json"), {"movie_name": "x"})
        with self.assertRaises(pj.ProjectError) as caught:
            pj.save_script(self.dir, [{"index": 0, "narration": "က"}])
        self.assertEqual(caught.exception.code, "no_script")

    def test_voice_manifest_marks_exactly_the_edited_clip_as_stale(self):
        """The edit relies on VoiceAgent's per-clip fingerprints: only the changed line is re-synthesized."""
        from agents.voice_agent import VoiceAgent
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.engine, agent.voice = "edge_tts", "my-MM-ThihaNeural"
        before = [agent._clip_fingerprint(b) for b in read(os.path.join(self.dir, "state.json"))["generated_script"]]
        pj.save_script(self.dir, [{"index": 2, "narration": "တိုတိုလေးပါ"}])
        after = [agent._clip_fingerprint(b) for b in read(os.path.join(self.dir, "state.json"))["generated_script"]]
        self.assertEqual([a != b for a, b in zip(before, after)], [False, False, True, False])


class JobOptionsTests(ProjectCase):
    def test_roundtrip(self):
        self.assertIsNone(pj.load_job_options(self.dir))
        pj.save_job_options(self.dir, {"input": "movies/a.mp4", "language": "burmese", "resume": True})
        self.assertEqual(pj.load_job_options(self.dir), {"input": "movies/a.mp4", "language": "burmese", "resume": True})
        self.assertTrue(pj.load_script(self.dir)["has_saved_options"])

    def test_unwritable_location_is_not_fatal(self):
        pj.save_job_options(os.path.join(self.dir, "state.json", "nope"), {"a": 1})   # a file in the way


class QualityReportTests(ProjectCase):
    def _clip(self, index, seconds, rate=24000):
        import numpy as np
        import soundfile as sf
        path = os.path.join(self.dir, "voiceover", f"scene_{index + 1:04d}.mp3")
        sf.write(path, np.zeros(int(seconds * rate), "float32") + 0.01, rate, format="WAV")   # libsndfile sniffs the header

    def test_recap_report(self):
        self._clip(0, 3.0)       # fits its 4 s slot
        self._clip(1, 6.0)       # overflows: 6 s clip in a 4 s slot (> 1.15x)
        # clips 2 and 3 are missing
        report = pj.quality_report(self.dir, "My Film")
        self.assertEqual(report["engine_type"], "recap")
        script, voice = report["script"], report["voice"]
        self.assertEqual((script["lines"], script["needs_review"], script["over_budget"]), (4, 1, 1))
        self.assertEqual(script["issues"], {"untranslated": 1})
        self.assertEqual(script["review_lines"][0]["index"], 1)
        self.assertEqual((voice["clips"], voice["missing_clips"], voice["overflowing_clips"]), (2, 2, 1))
        self.assertEqual(voice["overflow"][0]["index"], 1)
        self.assertEqual(report["verdict"], "attention")      # 2 of 4 lines flagged = 50 %
        self.assertLess(report["score"], 60)

    def test_clean_project_is_ready(self):
        state = read(os.path.join(self.dir, "state.json"))
        state["generated_script"] = [block(i) for i in range(10)]
        write(os.path.join(self.dir, "state.json"), state)
        report = pj.quality_report(self.dir, "My Film")
        self.assertEqual((report["verdict"], report["score"]), ("ready", 100))

    def test_warnings_and_errors_lower_the_score(self):
        state = read(os.path.join(self.dir, "state.json"))
        state["generated_script"] = [block(i) for i in range(10)]
        state["warnings"] = ["a", "b"]
        write(os.path.join(self.dir, "state.json"), state)
        self.assertEqual(pj.quality_report(self.dir)["score"], 90)
        state["errors"] = ["boom"]
        write(os.path.join(self.dir, "state.json"), state)
        report = pj.quality_report(self.dir)
        self.assertEqual(report["verdict"], "attention")
        self.assertEqual(report["score"], 70)

    def test_subtitle_project_report_uses_the_records(self):
        write(os.path.join(self.dir, "state.json"), {"engine_type": "subtitle", "movie_name": "x", "total_records": 3})
        write(os.path.join(self.dir, "records_data.json"), [
            {"no": 1, "original": "Hello there my friend", "burmese": "ဟယ်လို သူငယ်ချင်း"},
            {"no": 2, "original": "Thank you very much", "burmese": "Thank you very much"},
            {"no": 3, "original": "快跑", "burmese": ""},
        ])
        report = pj.quality_report(self.dir, "sub")
        self.assertEqual(report["engine_type"], "subtitle")
        self.assertEqual((report["subtitles"]["lines"], report["subtitles"]["needs_review"]), (3, 2))
        self.assertEqual([r["index"] for r in report["subtitles"]["review_lines"]], [1, 2])
        self.assertEqual(report["verdict"], "attention")

    def test_write_report_file_and_listing_use_it(self):
        report = pj.write_quality_report(self.dir, "My Film")
        self.assertEqual(read(os.path.join(self.dir, "quality_report.json"))["score"], report["score"])
        rows = pj.list_projects(self.base)
        self.assertEqual([r["name"] for r in rows], ["My Film"])
        row = rows[0]
        self.assertEqual((row["lines"], row["needs_review"], row["engine_type"]), (4, 1, "recap"))
        self.assertEqual(row["score"], report["score"])
        self.assertEqual(row["verdict"], report["verdict"])

    def test_missing_state(self):
        os.remove(os.path.join(self.dir, "state.json"))
        with self.assertRaises(pj.ProjectError):
            pj.quality_report(self.dir)
        self.assertIsNone(pj.write_quality_report(self.dir))


class ListingAndStorageTests(ProjectCase):
    def test_listing_ignores_folders_without_state_and_sorts_newest_first(self):
        os.makedirs(os.path.join(self.base, "not_a_project"))
        newer = os.path.join(self.base, "Newer")
        os.makedirs(newer)
        write(os.path.join(newer, "state.json"), {"movie_name": "Newer", "engine_type": "hardsub", "total_records": 7})
        os.utime(os.path.join(self.dir, "state.json"), (1_000_000, 1_000_000))
        rows = pj.list_projects(self.base)
        self.assertEqual([r["name"] for r in rows], ["Newer", "My Film"])
        self.assertEqual(rows[0]["lines"], 7)
        self.assertEqual(pj.list_projects(os.path.join(self.base, "missing")), [])

    def test_storage_summary(self):
        with open(os.path.join(self.dir, "big.bin"), "wb") as fh:
            fh.write(b"0" * 2_000_000)
        summary = pj.storage_summary({"outputs": self.base, "temp": os.path.join(self.base, "nope")})
        self.assertGreaterEqual(summary["outputs"], 2.0)
        self.assertEqual(summary["temp"], 0)
        self.assertEqual(summary["total_mb"], summary["outputs"])


if __name__ == "__main__":
    unittest.main()
