"""
Smart Sync with the real FFmpeg (agents/merger/smart_sync.py): a generated film whose brightness is 12 x its time in
seconds, so the picture of the re-timed film can be checked against the picture of the source at the moment it should
come from; a 440 Hz tone, so the pitch can be checked after the sound was sped up or slowed down.
"""

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.merger import smart_sync as merger_sync  # noqa: E402
from agents.merger.media_utils import _get_audio_duration, _get_video_info, _has_audio_stream  # noqa: E402
from agents.merger.plan import MergePlan  # noqa: E402
from agents.video_merger_agent import VideoMergerAgent, _get_ffmpeg_bin  # noqa: E402
from core import smart_sync  # noqa: E402

CPU = {"codec": "libx264", "preset": "ultrafast", "quality_args": ["-crf", "26"]}
FPS = 25.0
LENGTH = 20.0


def run(cmd):
    return subprocess.run(cmd, capture_output=True)


def make_film(path):
    cmd = [_get_ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=black:s=160x120:r={int(FPS)}:d={LENGTH},geq=lum='12*T':cb=128:cr=128",
           "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={LENGTH}",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path]
    return run(cmd).returncode == 0 and os.path.exists(path)


def make_clip(path, seconds):
    cmd = [_get_ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
           "-c:a", "libmp3lame", path]
    return run(cmd).returncode == 0 and os.path.exists(path)


def luma(path, at):
    """Mean brightness (0-255) of the frame at ``at`` seconds, measured the same way for any film."""
    import numpy as np
    cmd = [_get_ffmpeg_bin(), "-loglevel", "error", "-ss", f"{at:.3f}", "-i", path, "-frames:v", "1",
           "-vf", "scale=16:16", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    data = run(cmd).stdout
    return float(np.frombuffer(data, dtype="uint8").mean())


def dominant_frequency(path, start=0.5, length=1.0):
    import numpy as np
    cmd = [_get_ffmpeg_bin(), "-loglevel", "error", "-ss", str(start), "-t", str(length), "-i", path, "-vn", "-ac", "1",
           "-ar", "16000", "-f", "s16le", "-"]
    samples = np.frombuffer(run(cmd).stdout, dtype="<i2").astype("float32")
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.argmax(spectrum) * 16000 / len(samples))


# three scenes: slowed exactly 1.25x, sped up as far as allowed, and a voice that needs the voice speed-up too
SCENES = [(2.0, 6.0, 5.0), (10.0, 14.0, 2.0), (16.0, 18.0, 3.5)]


class SmartSyncRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="pai_smartsync_")
        cls.film = os.path.join(cls.base, "film.mp4")
        if not make_film(cls.film):
            raise unittest.SkipTest("FFmpeg could not generate the test film")
        cls.clips = []
        for index, (_s, _e, seconds) in enumerate(SCENES):
            clip = os.path.join(cls.base, f"scene_{index + 1:04d}.mp3")
            if not make_clip(clip, seconds):
                raise unittest.SkipTest("FFmpeg could not generate a voice clip")
            cls.clips.append(clip)

        # The film every "it works" test looks at is re-timed ONCE (each re-timing is a few seconds of encoding).
        cls.encoder_patch = mock.patch.object(merger_sync, "detect_hardware_encoder", return_value=CPU)
        cls.encoder_patch.start()
        cls.shared_work = tempfile.mkdtemp(prefix="pai_smartsync_shared_")
        cls.shared_state = SimpleNamespace(sync_mode="smart", warnings=[], sync_plan=None)
        cls.shared_plan = cls.build_plan(cls.shared_work)
        cls.input_blocks = [dict(b) for b in cls.shared_plan.script_blocks]
        cls.shared_result = VideoMergerAgent(output_dir=cls.shared_work)._apply_smart_sync(cls.shared_plan, cls.shared_state, cls.film)

    @classmethod
    def tearDownClass(cls):
        cls.encoder_patch.stop()
        shutil.rmtree(cls.base, ignore_errors=True)
        shutil.rmtree(cls.shared_work, ignore_errors=True)

    @classmethod
    def build_plan(cls, work, scenes=SCENES, with_times=True):
        blocks = [{"scene_id": str(i + 1), "start_sec": s, "end_sec": e, "narration": f"line {i + 1}"} if with_times
                  else {"scene_id": str(i + 1), "narration": f"line {i + 1}"} for i, (s, e, _v) in enumerate(scenes)]
        items = [(i, cls.clips[i], _get_audio_duration(cls.clips[i]), blocks[i]) for i in range(len(scenes))]
        plan = MergePlan(output_dir=work, temp_dir=os.path.join(work, "temp"), safe_id="t")
        info = _get_video_info(cls.film)
        plan.video_dur, plan.video_w, plan.video_h, plan.video_fps = info["duration"], info["width"], info["height"], info["fps"]
        plan.script_blocks, plan.audio_items, plan.movie_path = blocks, items, cls.film
        return plan

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_smartsync_work_")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.agent = VideoMergerAgent(output_dir=self.work)
        self.state = SimpleNamespace(sync_mode="smart", warnings=[], sync_plan=None)

    def make_plan(self, scenes=SCENES, with_times=True):
        return self.build_plan(self.work, scenes, with_times)

    # ── the re-timed film ─────────────────────────────────────────────────────
    def test_the_film_is_retimed_to_the_plan(self):
        plan, result = self.shared_plan, self.shared_result
        self.assertNotEqual(result, self.film)
        self.assertEqual(plan.movie_path, result)
        sync = plan.sync_plan
        self.assertIsNotNone(sync)
        self.assertAlmostEqual(_get_video_info(result)["duration"], sync.out_duration, delta=0.15)
        self.assertEqual(plan.video_dur, _get_video_info(result)["duration"])
        self.assertEqual((plan.video_w, plan.video_h), (160, 120))
        self.assertTrue(_has_audio_stream(result))
        self.assertGreater(sync.out_duration, LENGTH)          # the scenes were slowed more than they were sped up

    def test_every_moment_of_the_new_film_comes_from_the_right_moment_of_the_source(self):
        plan, result = self.shared_plan, self.shared_result
        sync = plan.sync_plan
        for out_time in (1.0, 3.0, 4.5, 6.5, 8.0, 11.8, 12.6, 13.5, 15.0, 17.0, 18.5, 20.5):
            source_time = sync.to_source(out_time)
            self.assertAlmostEqual(luma(result, out_time), luma(self.film, source_time), delta=9.0,
                                   msg=f"new {out_time:.2f}s should show the source at {source_time:.2f}s")

    def test_the_scenes_follow_their_voice_exactly(self):
        plan = self.shared_plan
        first, second, third = (plan.sync_plan.block_segment(i) for i in range(3))
        self.assertAlmostEqual(first.out_length, 5.0, delta=0.05)         # 4 s of picture slowed to the 5 s voice
        self.assertAlmostEqual(first.factor, 1.25, delta=0.02)
        self.assertAlmostEqual(second.factor, 0.8, delta=0.02)             # a 2 s voice for a 4 s scene: as fast as allowed
        self.assertGreater(third.factor, 1.25)                             # a 3.5 s voice for a 2 s scene

    def test_the_sound_keeps_its_pitch_when_it_is_slowed_or_sped_up(self):
        plan, result = self.shared_plan, self.shared_result
        sync = plan.sync_plan
        for segment in (sync.block_segment(0), sync.block_segment(1)):
            middle = (segment.out_start + segment.out_end) / 2
            self.assertAlmostEqual(dominant_frequency(result, middle - 0.5, 1.0), 440.0, delta=12.0, msg=segment)

    # ── the lines on the new film ─────────────────────────────────────────────
    def test_the_lines_start_where_their_scene_starts_on_the_new_film(self):
        plan = self.shared_plan
        sync = plan.sync_plan
        self.assertEqual(len(plan.audio_items), 3)
        for index, (_sorted, _clip, duration, block) in enumerate(plan.audio_items):
            self.assertAlmostEqual(block["start_sec"], sync.block_start(index), places=6)
            self.assertAlmostEqual(block["end_sec"] - block["start_sec"], duration, places=6)
        self.assertEqual(plan.script_blocks, [item[3] for item in plan.audio_items])

    def test_a_clip_that_still_does_not_fit_is_sped_up_and_the_original_is_untouched(self):
        plan = self.shared_plan
        _sorted, clip, duration, _block = plan.audio_items[2]
        self.assertTrue(clip.endswith("voice_0003.wav"))
        self.assertAlmostEqual(duration, 3.5 / 1.15, delta=0.08)
        self.assertEqual(plan.audio_items[0][1], self.clips[0])            # the others are used as they are
        self.assertTrue(os.path.exists(self.clips[2]))

    def test_the_scripts_own_times_are_never_changed(self):
        # the plan carries moved COPIES; the dicts handed in (the project's own) keep the times of the original film
        self.assertEqual([(b["start_sec"], b["end_sec"]) for b in self.input_blocks], [(s, e) for s, e, _v in SCENES])
        for moved, original in zip(self.shared_plan.script_blocks, self.input_blocks):
            self.assertIsNot(moved, original)
        self.assertNotEqual(self.shared_plan.script_blocks[1]["start_sec"], SCENES[1][0])

    def test_the_plan_is_kept_with_the_project(self):
        import json
        saved = json.load(open(os.path.join(self.shared_work, "sync_plan.json"), encoding="utf-8"))
        self.assertEqual(saved["summary"]["scenes"], 3)
        self.assertEqual(self.shared_state.sync_plan["summary"], saved["summary"])
        self.assertGreater(len(saved["segments"]), 3)

    # ── when nothing needs doing ──────────────────────────────────────────────
    def test_audio_fit_leaves_everything_alone(self):
        plan = self.make_plan()
        self.state.sync_mode = "audio_fit"
        self.assertEqual(self.agent._apply_smart_sync(plan, self.state, self.film), self.film)
        self.assertIsNone(plan.sync_plan)
        self.assertEqual(plan.audio_items[0][1], self.clips[0])

    def test_lines_that_already_fit_leave_the_film_alone(self):
        fitting = [(2.0, 4.0, _get_audio_duration(self.clips[0])), (10.0, 12.0, _get_audio_duration(self.clips[1]))]
        plan = self.make_plan([(s, e, v) for s, e, v in fitting])
        plan.audio_items = [(i, self.clips[i], fitting[i][2], plan.script_blocks[i]) for i in range(2)]
        plan.script_blocks[0].update(end_sec=2.0 + fitting[0][2])
        plan.script_blocks[1].update(end_sec=10.0 + fitting[1][2])
        self.assertEqual(self.agent._apply_smart_sync(plan, self.state, self.film), self.film)
        self.assertIsNone(plan.sync_plan)

    def test_a_script_without_scene_times_is_skipped_with_a_warning(self):
        plan = self.make_plan(with_times=False)
        self.assertEqual(self.agent._apply_smart_sync(plan, self.state, self.film), self.film)
        self.assertTrue(any("no scene times" in w for w in self.state.warnings))

    def test_no_clips_nothing_to_follow(self):
        plan = self.make_plan()
        plan.audio_items = []
        self.assertEqual(self.agent._apply_smart_sync(plan, self.state, self.film), self.film)

    # ── trouble ───────────────────────────────────────────────────────────────
    def test_an_unreadable_film_fails_with_the_way_out(self):
        plan = self.make_plan()
        broken = os.path.join(self.work, "broken.mp4")
        open(broken, "wb").write(b"not a video")
        with self.assertRaises(RuntimeError) as caught:
            self.agent._apply_smart_sync(plan, self.state, broken)
        self.assertIn("audio_fit", str(caught.exception))

    def test_the_stop_button_stops_the_re_timing(self):
        plan = self.make_plan()
        with mock.patch.dict(os.environ, {"CURRENT_JOB_CANCELLED": "1"}):
            with self.assertRaises(InterruptedError):
                self.agent._apply_smart_sync(plan, self.state, self.film)

    def test_a_far_too_long_line_is_reported_to_the_user(self):
        scenes = [(2.0, 3.0, 5.0)]
        plan = self.make_plan(scenes)
        self.agent._apply_smart_sync(plan, self.state, self.film)
        self.assertTrue(any("Script Editor" in w for w in self.state.warnings))

    # ── the background effects ────────────────────────────────────────────────
    def test_the_background_stem_follows_the_same_segments(self):
        plan = copy.copy(self.shared_plan)
        stem = os.path.join(self.base, "stem.wav")
        run([_get_ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=300:duration={LENGTH}", stem])
        plan.bg_source_type, plan.bg_audio_file = "demucs", stem
        self.agent._retime_background(plan)
        self.assertNotEqual(plan.bg_audio_file, stem)
        self.assertAlmostEqual(_get_audio_duration(plan.bg_audio_file), plan.sync_plan.out_duration, delta=0.2)
        self.assertEqual(plan.bg_source_type, "demucs")

    def test_other_background_sources_are_not_touched(self):
        plan = copy.copy(self.shared_plan)
        for kind in ("orig", "bgm", "none"):
            plan.bg_source_type, plan.bg_audio_file = kind, "x.wav"
            self.agent._retime_background(plan)
            self.assertEqual(plan.bg_audio_file, "x.wav")

    def test_nothing_happens_to_the_background_without_smart_sync(self):
        plan = self.make_plan()
        plan.bg_source_type, plan.bg_audio_file = "demucs", "x.wav"
        self.agent._retime_background(plan)
        self.assertEqual(plan.bg_audio_file, "x.wav")


class MergeIntegrationTests(unittest.TestCase):
    def test_the_slow_fallback_cannot_follow_a_retimed_film_and_says_so(self):
        agent = VideoMergerAgent()
        plan = MergePlan(movie_path="retimed.mkv", sync_plan=smart_sync.SyncPlan())
        with mock.patch.object(agent, "_build_merge_plan", return_value=plan), \
                mock.patch.object(agent, "_render_single_pass", return_value=False), \
                mock.patch.object(agent, "_legacy_moviepy_merge") as legacy:
            with self.assertRaises(RuntimeError) as caught:
                agent.merge_video(SimpleNamespace(), "movie.mp4")
        self.assertIn("audio_fit", str(caught.exception))
        legacy.assert_not_called()

    def test_the_render_uses_the_retimed_film(self):
        agent = VideoMergerAgent()
        plan = MergePlan(movie_path="retimed.mkv")
        seen = {}

        def render(this_plan, state, movie):
            seen["movie"] = movie
            return True

        with mock.patch.object(agent, "_build_merge_plan", return_value=plan), \
                mock.patch.object(agent, "_render_single_pass", side_effect=render), \
                mock.patch.object(agent, "_apply_intro_outro"):
            agent.merge_video(SimpleNamespace(), "movie.mp4")
        self.assertEqual(seen["movie"], "retimed.mkv")

    def test_without_smart_sync_the_given_film_is_used(self):
        agent = VideoMergerAgent()
        plan = MergePlan()
        seen = {}
        with mock.patch.object(agent, "_build_merge_plan", return_value=plan), \
                mock.patch.object(agent, "_render_single_pass", side_effect=lambda p, s, m: seen.update(movie=m) or True), \
                mock.patch.object(agent, "_apply_intro_outro"):
            agent.merge_video(SimpleNamespace(), "movie.mp4")
        self.assertEqual(seen["movie"], "movie.mp4")


class FilterStringTests(unittest.TestCase):
    def test_atempo_steps_stay_within_ffmpegs_range(self):
        self.assertEqual(smart_sync.atempo_filter(1.0), "anull")
        self.assertEqual(smart_sync.atempo_filter(1.15), "atempo=1.15000")
        self.assertEqual(smart_sync.atempo_filter(3.0).count("atempo"), 2)
        self.assertEqual(smart_sync.atempo_filter(0.3).count("atempo"), 2)
        for tempo in (0.3, 0.45, 0.8, 1.4, 2.5, 3.9):
            product = 1.0
            for step in smart_sync.atempo_filter(tempo).split(","):
                value = float(step.split("=")[1])
                self.assertTrue(0.5 <= value <= 2.0, step)
                product *= value
            self.assertAlmostEqual(product, tempo, places=3)

    def test_the_segment_graph_slows_the_picture_and_the_sound_follows(self):
        graph = smart_sync.segment_graph(1.25, 25.0, True)
        self.assertIn("setpts=(PTS-STARTPTS)*1.250000", graph)
        self.assertIn("fps=25.0000", graph)
        self.assertIn("atempo=0.80000", graph)
        self.assertIn("[v]", graph)
        self.assertIn("[a]", graph)

    def test_without_sound_there_is_no_audio_chain(self):
        graph = smart_sync.segment_graph(0.8, 30.0, False)
        self.assertNotIn("[a]", graph)
        self.assertNotIn("atempo", graph)


if __name__ == "__main__":
    unittest.main()
