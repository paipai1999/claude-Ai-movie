"""
core.audio_trim - trimming Edge TTS dead air, and its wiring into VoiceAgent._speak_with_retry.
Uses the real FFmpeg from imageio-ffmpeg on generated tones; skipped when it cannot encode MP3.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import audio_trim  # noqa: E402

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:   # pragma: no cover - only without the dev dependency
    FFMPEG = None


def make_clip(path, lead=0.5, speech=1.0, tail=0.6, rate=24000):
    """MP3 shaped like an Edge TTS answer: silence, a tone standing in for speech, silence."""
    import numpy as np
    import soundfile as sf

    t = np.arange(int(speech * rate)) / rate
    tone = (0.5 * np.sin(2 * np.pi * 220 * t) * np.sin(np.pi * t / speech)).astype("float32")
    data = np.concatenate([np.zeros(int(lead * rate), "float32"), tone, np.zeros(int(tail * rate), "float32")])
    wav = path + ".src.wav"
    sf.write(wav, data, rate)
    res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", wav, "-c:a", "libmp3lame", "-q:a", "4", path],
                         capture_output=True)
    os.remove(wav)
    return res.returncode == 0 and os.path.exists(path)


def duration(path):
    import soundfile as sf
    return float(sf.info(path).duration)


def can_encode_mp3():
    if not FFMPEG:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return make_clip(os.path.join(tmp, "probe.mp3")) and duration(os.path.join(tmp, "probe.mp3")) > 0
        except Exception:
            return False


@unittest.skipUnless(can_encode_mp3(), "FFmpeg MP3 encoding / soundfile MP3 reading unavailable")
class TrimSilenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pai_trim_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clip = os.path.join(self.tmp, "scene_0001.mp3")

    def test_dead_air_is_removed_but_a_natural_margin_is_kept(self):
        make_clip(self.clip, lead=0.5, speech=1.0, tail=0.6)
        before = duration(self.clip)
        self.assertTrue(audio_trim.trim_silence(FFMPEG, self.clip))
        after = duration(self.clip)
        self.assertLess(after, before - 0.5)
        # speech (1.0s) + ~0.05s lead + ~0.2s tail, with a little encoder slack either way
        self.assertAlmostEqual(after, 1.0 + audio_trim.KEEP_LEAD_SEC + audio_trim.KEEP_TAIL_SEC, delta=0.15)
        self.assertEqual(os.listdir(self.tmp), ["scene_0001.mp3"], "no temp file left behind")

    def test_the_speech_itself_is_not_clipped(self):
        make_clip(self.clip, lead=0.4, speech=1.2, tail=0.4)
        audio_trim.trim_silence(FFMPEG, self.clip)
        import numpy as np
        import soundfile as sf
        data, rate = sf.read(self.clip)
        loud = np.where(np.abs(data) > 0.05)[0]
        self.assertGreater((loud[-1] - loud[0]) / rate, 1.0)      # almost all of the 1.2 s tone survives

    def test_already_tight_clip_is_left_alone(self):
        make_clip(self.clip, lead=0.0, speech=1.0, tail=0.1)
        with open(self.clip, "rb") as fh:
            original = fh.read()
        self.assertFalse(audio_trim.trim_silence(FFMPEG, self.clip))
        with open(self.clip, "rb") as fh:
            self.assertEqual(fh.read(), original)

    def test_pure_silence_is_never_turned_into_nothing(self):
        make_clip(self.clip, lead=1.0, speech=0.05, tail=1.0)
        # (a 0.05s blip is below the minimum result) - the original must survive untouched
        before = duration(self.clip)
        self.assertFalse(audio_trim.trim_silence(FFMPEG, self.clip))
        self.assertAlmostEqual(duration(self.clip), before, places=2)

    def test_failures_keep_the_original(self):
        make_clip(self.clip)
        before = duration(self.clip)
        self.assertFalse(audio_trim.trim_silence(os.path.join(self.tmp, "no-ffmpeg.exe"), self.clip))
        self.assertFalse(audio_trim.trim_silence(FFMPEG, os.path.join(self.tmp, "missing.mp3")))
        self.assertFalse(audio_trim.trim_silence("", self.clip))
        self.assertAlmostEqual(duration(self.clip), before, places=2)


class VoiceAgentWiringTests(unittest.TestCase):
    """_speak_with_retry trims freshly synthesized speech, but never the silent fallback clip."""

    def _agent(self, trim=True):
        from agents.voice_agent import VoiceAgent
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.voice = "my-MM-ThihaNeural"
        agent.trim_tts_silence = trim
        return agent

    def _run(self, agent, tts_ok):
        calls = {"trim": 0}
        tmp = tempfile.mkdtemp(prefix="pai_voice_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "scene_0001.mp3")

        class FakeCommunicate:
            def __init__(self, *a, **k):
                pass

            async def save(self, path):
                if not tts_ok:
                    raise RuntimeError("service down")
                with open(path, "wb") as fh:
                    fh.write(b"\0" * 4000)

        def fake_trim(ffmpeg, path):
            calls["trim"] += 1
            return True

        fake_edge = types.SimpleNamespace(Communicate=FakeCommunicate)
        async def instant_sleep(*_a, **_k):
            return None

        sleep = patch("asyncio.sleep", new=instant_sleep)
        with patch.dict(sys.modules, {"edge_tts": fake_edge}), patch("agents.voice_agent.trim_silence", fake_trim), \
                patch.object(type(agent), "_ffmpeg_exe", staticmethod(lambda: "ffmpeg")), \
                patch("agents.voice_agent.subprocess.run", return_value=types.SimpleNamespace(returncode=1)), \
                sleep:
            asyncio.run(agent._speak_with_retry("မင်္ဂလာပါ", out, 1, target_dur=None))
        return calls["trim"]

    def test_successful_synthesis_is_trimmed(self):
        self.assertEqual(self._run(self._agent(), tts_ok=True), 1)

    def test_can_be_switched_off(self):
        self.assertEqual(self._run(self._agent(trim=False), tts_ok=True), 0)

    def test_silent_fallback_clip_is_not_trimmed(self):
        self.assertEqual(self._run(self._agent(), tts_ok=False), 0)


if __name__ == "__main__":
    unittest.main()
