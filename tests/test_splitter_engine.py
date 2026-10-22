"""
splitter_engine - the Splitter tool, with real FFmpeg on a generated test video:
130 s, a keyframe every second, a 440 Hz tone with a 3 s pause at 47-50 s and 97-100 s.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import splitter_engine  # noqa: E402
from agents.video_merger_agent import _get_ffmpeg_bin  # noqa: E402

DURATION = 130.0
CPU = {"codec": "libx264", "preset": "ultrafast", "quality_args": ["-crf", "30"]}


def make_video(path):
    cmd = [_get_ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={DURATION}",
           "-f", "lavfi", "-i", f"aevalsrc='if(lt(mod(t,50),47),0.5*sin(2*PI*440*t),0)':s=22050:d={DURATION}",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-shortest", path]
    return subprocess.run(cmd, capture_output=True).returncode == 0 and os.path.exists(path)


class SplitterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="pai_split_")
        cls.video = os.path.join(cls.base, "long film.mp4")
        if not make_video(cls.video):
            raise unittest.SkipTest("FFmpeg could not generate the test video")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="pai_split_out_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.engine = splitter_engine.SplitterEngine(output_base_dir=self.out)

    def part_files(self, project):
        folder = os.path.join(self.out, project)
        return sorted(n for n in os.listdir(folder) if n.startswith("part_"))

    def test_every_n_minutes_with_stream_copy(self):
        result = self.engine.run(self.video, project_name="film", split_mode="duration", split_minutes=0.5, split_snap=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["phase"], "Split into 5 parts")
        self.assertEqual(self.part_files("film"), [f"part_0{i}.mp4" for i in range(1, 6)])
        parts = result["parts"]
        self.assertEqual([p["file"] for p in parts], self.part_files("film"))
        self.assertAlmostEqual(sum(p["duration"] for p in parts), DURATION, delta=1.5)
        for earlier, later in zip(parts, parts[1:]):                    # contiguous in the manifest
            self.assertAlmostEqual(earlier["end"], later["start"], delta=0.01)
        for part in parts[:4]:
            self.assertAlmostEqual(part["duration"], 30.0, delta=1.5)    # keyframe cuts land within a second
        self.assertGreater(min(p["size_mb"] for p in parts), 0)

    def test_reports_and_project_state(self):
        result = self.engine.run(self.video, project_name="film", split_mode="parts", split_parts=2, split_snap=False)
        folder = result["project_dir"]
        manifest = json.load(open(os.path.join(folder, "parts.json"), encoding="utf-8"))
        self.assertEqual(manifest["source"], "long film")
        self.assertEqual(manifest["settings"]["mode"], "parts")
        self.assertEqual(len(manifest["parts"]), 2)
        state = json.load(open(os.path.join(folder, "state.json"), encoding="utf-8"))
        self.assertEqual((state["engine_type"], state["pipeline_status"], state["total_records"]), ("splitter", "COMPLETED", 2))
        self.assertEqual(state["total_duration_formatted"], "00:02:10")
        from services import projects
        row = [r for r in projects.list_projects(self.out) if r["name"] == "film"][0]
        self.assertEqual(row["engine_type"], "splitter")

    def test_cuts_snap_to_the_pauses(self):
        result = self.engine.run(self.video, project_name="snap", split_mode="parts", split_parts=3, split_snap=True)
        parts = result["parts"]
        self.assertEqual(len(parts), 3)
        # Even thirds would cut at 43.3 s and 86.7 s; the pauses are at 47-50 s and 97-100 s.
        self.assertAlmostEqual(parts[0]["end"], 49.0, delta=1.6)
        self.assertAlmostEqual(parts[1]["end"], 99.0, delta=1.6)

    def test_frame_accurate_reencode(self):
        with mock.patch.object(splitter_engine, "detect_hardware_encoder", return_value=CPU):
            result = self.engine.run(self.video, project_name="exact", split_mode="duration", split_minutes=1,
                                     split_snap=False, split_reencode=True)
        self.assertEqual(len(result["parts"]), 2)
        self.assertAlmostEqual(result["parts"][0]["duration"], 60.0, delta=0.5)
        self.assertAlmostEqual(result["parts"][1]["duration"], 70.0, delta=0.5)

    def test_a_video_shorter_than_one_part_is_explained(self):
        with self.assertRaises(ValueError) as caught:
            self.engine.run(self.video, project_name="short", split_minutes=10)
        self.assertIn("nothing to split", str(caught.exception))

    def test_missing_input_and_bad_settings(self):
        with self.assertRaises(FileNotFoundError):
            self.engine.run(os.path.join(self.base, "nope.mp4"), project_name="x")
        with self.assertRaises(ValueError):
            self.engine.run(self.video, project_name="x", split_mode="parts", split_parts=1)
        with self.assertRaises(ValueError):
            self.engine.run(self.video, project_name="x", split_mode="duration", split_minutes=0.05)

    def test_a_rerun_removes_the_parts_of_the_previous_run(self):
        self.engine.run(self.video, project_name="again", split_mode="duration", split_minutes=0.5, split_snap=False)
        self.assertEqual(len(self.part_files("again")), 5)
        self.engine.run(self.video, project_name="again", split_mode="parts", split_parts=2, split_snap=False)
        self.assertEqual(self.part_files("again"), ["part_01.mp4", "part_02.mp4"])

    def test_the_project_name_cannot_escape_the_output_folder(self):
        result = self.engine.run(self.video, project_name="../../evil", split_mode="parts", split_parts=2, split_snap=False)
        self.assertTrue(os.path.abspath(result["project_dir"]).startswith(os.path.abspath(self.out) + os.sep))

    def test_cancellation_stops_before_any_work(self):
        event = threading.Event()
        event.set()
        engine = splitter_engine.SplitterEngine(output_base_dir=self.out, cancel_event=event)
        with self.assertRaises(InterruptedError):
            engine.run(self.video, project_name="stop", split_parts=2)
        self.assertEqual([n for n in os.listdir(os.path.join(self.out, "stop")) if n.startswith("part_")], [])

    def test_source_is_left_untouched(self):
        before = os.path.getsize(self.video)
        self.engine.run(self.video, project_name="keep", split_mode="parts", split_parts=2, split_snap=False)
        self.assertEqual(os.path.getsize(self.video), before)
        self.assertEqual([n for n in os.listdir(os.path.join(self.out, "keep")) if not n.startswith("part_")],
                         ["parts.json", "state.json"])


class CommandLineTests(unittest.TestCase):
    def test_the_cli_is_generated_from_the_option_model(self):
        import argparse
        from core.cli import SPLITTER, add_option_arguments, resolve_args
        parser = argparse.ArgumentParser()
        parser.add_argument("input")
        add_option_arguments(parser, SPLITTER)
        values = resolve_args(SPLITTER, parser.parse_args(["film.mp4", "--minutes", "5", "--exact-cuts", "--frame-accurate"]))
        self.assertEqual((values["split_minutes"], values["split_snap"], values["split_reencode"], values["split_mode"]),
                         (5.0, False, True, "duration"))
        help_text = parser.format_help()
        for flag in ("--split-mode", "--split-parts", "--name", "--res"):
            self.assertIn(flag, help_text)
        for other in ("--clips", "--tts-rate", "--mirror", "--story-length"):
            self.assertNotIn(other, help_text)


if __name__ == "__main__":
    unittest.main()
