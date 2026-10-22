"""
services.capcut_export - the CapCut-ready package (one narration track, BOM SRT, guide) built from a project.
Uses the real FFmpeg from imageio-ffmpeg on generated tones; skipped when it cannot encode MP3.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import capcut_export as ce  # noqa: E402

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:   # pragma: no cover
    FFMPEG = None

PHASES = ["phase_4_script_seo_thumbnail", "phase_5_voice_generation", "phase_6_video_merge"]


def make_clip(path, seconds, rate=24000):
    import numpy as np
    import soundfile as sf
    t = np.arange(int(seconds * rate)) / rate
    tone = (0.4 * np.sin(2 * np.pi * 330 * t)).astype("float32")
    wav = path + ".src.wav"
    sf.write(wav, tone, rate)
    res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", wav, "-c:a", "libmp3lame", "-q:a", "4", path], capture_output=True)
    os.remove(wav)
    return res.returncode == 0 and os.path.exists(path)


def can_encode():
    if not FFMPEG:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return make_clip(os.path.join(tmp, "probe.mp3"), 0.5)
        except Exception:
            return False


@unittest.skipUnless(can_encode(), "FFmpeg MP3 encoding is unavailable")
class CapcutExportTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pai_capcut_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.dir = os.path.join(self.base, "My Film")
        os.makedirs(os.path.join(self.dir, "voiceover"))
        self.script = [
            {"scene_id": "1", "narration": "ပထမစာကြောင်း ဖြစ်ပါတယ်", "start_sec": 0.0, "end_sec": 2.0},
            {"scene_id": "2", "narration": "ဒုတိယစာကြောင်း ဖြစ်ပါတယ်", "start_sec": 5.0, "end_sec": 7.0},
            {"scene_id": "3", "narration": "တတိယစာကြောင်း ဖြစ်ပါတယ်", "start_sec": 10.0, "end_sec": 12.0},
        ]
        for i, seconds in enumerate((1.5, 1.8, 1.2), 1):
            make_clip(os.path.join(self.dir, "voiceover", f"scene_{i:04d}.mp3"), seconds)
        self.write_state(movie_name="My Film", generated_script=self.script, completed_phases=PHASES, duration="00:00:15")

    def write_state(self, **state):
        with open(os.path.join(self.dir, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)

    def build(self):
        return ce.export_capcut_package(self.dir, "My Film")

    def test_package_contents_and_the_bom(self):
        result = self.build()
        zip_path = os.path.join(self.dir, "capcut_package.zip")
        self.assertTrue(os.path.exists(zip_path))
        self.assertEqual(sorted(result["files"]), ["README.txt", "narration.mp3", "script.txt", "subtitles.srt"])
        self.assertEqual((result["lines"], result["missing_clips"]), (3, 0))
        with zipfile.ZipFile(zip_path) as zf:
            srt_bytes = zf.read("subtitles.srt")
            self.assertTrue(srt_bytes.startswith(b"\xef\xbb\xbf"), "CapCut needs a UTF-8 BOM for Burmese")
            srt = srt_bytes.decode("utf-8-sig")
            self.assertIn("ပထမစာကြောင်း ဖြစ်ပါတယ်", srt)
            self.assertIn("00:00:05,000 --> 00:00:06,800", srt)             # line 2 starts at its scene time, 1.8 s long
            self.assertIn("00:00:10,000 --> 00:00:11,200", srt)
            script = zf.read("script.txt").decode("utf-8")
            self.assertIn("[00:00:05] ဒုတိယစာကြောင်း", script)
            readme = zf.read("README.txt").decode("utf-8")
            self.assertIn("narration.mp3", readme)
            self.assertIn("ပရောဂျက်အသစ်", readme)                              # Burmese steps
            self.assertIn("Import captions", readme)                            # English steps
            self.assertNotIn("subtitles.ass", readme)                           # none was produced
            self.assertGreater(len(zf.read("narration.mp3")), 5000)

    def test_the_narration_track_places_every_line_at_its_time(self):
        self.build()
        import soundfile as sf
        import numpy as np
        with zipfile.ZipFile(os.path.join(self.dir, "capcut_package.zip")) as zf:
            track = os.path.join(self.base, "n.mp3")
            with open(track, "wb") as fh:
                fh.write(zf.read("narration.mp3"))
        data, rate = sf.read(track)
        self.assertAlmostEqual(len(data) / rate, 15.0, delta=0.3)             # the source video length
        loud = np.abs(data) > 0.05
        def active(a, b):
            return loud[int(a * rate):int(b * rate)].mean()
        self.assertGreater(active(0.1, 1.4), 0.9)                              # line 1
        self.assertLess(active(2.2, 4.8), 0.02)                                # silence between lines
        self.assertGreater(active(5.1, 6.6), 0.9)                              # line 2 at 5 s
        self.assertGreater(active(10.1, 11.1), 0.9)                            # line 3 at 10 s

    def test_the_styled_subtitles_are_included_when_the_render_made_them(self):
        with open(os.path.join(self.dir, "myanmar_subs.ass"), "w", encoding="utf-8") as fh:
            fh.write("[Script Info]\nTitle: x\n")
        result = self.build()
        self.assertIn("subtitles.ass", result["files"])
        with zipfile.ZipFile(os.path.join(self.dir, "capcut_package.zip")) as zf:
            self.assertIn("subtitles.ass", zf.read("README.txt").decode("utf-8"))

    def test_edited_lines_that_were_not_re_voiced_block_the_export(self):
        self.write_state(movie_name="My Film", generated_script=self.script, completed_phases=PHASES[:1], duration="00:00:15")
        with self.assertRaises(ce.CapcutError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "not_ready")
        self.assertIn("re-render", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "capcut_package.zip")))

    def test_a_missing_script_is_refused(self):
        self.write_state(movie_name="x", completed_phases=PHASES)
        with self.assertRaises(ce.CapcutError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "no_script")

    def test_without_a_voice_over_the_package_has_the_captions_and_the_script_only(self):
        shutil.rmtree(os.path.join(self.dir, "voiceover"))
        result = self.build()
        self.assertFalse(result["narration"])
        self.assertEqual(sorted(result["files"]), ["README.txt", "script.txt", "subtitles.srt"])
        with zipfile.ZipFile(os.path.join(self.dir, "capcut_package.zip")) as zf:
            self.assertTrue(zf.read("subtitles.srt").startswith(b"\xef\xbb\xbf"))
            self.assertIn("[00:00:05] ဒုတိယစာကြောင်း", zf.read("script.txt").decode("utf-8"))
            self.assertIn("No voice-over", zf.read("README.txt").decode("utf-8"))
        # The same when the voice step never ran (audio mode "original"): there is no folder to be out of date.
        self.write_state(movie_name="My Film", generated_script=self.script, completed_phases=PHASES[:1], duration="00:00:15")
        self.assertFalse(self.build()["narration"])

    def test_the_readme_belongs_to_this_studio(self):
        self.build()
        with zipfile.ZipFile(os.path.join(self.dir, "capcut_package.zip")) as zf:
            readme = zf.read("README.txt").decode("utf-8")
        self.assertIn("Pai AI Movie Studio", readme)
        self.assertNotIn("Narrator", readme)

    def test_a_missing_clip_is_reported_not_fatal(self):
        os.remove(os.path.join(self.dir, "voiceover", "scene_0002.mp3"))
        result = self.build()
        self.assertEqual((result["lines"], result["missing_clips"]), (2, 1))

    def test_rebuilding_replaces_the_package_and_leaves_no_temp_files(self):
        def scratch_dirs():
            return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("pai_capcut_")}

        before = scratch_dirs()
        self.build()
        first = os.path.getsize(os.path.join(self.dir, "capcut_package.zip"))
        self.build()
        self.assertEqual(os.path.getsize(os.path.join(self.dir, "capcut_package.zip")), first)
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n not in ("voiceover", "state.json")), ["capcut_package.zip"])
        self.assertEqual(scratch_dirs() - before, set(), "the export must clean its working folder")

    def test_duration_parsing(self):
        self.assertEqual(ce.source_duration_sec({"duration": "01:02:03"}), 3723.0)
        self.assertEqual(ce.source_duration_sec({"duration": "02:30"}), 150.0)
        self.assertEqual(ce.source_duration_sec({"duration": "45.5"}), 45.5)
        for bad in ({}, {"duration": None}, {"duration": "abc"}, {"duration": "1:2:3:4"}):
            self.assertEqual(ce.source_duration_sec(bad), 0.0)


@unittest.skipUnless(can_encode(), "FFmpeg MP3 encoding is unavailable")
class CapcutApiTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_capcut_api_")
        self.old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(lambda: (os.chdir(self.old), shutil.rmtree(self.work, ignore_errors=True)))
        project = os.path.join("outputs", "My Film")
        os.makedirs(os.path.join(project, "voiceover"))
        for i, seconds in enumerate((1.0, 1.0), 1):
            make_clip(os.path.join(project, "voiceover", f"scene_{i:04d}.mp3"), seconds)
        with open(os.path.join(project, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"movie_name": "My Film", "completed_phases": PHASES, "duration": "00:00:10", "generated_script": [
                {"scene_id": "1", "narration": "က", "start_sec": 0.0, "end_sec": 2.0},
                {"scene_id": "2", "narration": "ခ", "start_sec": 4.0, "end_sec": 6.0}]}, fh)
        import web_ui
        from fastapi.testclient import TestClient
        self.client = TestClient(web_ui.app)

    def test_build_then_download(self):
        self.assertEqual(self.client.get("/api/projects/My Film/capcut").status_code, 404)     # nothing built yet
        built = self.client.post("/api/projects/My Film/capcut")
        self.assertEqual(built.status_code, 200)
        body = built.json()
        self.assertEqual(body["download"], "/api/projects/My%20Film/capcut")
        self.assertIn("narration.mp3", body["files"])
        got = self.client.get(body["download"])
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.headers["content-type"], "application/zip")
        self.assertIn("attachment", got.headers["content-disposition"])
        with open("dl.zip", "wb") as fh:
            fh.write(got.content)
        self.assertIn("subtitles.srt", zipfile.ZipFile("dl.zip").namelist())

    def test_not_ready_and_unknown_projects(self):
        path = os.path.join("outputs", "My Film", "state.json")
        state = json.load(open(path, encoding="utf-8"))
        state["completed_phases"] = []
        json.dump(state, open(path, "w", encoding="utf-8"))
        res = self.client.post("/api/projects/My Film/capcut")
        self.assertEqual(res.status_code, 409)
        self.assertIn("re-render", res.json()["detail"])
        self.assertEqual(self.client.post("/api/projects/Nope/capcut").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/Nope/capcut").status_code, 404)

    def test_login_and_origin_rules_apply(self):
        from unittest.mock import patch
        import web_ui
        from fastapi.testclient import TestClient
        self.assertEqual(self.client.post("/api/projects/My Film/capcut", headers={"Origin": "https://evil.example"}).status_code, 403)
        with patch.dict(os.environ, {"WEB_UI_PASSWORD": "s3cret"}):
            locked = TestClient(web_ui.app)
            self.assertEqual(locked.post("/api/projects/My Film/capcut").status_code, 401)
            self.assertEqual(locked.get("/api/projects/My Film/capcut").status_code, 401)


if __name__ == "__main__":
    unittest.main()
