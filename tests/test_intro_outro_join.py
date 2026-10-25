"""
The thumbnail intro and the outro card, with the real FFmpeg.

A film made from a source with a slightly non-square pixel (for example SAR 11:10) never matched the card (SAR 1:1):
FFmpeg's concat filter refused to join them, on every run, and the intro silently never appeared (only a warning).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.merger import intro_outro  # noqa: E402
from agents.merger.single_pass import SinglePassMixin  # noqa: E402
from agents.video_merger_agent import VideoMergerAgent, _get_ffmpeg_bin  # noqa: E402

CPU = {"codec": "libx264", "preset": "ultrafast", "type": "cpu"}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def make_film(path, seconds=3, audio=True, sar="11/10"):
    cmd = [_get_ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={seconds}"]
    cmd += ["-vf", f"setsar={sar},format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    cmd += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    return run(cmd + [path]).returncode == 0 and os.path.exists(path)


def describe(path):
    return run([_get_ffmpeg_bin(), "-i", path, "-hide_banner"]).stderr


def seconds_of(path):
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", describe(path)).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


class JoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="pai_join_")
        cls.thumb = os.path.join(cls.base, "thumb.png")
        from PIL import Image
        Image.new("RGB", (320, 240), (200, 40, 40)).save(cls.thumb)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_join_work_")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        patcher = mock.patch.object(intro_outro, "detect_hardware_encoder", return_value=CPU)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.film = os.path.join(self.work, "film.mp4")

    def test_the_film_of_this_bug_really_has_a_non_square_pixel(self):
        self.assertTrue(make_film(self.film))
        self.assertIn("SAR 11:10", describe(self.film))

    def test_the_intro_is_prepended_to_a_film_with_a_non_square_pixel(self):
        make_film(self.film)
        before = seconds_of(self.film)
        self.assertTrue(VideoMergerAgent._prepend_thumbnail_intro_ffmpeg(self.thumb, self.film, thumb_duration=2.0))
        self.assertAlmostEqual(seconds_of(self.film), before + 2.0, delta=0.3)
        self.assertIn("Audio:", describe(self.film))
        self.assertIn("SAR 1:1", describe(self.film))
        self.assertEqual(sorted(os.listdir(self.work)), ["film.mp4"], "no temporary file is left behind")

    def test_the_outro_card_is_appended_to_the_same_kind_of_film(self):
        make_film(self.film)
        before = seconds_of(self.film)
        self.assertTrue(VideoMergerAgent._append_outro_card_ffmpeg(self.film, outro_duration=2.0))
        self.assertAlmostEqual(seconds_of(self.film), before + 2.0, delta=0.3)
        self.assertIn("SAR 1:1", describe(self.film))

    def test_a_film_with_square_pixels_still_works(self):
        make_film(self.film, sar="1")
        self.assertTrue(VideoMergerAgent._prepend_thumbnail_intro_ffmpeg(self.thumb, self.film, thumb_duration=1.0))

    def test_a_film_without_sound_gets_its_intro_too(self):
        make_film(self.film, audio=False)
        before = seconds_of(self.film)
        self.assertTrue(VideoMergerAgent._prepend_thumbnail_intro_ffmpeg(self.thumb, self.film, thumb_duration=1.0))
        self.assertAlmostEqual(seconds_of(self.film), before + 1.0, delta=0.3)
        self.assertNotIn("Audio:", describe(self.film))

    def test_when_the_hardware_encoder_fails_the_cpu_finishes_the_job(self):
        make_film(self.film)
        broken = {"codec": "h264_not_an_encoder", "preset": "fast", "type": "gpu"}
        with mock.patch.object(intro_outro, "detect_hardware_encoder", return_value=broken):
            self.assertTrue(VideoMergerAgent._prepend_thumbnail_intro_ffmpeg(self.thumb, self.film, thumb_duration=1.0))

    def test_an_unreadable_film_is_reported_with_ffmpegs_words_and_nothing_is_left(self):
        broken = os.path.join(self.work, "broken.mp4")
        open(broken, "wb").write(b"not a video")
        ok, message = intro_outro.join_clips(_get_ffmpeg_bin(), broken, broken, os.path.join(self.work, "out.mp4"), 60)
        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertFalse(os.path.exists(os.path.join(self.work, "out.mp4")))

    def test_the_join_does_not_depend_on_the_order_of_the_clips(self):
        card = os.path.join(self.work, "card.mp4")
        make_film(self.film)
        make_film(card, seconds=1, sar="1")
        out = os.path.join(self.work, "out.mp4")
        for first, second in ((self.film, card), (card, self.film)):
            ok, message = intro_outro.join_clips(_get_ffmpeg_bin(), first, second, out, 120)
            self.assertTrue(ok, message)
            self.assertAlmostEqual(seconds_of(out), seconds_of(first) + seconds_of(second), delta=0.35)
            os.remove(out)


class SquarePixelTests(unittest.TestCase):
    def test_the_single_pass_film_always_leaves_with_square_pixels(self):
        from types import SimpleNamespace
        from agents.merger.plan import MergePlan
        agent = VideoMergerAgent()
        plan = MergePlan(effective_video_dur=60.0, video_fps=24.0)
        parts, _label = agent._video_filter_parts(plan, SimpleNamespace(mirror=False), include_blur=False, wm_input_idx=None, has_ass=False)
        self.assertTrue(parts[0].endswith(",setsar=1[v_base]"), parts[0])
        self.assertTrue(hasattr(SinglePassMixin, "_video_filter_parts"))

    def test_the_reels_canvas_is_set_to_square_pixels_in_both_filters(self):
        import inspect
        from agents.merger import reels
        source = inspect.getsource(reels)
        self.assertEqual(source.count("overlay=(W-w)/2:(H-h)/2,setsar=1"), 2)


class IntroShiftsEverythingTests(unittest.TestCase):
    """With the intro really there (it never was), the finished video starts 3 s later than the film the script was timed for."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_intro_shift_")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        from types import SimpleNamespace
        from agents.merger.plan import MergePlan
        self.agent = VideoMergerAgent(output_dir=self.work)
        self.state = SimpleNamespace()
        self.plan = MergePlan(output_dir=self.work, final_output=os.path.join(self.work, "final_recap.mp4"),
                              persistent_clean_path=os.path.join(self.work, "final_recap_clean.mp4"),
                              clean_video_path=os.path.join(self.work, "temp_clean.mp4"), has_thumb_intro=True, thumb_duration=3.0,
                              thumbnail_path=os.path.join(self.work, "thumbnail.jpg"),
                              subtitle_timings=[(1.5, 2.0, "စာတန်း တစ်"), (10.0, 3.5, "စာတန်း နှစ်")])

    def apply(self, intro_ok):
        with mock.patch.object(VideoMergerAgent, "_prepend_thumbnail_intro_ffmpeg", return_value=intro_ok):
            self.agent._apply_intro_outro(self.plan, self.state)

    def test_an_applied_intro_is_recorded_and_a_matching_srt_is_written(self):
        self.apply(True)
        self.assertTrue(self.state.thumbnail_intro_applied)
        self.assertEqual(self.state.thumbnail_intro_seconds, 3.0)
        srt = open(os.path.join(self.work, "myanmar_subs_final_video.srt"), encoding="utf-8").read()
        self.assertIn("00:00:04,500 --> 00:00:06,500", srt)            # 1.5 s + the 3 s intro
        self.assertIn("00:00:13,000 --> 00:00:16,500", srt)
        self.assertIn("စာတန်း တစ်", srt)

    def test_the_srt_timed_for_the_film_without_the_intro_is_left_alone(self):
        self.agent._export_standalone_srt(self.plan.subtitle_timings, self.work)
        before = open(os.path.join(self.work, "myanmar_subs.srt"), encoding="utf-8").read()
        self.apply(True)
        self.assertEqual(open(os.path.join(self.work, "myanmar_subs.srt"), encoding="utf-8").read(), before)
        self.assertIn("00:00:01,500 --> 00:00:03,500", before)
        self.assertFalse(os.path.exists(os.path.join(self.work, "myanmar_subs_final_video.ass")), "only the SRT is shifted")

    def test_a_failed_intro_changes_nothing(self):
        self.apply(False)
        self.assertFalse(self.state.thumbnail_intro_applied)
        self.assertEqual(self.state.thumbnail_intro_seconds, 0.0)
        self.assertFalse(os.path.exists(os.path.join(self.work, "myanmar_subs_final_video.srt")))

    def test_no_intro_planned_means_no_offset(self):
        self.plan.has_thumb_intro = False
        self.apply(True)
        self.assertFalse(self.state.thumbnail_intro_applied)
        self.assertEqual(self.state.thumbnail_intro_seconds, 0.0)

    def test_the_default_srt_export_is_unchanged(self):
        self.agent._export_standalone_srt([(0.0, 2.0, "စာတန်း")], self.work)
        self.assertTrue(os.path.exists(os.path.join(self.work, "myanmar_subs.srt")))
        self.assertTrue(os.path.exists(os.path.join(self.work, "myanmar_subs.ass")))

    def test_qa_is_told_the_times_of_the_video_it_watches_intro_included(self):
        from agents.qa_agent import QAAgent
        from brain.memory import MovieState
        from core import smart_sync

        def schedule(state):
            seen = {}

            def ask(file_name, system, prompt, key, **kwargs):
                seen["prompt"] = prompt
                return "{}"

            with mock.patch("agents.qa_agent.ask_gemini_with_video", side_effect=ask):
                QAAgent(output_dir=self.work)._run_sync_check_with_file("files/x", "k", state, "m")
            return seen["prompt"]

        state = MovieState(movie_name="Film")
        state.generated_script = [{"scene_id": "1", "narration": "one", "start_sec": 10.0, "end_sec": 14.0}]
        self.assertIn("[10.0s-14.0s] one", schedule(state))                       # no intro: exactly as before
        state.thumbnail_intro_applied, state.thumbnail_intro_seconds = True, 3.0
        self.assertIn("[13.0s-17.0s] one", schedule(state))
        state.thumbnail_intro_applied = False
        self.assertIn("[10.0s-14.0s] one", schedule(state))                       # an intro that failed shifts nothing
        state.thumbnail_intro_applied = True
        plan = smart_sync.plan([(10.0, 14.0, 6.0)], 60.0)                          # Smart Sync AND an intro
        state.sync_plan = plan.to_dict()
        self.assertIn(f"[{plan.to_output(10.0) + 3.0:.1f}s-{plan.to_output(14.0) + 3.0:.1f}s] one", schedule(state))


if __name__ == "__main__":
    unittest.main()
