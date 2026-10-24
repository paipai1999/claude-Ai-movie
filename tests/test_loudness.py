"""
The finished mix is brought to a normal listening level.

The narration clips are quiet (about -24 dB) and ``amix`` halves every input, so a Recap used to come out near -29 LUFS,
far below what YouTube / TikTok / Reels play at. ``loudnorm`` at the end of the voice-over mix fixes it; the film's own
mix (100% original audio) is left alone.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.merger.plan import MergePlan  # noqa: E402
from agents.video_merger_agent import VideoMergerAgent  # noqa: E402


def make_plan(config=None, **kw):
    plan = MergePlan(effective_video_dur=20.0, video_fps=24.0)
    plan.config_data = config or {}
    for key, value in kw.items():
        setattr(plan, key, value)
    return plan


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.agent = VideoMergerAgent()
        self.state = MagicMock(audio_mode="ai_voiceover", sfx_volume=None, audio_anti_copyright=False)

    def chain(self, plan=None, vo=1, bg=2, **state):
        for key, value in state.items():
            setattr(self.state, key, value)
        return ";".join(self.agent._audio_filter_parts(plan or make_plan(bg_source_type="orig"), self.state, vo, bg, None))

    def test_a_voice_over_mix_is_normalised_before_it_is_split_into_the_two_masters(self):
        chain = self.chain()
        self.assertIn("loudnorm=I=-16:LRA=11:TP=-1.5,aresample=48000,asplit=2[a_master1][a_master2]", chain)

    def test_every_voice_over_variant_is_normalised(self):
        for bg_type in ("none", "orig", "demucs", "bgm", "both"):
            self.assertIn("loudnorm", self.chain(make_plan(bg_source_type=bg_type), bg=2), bg_type)
            self.assertIn("loudnorm", self.chain(make_plan({"audio_ducking": {"enabled": False}}, bg_source_type=bg_type), bg=2), bg_type)
        self.assertIn("loudnorm", self.chain(make_plan(bg_source_type="none"), bg=None))          # the voice alone

    def test_no_voice_over_nothing_to_normalise(self):
        self.assertNotIn("loudnorm", self.chain(vo=None))

    def test_the_films_own_audio_is_left_alone(self):
        self.assertNotIn("loudnorm", self.chain(audio_mode="original"))

    def test_it_can_be_switched_off_or_retargeted(self):
        self.assertNotIn("loudnorm", self.chain(make_plan({"audio_ducking": {"normalize_loudness": False}}, bg_source_type="orig")))
        self.assertIn("loudnorm=I=-14:", self.chain(make_plan({"audio_ducking": {"target_lufs": -14}}, bg_source_type="orig")))
        self.assertIn("loudnorm=I=-9:", self.chain(make_plan({"audio_ducking": {"target_lufs": -3}}, bg_source_type="orig")))
        self.assertIn("loudnorm=I=-30:", self.chain(make_plan({"audio_ducking": {"target_lufs": -80}}, bg_source_type="orig")))

    def test_the_anti_copyright_tempo_still_comes_first(self):
        chain = self.chain(audio_anti_copyright=True)
        self.assertLess(chain.index("atempo=1.008"), chain.index("loudnorm"))

    def test_the_default_config_documents_the_switches(self):
        import brain.config as cfg
        self.assertTrue(cfg.DEFAULT_CONFIG["audio_ducking"]["normalize_loudness"])
        self.assertEqual(cfg.DEFAULT_CONFIG["audio_ducking"]["target_lufs"], -16.0)


class MeasuredTests(unittest.TestCase):
    """The real FFmpeg, real numbers: a quiet voice and a quiet ambience, mixed the way the merger mixes them."""

    @classmethod
    def setUpClass(cls):
        import imageio_ffmpeg
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cls.tmp = tempfile.mkdtemp(prefix="pai_loudness_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def mix_lufs(self, config):
        agent = VideoMergerAgent()
        state = MagicMock(audio_mode="ai_voiceover", sfx_volume=None, audio_anti_copyright=False)
        parts = agent._audio_filter_parts(make_plan(config, bg_source_type="orig"), state, 1, None, None)
        out = os.path.join(self.tmp, f"mix_{abs(hash(str(config)))}.wav")
        # input 0 = the film's own sound (quiet), input 1 = the narration (-26 dB)
        cmd = [self.ffmpeg, "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "sine=frequency=180:duration=20,volume=0.03",
               "-f", "lavfi", "-i", "sine=frequency=330:duration=20,volume=0.05",
               "-filter_complex", ";".join(parts), "-map", "[a_master1]", out, "-map", "[a_master2]", "-f", "null", "-"]
        done = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        shown = subprocess.run([self.ffmpeg, "-i", out, "-af", "ebur128=peak=true", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
        return float(re.search(r"Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS", shown).group(1))

    def test_the_mix_reaches_the_target_and_was_far_too_quiet_before(self):
        before = self.mix_lufs({"audio_ducking": {"normalize_loudness": False}})
        after = self.mix_lufs({})
        self.assertLess(before, -24.0, "this is the problem: the un-normalised mix is much too quiet")
        self.assertAlmostEqual(after, -16.0, delta=2.5)

    def test_another_target_is_honoured(self):
        self.assertAlmostEqual(self.mix_lufs({"audio_ducking": {"target_lufs": -20}}), -20.0, delta=2.5)


if __name__ == "__main__":
    unittest.main()
