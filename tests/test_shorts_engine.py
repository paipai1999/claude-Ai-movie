"""
shorts_engine - the Long to Short tool. Real FFmpeg on a generated 130 s video; the model and the translator are
scripted fakes (so no network), and the transcript is injected (so no Whisper).
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

import shorts_engine  # noqa: E402
from agents.video_merger_agent import _get_ffmpeg_bin  # noqa: E402

DURATION = 130.0
CPU = {"codec": "libx264", "preset": "ultrafast", "quality_args": ["-crf", "32"]}


def make_video(path):
    cmd = [_get_ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=15:duration={DURATION}",
           "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=22050:duration={DURATION}",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "48k", "-shortest", path]
    return subprocess.run(cmd, capture_output=True).returncode == 0 and os.path.exists(path)


def transcript():
    return [{"id": i + 1, "start": i * 5.0, "end": i * 5.0 + 4.0, "start_ts": "", "end_ts": "",
             "original": f"Line {i}: this is sentence number {i}.", "speaker_gender": "neutral", "burmese": ""}
            for i in range(26)]


MODEL_PICKS = json.dumps([
    {"start": "00:10", "end": "00:40", "score": 90, "title": "Opening hook", "hook": "Watch this", "reason": "Strong start",
     "caption": "A great moment.", "hashtags": ["movie", "#clip"]},
    {"start": "01:05", "end": "01:35", "score": 80, "title": "Middle twist", "hook": "", "reason": "Twist",
     "caption": "Twist!", "hashtags": []},
    {"start": "01:40", "end": "02:05", "score": 70, "title": "Ending", "hook": "", "reason": "Payoff", "caption": "", "hashtags": []},
])


def fake_translate(self, segments, source_language="auto", translation_style="persona", context_hint=None):
    for segment in segments:
        segment["burmese"] = "မြန်မာ စာတန်း " + str(segment["id"])
    return segments


def frame_difference(video_a, video_b, at=6.0, rows=(0.0, 1.0)):
    """Mean absolute pixel difference of one frame of two videos (0 = identical), over a band of rows (0-1)."""
    import numpy as np
    from PIL import Image
    tmp = tempfile.mkdtemp(prefix="pai_frame_")
    try:
        images = []
        for index, video in enumerate((video_a, video_b)):
            png = os.path.join(tmp, f"{index}.png")
            subprocess.run([_get_ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", str(at), "-i", video, "-frames:v", "1", png],
                           capture_output=True)
            frame = np.asarray(Image.open(png).convert("L"), dtype="float32")
            images.append(frame[int(rows[0] * frame.shape[0]):int(rows[1] * frame.shape[0])])
        return float(np.abs(images[0] - images[1]).mean())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ShortsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="pai_shorts_")
        cls.video = os.path.join(cls.base, "long show.mp4")
        if not make_video(cls.video):
            raise unittest.SkipTest("FFmpeg could not generate the test video")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="pai_shorts_out_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.engine = shorts_engine.ShortsEngine(output_base_dir=self.out)
        self.llm_calls = []
        self.answer = MODEL_PICKS
        self.keys = ["fake-key"]

        def fake_llm(**kwargs):
            self.llm_calls.append(kwargs)
            return self.answer, "fake-model"

        self.transcribe_calls = []

        def fake_extract(this, video_file, sub_file, source_language="auto"):
            self.transcribe_calls.append(video_file)
            return transcript(), "faster_whisper"

        for patcher in (
            mock.patch.object(shorts_engine.ShortsEngine, "_call_llm", side_effect=lambda **kw: fake_llm(**kw)),
            mock.patch.object(shorts_engine.ShortsEngine, "_get_api_keys", side_effect=lambda: list(self.keys)),
            mock.patch.object(shorts_engine.ShortsEngine, "_extract_transcript", autospec=True, side_effect=fake_extract),
            mock.patch.object(shorts_engine.ShortsEngine, "_translate_dialogue", fake_translate),
            mock.patch.object(shorts_engine, "detect_hardware_encoder", return_value=CPU),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, project="show", **kwargs):
        defaults = dict(clip_count=3, clip_min_sec=20, clip_max_sec=40, resolution="720p")
        defaults.update(kwargs)
        return self.engine.run(self.video, project_name=project, **defaults)

    def folder(self, project="show"):
        return os.path.join(self.out, project)

    def size_of(self, project, name):
        meta = self.engine._probe_video_metadata(os.path.join(self.folder(project), name))
        return meta["width"], meta["height"], meta["duration"]

    # ── the whole run ──────────────────────────────────────────────────────
    def test_three_shorts_with_everything(self):
        result = self.run_tool()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["phase"], "Made 3 Short(s)")
        folder = self.folder()
        for number in (1, 2, 3):
            width, height, length = self.size_of("show", f"short_{number:02d}.mp4")
            self.assertEqual((width, height), (720, 1280))
            self.assertAlmostEqual(length, result["clips"][number - 1]["duration"], delta=0.4)
            self.assertTrue(os.path.getsize(os.path.join(folder, f"short_{number:02d}.jpg")) > 0)
        self.assertFalse(os.path.exists(os.path.join(folder, "_work")), "the working folder must be removed")

        manifest = json.load(open(os.path.join(folder, "shorts.json"), encoding="utf-8"))
        self.assertEqual(manifest["source"], "long show")
        clips = manifest["clips"]
        self.assertEqual([c["title"] for c in clips], ["Opening hook", "Middle twist", "Ending"])
        self.assertEqual([c["start"] for c in clips], [10.0, 65.0, 100.0])
        self.assertEqual(clips[0]["hashtags"], ["#movie", "#clip"])
        self.assertEqual(clips[0]["files"], {"9:16": "short_01.mp4"})
        self.assertNotIn("lines", clips[0])
        captions = open(os.path.join(folder, "captions.txt"), encoding="utf-8").read()
        self.assertIn("Title:    Opening hook", captions)
        self.assertIn("Hashtags: #movie #clip", captions)

        srt = open(os.path.join(folder, "short_01.srt"), "rb").read()
        self.assertTrue(srt.startswith(b"\xef\xbb\xbf"), "the .srt needs a UTF-8 BOM for CapCut")
        self.assertIn("မြန်မာ စာတန်း", srt.decode("utf-8-sig"))
        self.assertTrue(srt.decode("utf-8-sig").startswith("1\n00:00:00,000 -->"))

        state = json.load(open(os.path.join(folder, "state.json"), encoding="utf-8"))
        self.assertEqual((state["engine_type"], state["pipeline_status"], state["total_records"]), ("shorts", "COMPLETED", 3))

    def test_only_the_clip_lines_are_translated(self):
        seen = []

        def spy(this, segments, *args, **kwargs):
            seen.append(len(segments))
            return fake_translate(this, segments)

        with mock.patch.object(shorts_engine.ShortsEngine, "_translate_dialogue", spy):
            self.run_tool(clip_count=1, project="spy")
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], 26)        # 6 lines for a 30 s clip, not the whole 26-line film

    def test_subtitles_and_the_hook_are_really_burnt_in(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        self.run_tool(project="with", clip_count=1, short_subtitles="burmese", short_hook_title=True)
        self.run_tool(project="without", clip_count=1, short_subtitles="none", short_hook_title=False)
        with_subs = os.path.join(self.folder("with"), "short_01.mp4")
        without = os.path.join(self.folder("without"), "short_01.mp4")
        self.assertGreater(frame_difference(with_subs, without, at=1.5, rows=(0.05, 0.25)), 5.0)   # the hook title (shown 0-3.2 s)
        self.assertGreater(frame_difference(with_subs, without, at=6.0, rows=(0.55, 0.85)), 5.0)   # a subtitle line (5-9 s)
        self.assertLess(frame_difference(with_subs, without, at=6.0, rows=(0.30, 0.45)), 2.0)      # nothing else changed
        self.assertFalse(os.path.exists(os.path.join(self.folder("without"), "short_01.srt")))
        manifest = json.load(open(os.path.join(self.folder("without"), "shorts.json"), encoding="utf-8"))
        self.assertIsNone(manifest["clips"][0]["subtitles"])

    def test_both_formats_and_fill_framing(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="both", clip_count=1, video_format="both", short_framing="fill")
        self.assertEqual(result["clips"][0]["files"], {"16:9": "short_01_16x9.mp4", "9:16": "short_01.mp4"})
        self.assertEqual(self.size_of("both", "short_01.mp4")[:2], (720, 1280))
        self.assertEqual(self.size_of("both", "short_01_16x9.mp4")[:2], (1280, 720))

    def test_draft_quality_renders_a_480p_canvas(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        self.run_tool(project="draft", clip_count=1, video_format="both", resolution="480p")
        self.assertEqual(self.size_of("draft", "short_01.mp4")[:2], (480, 854))
        self.assertEqual(self.size_of("draft", "short_01_16x9.mp4")[:2], (854, 480))

    # ── choosing ───────────────────────────────────────────────────────────
    def test_without_a_key_the_offline_scorer_picks(self):
        self.keys = []
        result = self.run_tool(project="offline", clip_count=2)
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(len(result["clips"]), 2)
        self.assertTrue(all(c["source"] == "heuristic" for c in result["clips"]))
        for clip in result["clips"]:
            self.assertGreaterEqual(clip["duration"], 20.0)
            self.assertLessEqual(clip["duration"], 40.0)

    def test_a_garbled_model_answer_falls_back_to_the_offline_scorer(self):
        self.answer = "I think the best part is somewhere in the middle!"
        result = self.run_tool(project="garbled", clip_count=2)
        self.assertGreaterEqual(len(self.llm_calls), 2)         # it was asked (and retried), then gave up
        self.assertTrue(all(c["source"] == "heuristic" for c in result["clips"]))

    def test_too_few_model_picks_are_topped_up_offline(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="topup", clip_count=3)
        sources = [c["source"] for c in result["clips"]]
        self.assertEqual(sources.count("model"), 1)
        self.assertEqual(len(sources), 3)
        spans = sorted((c["start"], c["end"]) for c in result["clips"])
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(start, end + 0.2 * 30)

    def test_the_prompt_carries_the_settings_and_the_note(self):
        self.run_tool(project="prompt", clip_count=1, clip_focus="funny", context_hint="I make cooking videos")
        prompt = self.llm_calls[0]["user_prompt"]
        self.assertIn("between 20 and 40 seconds", prompt)
        self.assertIn("funny moments", prompt)
        self.assertIn("I make cooking videos", prompt)
        self.assertIn("Burmese", prompt)
        self.assertIn("[00:10-00:14] Line 2", prompt)
        self.assertEqual(self.llm_calls[0]["response_mime_type"], "application/json")

    # ── re-runs ────────────────────────────────────────────────────────────
    def test_a_rerun_reuses_the_transcript_and_the_chosen_clips(self):
        self.run_tool(project="again", clip_count=2)
        calls, transcripts = len(self.llm_calls), len(self.transcribe_calls)
        stamp = os.path.getmtime(os.path.join(self.folder("again"), "clips_plan.json"))
        self.run_tool(project="again", clip_count=2, subtitle_style="yellow_pop", video_format="both")   # only the look changed
        self.assertEqual(len(self.llm_calls), calls)
        self.assertEqual(len(self.transcribe_calls), transcripts)
        self.assertEqual(os.path.getmtime(os.path.join(self.folder("again"), "clips_plan.json")), stamp)
        self.assertTrue(os.path.exists(os.path.join(self.folder("again"), "short_01_16x9.mp4")))

    def test_changing_the_selection_settings_chooses_again(self):
        self.run_tool(project="rechoose", clip_count=2)
        calls = len(self.llm_calls)
        self.run_tool(project="rechoose", clip_count=2, clip_focus="action")
        self.assertGreater(len(self.llm_calls), calls)
        self.assertEqual(len(self.transcribe_calls), 1)       # the transcript is still reused

    # ── guards ─────────────────────────────────────────────────────────────
    def test_a_video_shorter_than_the_shortest_clip_is_explained(self):
        with self.assertRaises(ValueError) as caught:
            self.engine.run(self.video, project_name="tiny", clip_min_sec=200, clip_max_sec=300)
        self.assertIn("shorter than the shortest clip", str(caught.exception))

    def test_cancellation_before_any_work(self):
        event = threading.Event()
        event.set()
        engine = shorts_engine.ShortsEngine(output_base_dir=self.out, cancel_event=event)
        with self.assertRaises(InterruptedError):
            engine.run(self.video, project_name="stop")
        self.assertEqual(self.llm_calls, [])

    def test_a_local_source_is_used_in_place_not_copied(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        before = os.path.getsize(self.video)
        self.run_tool(project="inplace", clip_count=1)
        folder = self.folder("inplace")
        self.assertFalse(os.path.exists(os.path.join(folder, "01_video_original.mp4")), "a local source must not be copied")
        self.assertEqual(os.path.getsize(self.video), before)
        with self.assertRaises(FileNotFoundError):
            self.engine.run(os.path.join(self.base, "missing.mp4"), project_name="nofile")

    def test_the_counts_are_kept_in_range(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="range", clip_count=0, clip_min_sec=1, clip_max_sec=2)   # -> 1 clip, 8-13 s
        self.assertEqual(len(result["clips"]), 1)
        self.assertGreaterEqual(result["clips"][0]["duration"], 8.0)

    def test_the_project_name_cannot_escape_the_output_folder(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="../../evil", clip_count=1)
        self.assertTrue(os.path.abspath(result["project_dir"]).startswith(os.path.abspath(self.out) + os.sep))

    # ── delivery modes ─────────────────────────────────────────────────────
    def test_the_capcut_pipeline_makes_clean_clips_and_the_package(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="cc", clip_count=1, delivery_mode="capcut")
        folder = self.folder("cc")
        self.assertEqual(result["package"], "capcut_package.zip")
        self.assertFalse(os.path.exists(os.path.join(folder, "short_01.mp4")), "no finished, burnt-in Short in this mode")
        width, height, length = self.size_of("cc", "capcut_clips/clip_01.mp4")
        self.assertEqual((width, height), (320, 240))                    # the source's own picture format, no canvas
        self.assertAlmostEqual(length, result["clips"][0]["duration"], delta=0.4)
        clip = result["clips"][0]
        self.assertEqual((clip["files"], clip["clean_file"]), ({}, "capcut_clips/clip_01.mp4"))
        with __import__("zipfile").ZipFile(os.path.join(folder, "capcut_package.zip")) as zf:
            names = zf.namelist()
            self.assertEqual(sorted(names), ["README.txt", "captions.txt", "clip_01.jpg", "clip_01.mp4", "clip_01.srt"])
            self.assertTrue(zf.read("clip_01.srt").startswith(b"\xef\xbb\xbf"))
            self.assertIn("clean cuts", zf.read("README.txt").decode("utf-8"))
        self.assertTrue(os.path.getsize(os.path.join(folder, "short_01.jpg")) > 0)            # the cover still exists
        manifest = json.load(open(os.path.join(folder, "shorts.json"), encoding="utf-8"))
        self.assertEqual(manifest["delivery_mode"], "capcut")
        state = json.load(open(os.path.join(folder, "state.json"), encoding="utf-8"))
        self.assertEqual(state["delivery_mode"], "capcut")
        from services import projects
        self.assertTrue([r for r in projects.list_projects(self.out) if r["name"] == "cc"][0]["has_video"])

    def test_the_clean_clip_has_nothing_burnt_in(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        self.run_tool(project="clean", clip_count=1, delivery_mode="capcut")
        self.run_tool(project="plain", clip_count=1, short_subtitles="none", short_hook_title=False, video_format="9:16")
        # Different formats cannot be compared pixel by pixel, so compare the clean clip with a plain cut of the source.
        clean = os.path.join(self.folder("clean"), "capcut_clips", "clip_01.mp4")
        reference = os.path.join(self.base, "reference.mp4")
        subprocess.run([shutil.which("ffmpeg") or _get_ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", "10", "-i", self.video, "-t", "29",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", reference], capture_output=True)
        self.assertLess(frame_difference(clean, reference, at=6.0, rows=(0.0, 1.0)), 6.0)

    def test_both_makes_the_finished_shorts_and_the_clean_clips(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="bothmodes", clip_count=1, delivery_mode="both")
        folder = self.folder("bothmodes")
        self.assertTrue(os.path.exists(os.path.join(folder, "short_01.mp4")))
        self.assertTrue(os.path.exists(os.path.join(folder, "capcut_clips", "clip_01.mp4")))
        self.assertEqual(result["package"], "capcut_package.zip")
        with __import__("zipfile").ZipFile(os.path.join(folder, "capcut_package.zip")) as zf:
            clean_size = os.path.getsize(os.path.join(folder, "capcut_clips", "clip_01.mp4"))
            self.assertEqual(len(zf.read("clip_01.mp4")), clean_size)       # the package carries the clean cut

    def test_oneclick_makes_no_clean_clips_and_no_package(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        result = self.run_tool(project="oneclick", clip_count=1)
        folder = self.folder("oneclick")
        self.assertIsNone(result["package"])
        self.assertFalse(os.path.exists(os.path.join(folder, "capcut_clips")))
        self.assertFalse(os.path.exists(os.path.join(folder, "capcut_package.zip")))

    def test_switching_the_delivery_mode_only_renders_again(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        self.run_tool(project="switch", clip_count=1)
        calls, transcripts = len(self.llm_calls), len(self.transcribe_calls)
        result = self.run_tool(project="switch", clip_count=1, delivery_mode="both")
        self.assertEqual((len(self.llm_calls), len(self.transcribe_calls)), (calls, transcripts))
        self.assertEqual(result["package"], "capcut_package.zip")

    def test_the_tool_result_links_the_clean_clip_and_the_package(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        self.run_tool(project="links", clip_count=1, delivery_mode="both")
        from services import projects
        data = projects.tool_result(self.folder("links"), "links")
        item = data["items"][0]
        self.assertEqual(data["delivery_mode"], "both")
        self.assertEqual(item["clean_url"], "/api/outputs/file?path=links/capcut_clips/clip_01.mp4")
        self.assertEqual(data["package_url"], "/api/projects/links/capcut")
        self.assertEqual([v["format"] for v in item["videos"]], ["9:16"])

    # ── pieces ─────────────────────────────────────────────────────────────
    def test_filter_graphs(self):
        engine = self.engine
        landscape = {"width": 1920, "height": 1080}
        fit = engine._filter_graph("9:16", 1080, 1920, "fit", landscape, "a.ass")
        self.assertIn("boxblur", fit)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", fit)
        self.assertIn("ass=filename='a.ass':shaping=1:fontsdir=", fit)
        self.assertTrue(fit.endswith("[vout]"))
        fill = engine._filter_graph("9:16", 1080, 1920, "fill", landscape, "a.ass")
        self.assertIn("crop='trunc(min(iw,ih*9/16)/2)*2':ih", fill)
        self.assertNotIn("boxblur", fill)
        direct = engine._filter_graph("16:9", 1920, 1080, "fit", landscape, "a.ass")
        self.assertNotIn("boxblur", direct)
        portrait = engine._filter_graph("16:9", 1920, 1080, "fit", {"width": 1080, "height": 1920}, "a.ass")
        self.assertIn("boxblur", portrait)

    def test_filter_paths_are_escaped(self):
        self.assertEqual(shorts_engine._escape_filter_path("C:\\Fonts\\it's"), "C\\:/Fonts/it\\'s")

    def test_the_render_is_a_plain_edit_without_anti_copyright_tricks(self):
        self.answer = json.dumps(json.loads(MODEL_PICKS)[:1])
        commands = []
        real_run = subprocess.run

        def record(cmd, *args, **kwargs):
            commands.append(" ".join(str(part) for part in cmd))
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(shorts_engine.subprocess, "run", side_effect=record):
            self.run_tool(project="plain", clip_count=1, video_format="both")
        renders = [c for c in commands if "-filter_complex" in c]
        self.assertEqual(len(renders), 2)
        for command in renders:
            for forbidden in ("hflip", "atempo", "1.02", "eq=", "vflip", "rotate"):
                self.assertNotIn(forbidden, command)
            self.assertIn("loudnorm", command)          # the only audio processing: loudness + short fades


class CommandLineTests(unittest.TestCase):
    def test_the_cli_is_generated_from_the_option_model(self):
        import argparse
        from core.cli import SHORTS, add_option_arguments, resolve_args
        parser = argparse.ArgumentParser()
        parser.add_argument("input")
        add_option_arguments(parser, SHORTS)
        values = resolve_args(SHORTS, parser.parse_args(["talk.mp4", "--clips", "8", "--focus", "funny", "--framing", "fill", "--no-hook-title"]))
        self.assertEqual((values["clip_count"], values["clip_focus"], values["short_framing"], values["short_hook_title"]),
                         (8, "funny", "fill", False))
        self.assertEqual((values["video_format"], values["translation_style"], values["short_subtitles"]), ("9:16", "dialogue", "burmese"))
        help_text = parser.format_help()
        for flag in ("--clip-min", "--clip-max", "--short-subtitles", "--sub-style", "--source-lang"):
            self.assertIn(flag, help_text)
        for other in ("--split-mode", "--mirror", "--blur-mode", "--audio-shield", "--tts-rate"):
            self.assertNotIn(other, help_text)


if __name__ == "__main__":
    unittest.main()
