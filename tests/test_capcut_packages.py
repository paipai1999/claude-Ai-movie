"""
services.capcut_export for the studios other than Recap: Subtitle, Hardsub and Shorts projects (no FFmpeg needed),
and the dashboard endpoint that builds / downloads the package for any of them.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import capcut_export as ce  # noqa: E402

BOM = b"\xef\xbb\xbf"


def write(path, content, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        fh.write(content)


def read_zip(folder):
    with zipfile.ZipFile(os.path.join(folder, "capcut_package.zip")) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pai_pkg_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.dir = os.path.join(self.base, "My Project")
        os.makedirs(self.dir)

    def state(self, **fields):
        write(os.path.join(self.dir, "state.json"), json.dumps(fields, ensure_ascii=False))


class SubtitlePackageTests(ProjectCase):
    HARDSUB_RECORDS = [
        {"id": 1, "start": 0.0, "end": 2.0, "start_ts": "00:00:00,000", "end_ts": "00:00:02,000", "original": "Hello", "burmese": "မင်္ဂလာပါ"},
        {"id": 2, "start": 3.0, "end": 5.5, "start_ts": "00:00:03,000", "end_ts": "00:00:05,500", "original": "Bye", "burmese": "သွားပါဦးမယ်"},
    ]
    SUBTITLE_RECORDS = [
        {"no": 1, "start_s": 1.0, "end_s": 3.0, "start": "00:00:01,000", "end": "00:00:03,000", "original": "Hello", "burmese": "မင်္ဂလာပါ"},
        {"no": 2, "start_s": 65.5, "end_s": 68.0, "start": "00:01:05,500", "end": "00:01:08,000", "original": "Bye", "burmese": "နှုတ်ဆက်ပါတယ်"},
    ]

    def test_a_hardsub_project(self):
        self.state(engine_type="hardsub", movie_name="Film", pipeline_status="COMPLETED")
        write(os.path.join(self.dir, "records_data.json"), json.dumps(self.HARDSUB_RECORDS, ensure_ascii=False))
        write(os.path.join(self.dir, "05_subtitle_burmese.ass"), "[Script Info]\n")
        result = ce.export_capcut_package(self.dir, "My Project")
        self.assertEqual((result["kind"], result["narration"], result["lines"]), ("hardsub", False, 2))
        files = read_zip(self.dir)
        self.assertEqual(sorted(files), ["README.txt", "subtitles.ass", "subtitles.srt", "subtitles_original.srt", "transcript.txt"])
        self.assertTrue(files["subtitles.srt"].startswith(BOM))
        srt = files["subtitles.srt"].decode("utf-8-sig")
        self.assertIn("00:00:03,000 --> 00:00:05,500", srt)
        self.assertIn("မင်္ဂလာပါ", srt)
        original = files["subtitles_original.srt"].decode("utf-8-sig")
        self.assertIn("Hello", original)
        self.assertTrue(files["subtitles_original.srt"].startswith(BOM))
        self.assertIn("[00:00:03] သွားပါဦးမယ်", files["transcript.txt"].decode("utf-8"))
        readme = files["README.txt"].decode("utf-8")
        self.assertIn("subtitles_original.srt", readme)
        self.assertIn("subtitles.ass", readme)
        self.assertIn("Pai AI Movie Studio", readme)

    def test_a_subtitle_project_uses_its_own_time_fields(self):
        self.state(engine_type="subtitle", movie_name="Film")
        write(os.path.join(self.dir, "records_data.json"), json.dumps(self.SUBTITLE_RECORDS, ensure_ascii=False))
        result = ce.export_capcut_package(self.dir)
        self.assertEqual(result["kind"], "subtitle")
        files = read_zip(self.dir)
        self.assertNotIn("subtitles.ass", files)
        self.assertNotIn("subtitles.ass", files["README.txt"].decode("utf-8"))
        self.assertIn("00:01:05,500 --> 00:01:08,000", files["subtitles.srt"].decode("utf-8-sig"))
        self.assertIn("[00:01:05] နှုတ်ဆက်ပါတယ်", files["transcript.txt"].decode("utf-8"))

    def test_an_older_project_falls_back_to_its_srt_and_gets_a_bom(self):
        self.state(engine_type="subtitle", movie_name="Film", total_records=1)
        write(os.path.join(self.dir, "05_subtitle_burmese.srt"), "1\n00:00:01,000 --> 00:00:02,000\nမင်္ဂလာပါ\n\n")
        files = {}
        ce.export_capcut_package(self.dir)
        files = read_zip(self.dir)
        self.assertEqual(sorted(files), ["README.txt", "subtitles.srt"])
        self.assertTrue(files["subtitles.srt"].startswith(BOM))
        self.assertFalse(files["subtitles.srt"].startswith(BOM + BOM))
        # A file that already has a BOM keeps exactly one.
        write(os.path.join(self.dir, "05_subtitle_burmese.srt"), BOM + "1\n00:00:01,000 --> 00:00:02,000\nx\n".encode("utf-8"), mode="wb")
        ce.export_capcut_package(self.dir)
        self.assertFalse(read_zip(self.dir)["subtitles.srt"].startswith(BOM + BOM))

    def test_untranslated_or_empty_projects_are_refused(self):
        self.state(engine_type="hardsub", movie_name="Film")
        with self.assertRaises(ce.CapcutError) as caught:
            ce.export_capcut_package(self.dir)
        self.assertEqual(caught.exception.code, "no_script")
        write(os.path.join(self.dir, "records_data.json"), json.dumps([{"id": 1, "start": 0, "end": 1, "original": "x", "burmese": ""}]))
        with self.assertRaises(ce.CapcutError):
            ce.export_capcut_package(self.dir)

    def test_no_temp_file_is_left_behind(self):
        self.state(engine_type="hardsub", movie_name="Film")
        write(os.path.join(self.dir, "records_data.json"), json.dumps(self.HARDSUB_RECORDS, ensure_ascii=False))
        ce.export_capcut_package(self.dir)
        ce.export_capcut_package(self.dir)
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n not in ("state.json", "records_data.json")), ["capcut_package.zip"])


class ShortsPackageTests(ProjectCase):
    def make_project(self, clean=True, finished=True):
        clips = []
        for number in (1, 2):
            clip = {"index": number, "start": 10.0 * number, "end": 10.0 * number + 30, "score": 80, "title": f"T{number}",
                    "caption": "cap", "hashtags": ["#a"], "reason": "r", "files": {}, "clean_file": None,
                    "cover": f"short_{number:02d}.jpg", "subtitles": f"short_{number:02d}.srt"}
            if finished:
                clip["files"] = {"9:16": f"short_{number:02d}.mp4"}
                write(os.path.join(self.dir, f"short_{number:02d}.mp4"), b"finished" * 10, mode="wb")
            if clean:
                clip["clean_file"] = f"capcut_clips/clip_{number:02d}.mp4"
                write(os.path.join(self.dir, "capcut_clips", f"clip_{number:02d}.mp4"), b"clean" * 10, mode="wb")
            write(os.path.join(self.dir, f"short_{number:02d}.jpg"), b"jpg", mode="wb")
            write(os.path.join(self.dir, f"short_{number:02d}.srt"), "﻿1\n00:00:00,000 --> 00:00:02,000\nမင်္ဂလာ\n\n")
            clips.append(clip)
        write(os.path.join(self.dir, "shorts.json"), json.dumps({"source": "Show", "clips": clips}, ensure_ascii=False))
        write(os.path.join(self.dir, "captions.txt"), "Show\n====\n")
        self.state(engine_type="shorts", movie_name="Show")

    def test_clean_clips_are_packaged_with_their_captions(self):
        self.make_project(clean=True, finished=True)
        result = ce.export_capcut_package(self.dir, "My Project")
        self.assertEqual((result["kind"], result["clips"], result["clean_clips"]), ("shorts", 2, 2))
        files = read_zip(self.dir)
        self.assertEqual(sorted(files), ["README.txt", "captions.txt", "clip_01.jpg", "clip_01.mp4", "clip_01.srt",
                                         "clip_02.jpg", "clip_02.mp4", "clip_02.srt"])
        self.assertEqual(files["clip_01.mp4"], b"clean" * 10)                      # the clean cut, not the finished Short
        self.assertTrue(files["clip_01.srt"].startswith(BOM))
        self.assertFalse(files["clip_01.srt"].startswith(BOM + BOM))
        readme = files["README.txt"].decode("utf-8")
        self.assertIn("clean cuts", readme)
        self.assertNotIn("burnt in", readme)

    def test_a_project_without_clean_clips_falls_back_to_the_finished_ones_and_says_so(self):
        self.make_project(clean=False, finished=True)
        result = ce.export_capcut_package(self.dir)
        self.assertEqual((result["clips"], result["clean_clips"]), (2, 0))
        files = read_zip(self.dir)
        self.assertEqual(files["clip_02.mp4"], b"finished" * 10)
        readme = files["README.txt"].decode("utf-8")
        self.assertIn("burnt in", readme)
        self.assertIn("CapCut pipeline", readme)

    def test_a_clip_missing_on_disk_is_skipped(self):
        self.make_project(clean=True, finished=False)
        os.remove(os.path.join(self.dir, "capcut_clips", "clip_02.mp4"))
        result = ce.export_capcut_package(self.dir)
        self.assertEqual(result["clips"], 1)
        self.assertNotIn("clip_02.mp4", read_zip(self.dir))

    def test_nothing_on_disk_or_no_manifest_is_refused(self):
        self.make_project(clean=True, finished=False)
        shutil.rmtree(os.path.join(self.dir, "capcut_clips"))
        with self.assertRaises(ce.CapcutError):
            ce.export_capcut_package(self.dir)
        os.remove(os.path.join(self.dir, "shorts.json"))
        with self.assertRaises(ce.CapcutError) as caught:
            ce.export_capcut_package(self.dir)
        self.assertEqual(caught.exception.code, "no_script")


class DispatchTests(ProjectCase):
    def test_unsupported_and_unreadable_projects(self):
        self.state(engine_type="splitter", movie_name="x")
        with self.assertRaises(ce.CapcutError) as caught:
            ce.export_capcut_package(self.dir)
        self.assertEqual(caught.exception.code, "unsupported")
        os.remove(os.path.join(self.dir, "state.json"))
        with self.assertRaises(ce.CapcutError) as caught:
            ce.export_capcut_package(self.dir)
        self.assertEqual(caught.exception.code, "no_state")

    def test_the_dashboard_is_told_which_engines_can_be_packaged(self):
        self.assertEqual(sorted(ce.supported_engines()), ["hardsub", "recap", "shorts", "subtitle"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_pkg_api_")
        self.old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(lambda: (os.chdir(self.old), shutil.rmtree(self.work, ignore_errors=True)))
        folder = os.path.join("outputs", "Film")
        write(os.path.join(folder, "state.json"), json.dumps({"engine_type": "hardsub", "movie_name": "Film"}))
        write(os.path.join(folder, "records_data.json"), json.dumps(SubtitlePackageTests.HARDSUB_RECORDS, ensure_ascii=False))
        import web_ui
        from fastapi.testclient import TestClient
        self.client = TestClient(web_ui.app)

    def test_a_hardsub_project_is_packaged_and_downloaded(self):
        built = self.client.post("/api/projects/Film/capcut")
        self.assertEqual(built.status_code, 200)
        self.assertEqual(built.json()["kind"], "hardsub")
        got = self.client.get(built.json()["download"])
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.headers["content-type"], "application/zip")

    def test_an_unsupported_project_is_a_409_with_a_reason(self):
        write(os.path.join("outputs", "Split", "state.json"), json.dumps({"engine_type": "splitter", "movie_name": "Split"}))
        res = self.client.post("/api/projects/Split/capcut")
        self.assertEqual(res.status_code, 409)
        self.assertIn("splitter", res.json()["detail"])


class BundleTests(unittest.TestCase):
    """GET /api/outputs/zip: a tool project's bundle is its result, not its multi-gigabyte source and working files."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_bundle_")
        self.old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(lambda: (os.chdir(self.old), shutil.rmtree(self.work, ignore_errors=True)))
        import web_ui
        from fastapi.testclient import TestClient
        self.client = TestClient(web_ui.app)

    def make(self, name, engine):
        folder = os.path.join("outputs", name)
        write(os.path.join(folder, "state.json"), json.dumps({"engine_type": engine, "movie_name": name}))
        for relative in ("01_video_original.mp4", "short_01.mp4", "capcut_clips/clip_01.mp4", "_work/a.ass", "_download/b.mp4"):
            write(os.path.join(folder, relative), b"x" * 64, mode="wb")
        for relative in ("source_transcript.json", "clips_plan.json", "captions.txt"):
            write(os.path.join(folder, relative), "{}")

    def names(self, movie):
        res = self.client.get("/api/outputs/zip", params={"movie": movie})
        self.assertEqual(res.status_code, 200)
        with open("bundle.zip", "wb") as fh:
            fh.write(res.content)
        return sorted(zipfile.ZipFile("bundle.zip").namelist())

    def test_a_shorts_bundle_leaves_out_the_source_and_the_working_files(self):
        self.make("Show", "shorts")
        self.assertEqual(self.names("Show"), ["capcut_clips/clip_01.mp4", "captions.txt", "short_01.mp4"])

    def test_a_splitter_bundle_too(self):
        self.make("Parts", "splitter")
        self.assertNotIn("01_video_original.mp4", self.names("Parts"))

    def test_other_studios_keep_their_behaviour(self):
        self.make("Film", "hardsub")
        names = self.names("Film")
        self.assertIn("01_video_original.mp4", names)
        self.assertIn("short_01.mp4", names)
        self.assertNotIn("state.json", names)


if __name__ == "__main__":
    unittest.main()
