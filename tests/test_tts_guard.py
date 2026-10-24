"""
A video with no narration must never be reported as a success.

An outdated edge-tts (HTTP 403 from Microsoft) used to fail every line three times, then fill the video with silent stand-ins,
cache them as if they were real clips, and finish with status COMPLETED, no warning and a CapCut package holding a silent
narration track. Now: one tiny synthesis before the real work (a dead service stops the run at once, with the cause and the
fix), the real error is logged, half the lines failing or more stops the run, fewer failures finish with a warning that names
the lines, and a silent stand-in is never trusted as a cached clip.
"""

import asyncio
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import voice_agent  # noqa: E402
from agents.voice_agent import TTSUnavailableError, VoiceAgent, explain_tts_error  # noqa: E402
from brain.memory import MovieState  # noqa: E402

FORBIDDEN = RuntimeError("403, message='Invalid response status', url='wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?TrustedClientToken=abc&ConnectionId=def'")


MARKS = {1: "ပထမ", 2: "ဒုတိယ", 3: "တတိယ", 4: "စတုတ္ထ"}


def line(n, text=None, **extra):
    return dict({"scene_id": str(n), "narration": text or f"ဒီ {MARKS[n]} စာကြောင်းကို ဖတ်ပါမယ်", "start_sec": n * 5.0, "end_sec": n * 5.0 + 4.0,
                 "gender": "male", "emotion": "normal"}, **extra)


def real_mp3() -> bytes:
    """A 1 s tone encoded by the bundled FFmpeg (falls back to filler bytes when it cannot encode MP3)."""
    import subprocess
    import imageio_ffmpeg
    path = os.path.join(tempfile.mkdtemp(prefix="pai_ttsguard_mp3_"), "tone.mp3")
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
                             "-c:a", "libmp3lame", path], capture_output=True)
    if result.returncode == 0 and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    return b"\0" * 4096


MP3 = real_mp3()


class FakeEdge:
    """Replaces edge_tts.Communicate: ``fail(text)`` decides which lines the service refuses."""

    def __init__(self, fail=lambda text: False, probe_fails=False):
        self.fail, self.probe_fails = fail, probe_fails
        self.calls = []                       # (text, is_probe)

    def __call__(self, text, voice, **kwargs):
        owner = self
        probe = text in ("Hello", "မင်္ဂလာပါ")
        self.calls.append((text, probe))

        class Communicate:
            async def save(self, path):
                if (probe and owner.probe_fails) or (not probe and owner.fail(text)):
                    raise FORBIDDEN
                with open(path, "wb") as fh:
                    fh.write(MP3)
        return Communicate()


class GuardCase(unittest.TestCase):
    def setUp(self):
        import edge_tts
        self.tmp = tempfile.mkdtemp(prefix="pai_ttsguard_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "voiceover")
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.engine, agent.voice, agent.language = "edge_tts", "my-MM-ThihaNeural", "burmese"
        agent.rate_mm = agent.rate_en = "+0%"
        agent.output_dir = self.out
        agent.f5_cfg, agent.f5_engine = {}, None
        agent.trim_tts_silence = False
        self.agent = agent
        voice_agent._tts_verified_at.clear()
        self.fake = FakeEdge()
        import imageio_ffmpeg
        for patcher in (
            mock.patch.object(edge_tts, "Communicate", side_effect=lambda *a, **k: self.fake(*a, **k)),
            mock.patch("asyncio.sleep", new=mock.AsyncMock()),                      # the 2 + 4 + 8 s back-off
            mock.patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": imageio_ffmpeg.get_ffmpeg_exe(), "PAI_SKIP_TTS_PREFLIGHT": "0"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = MovieState(movie_name="Film")
        self.state.generated_script = [line(1), line(2), line(3), line(4)]

    def clips(self):
        return sorted(n for n in os.listdir(self.out) if n.endswith(".mp3")) if os.path.isdir(self.out) else []

    def speak_calls(self):
        return [text for text, probe in self.fake.calls if not probe]

    def probe_calls(self):
        return [text for text, probe in self.fake.calls if probe]


class PreflightTests(GuardCase):
    def test_a_refusing_service_stops_the_run_before_any_line_is_tried(self):
        self.fake.probe_fails = True
        with self.assertRaises(TTSUnavailableError) as caught:
            self.agent.generate_voiceover(self.state)
        message = str(caught.exception)
        self.assertIn("403", message)
        self.assertIn("pip install -U edge-tts", message)
        self.assertIn("my-MM-ThihaNeural", message)
        self.assertNotIn("wss://", message)                      # the link with its token is not repeated
        self.assertEqual(self.speak_calls(), [])                 # not one of the 4 lines was tried (no 3 x 4 retries)
        self.assertEqual(self.clips(), [])

    def test_the_probe_leaves_no_temporary_file(self):
        before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("pai_tts_probe_")}
        self.fake.probe_fails = True
        with self.assertRaises(TTSUnavailableError):
            self.agent.generate_voiceover(self.state)
        self.agent.generate_voiceover  # noqa: B018 - (a second, successful run below)
        self.fake.probe_fails = False
        self.agent.generate_voiceover(self.state)
        self.assertEqual({n for n in os.listdir(tempfile.gettempdir()) if n.startswith("pai_tts_probe_")}, before)

    def test_a_working_service_passes_and_is_not_asked_again_for_the_next_video(self):
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.probe_calls()), 1)
        self.assertEqual(len(self.speak_calls()), 4)
        shutil.rmtree(self.out)
        self.agent.generate_voiceover(self.state)                # a second video of a batch, a moment later
        self.assertEqual(len(self.probe_calls()), 1)

    def test_a_service_that_never_answers_times_out_with_a_useful_message(self):
        async def hang(self_, path):
            await asyncio.sleep(0)               # (asyncio.sleep is patched to return at once, so wait on an event instead)
            await asyncio.Event().wait()

        with mock.patch.object(voice_agent, "PREFLIGHT_TIMEOUT_SEC", 0.2):
            self.fake.__class__.__call__ = self.fake.__class__.__call__
            original = self.fake.__call__

            def hanging(text, voice, **kwargs):
                comm = original(text, voice, **kwargs)
                comm.save = lambda path: asyncio.Event().wait()
                return comm
            self.fake.__call__ = hanging
            import edge_tts
            with mock.patch.object(edge_tts, "Communicate", side_effect=hanging):
                with self.assertRaises(TTSUnavailableError) as caught:
                    self.agent.generate_voiceover(self.state)
        self.assertIn("did not answer", str(caught.exception))

    def test_the_preflight_can_be_skipped(self):
        self.fake.probe_fails = True
        with mock.patch.dict(os.environ, {"PAI_SKIP_TTS_PREFLIGHT": "1"}):
            self.agent.generate_voiceover(self.state)
        self.assertEqual(self.probe_calls(), [])
        self.assertEqual(len(self.clips()), 4)

    def test_cached_clips_need_no_service_at_all(self):
        self.agent.generate_voiceover(self.state)
        voice_agent._tts_verified_at.clear()
        self.fake.probe_fails = True
        self.fake.fail = lambda text: True
        calls = len(self.fake.calls)
        self.agent.generate_voiceover(self.state)                # every clip is already there and still matches
        self.assertEqual(len(self.fake.calls), calls)


class FailurePolicyTests(GuardCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(VoiceAgent, "_preflight_tts", lambda self: None)       # these tests are about the lines
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_line_failing_stops_the_run_and_leaves_no_silent_clips(self):
        self.fake.fail = lambda text: True
        with self.assertRaises(TTSUnavailableError) as caught:
            self.agent.generate_voiceover(self.state)
        self.assertIn("failed for 4 of 4", str(caught.exception))
        self.assertIn("403", str(caught.exception))
        self.assertIn("the transcript and the script are kept", str(caught.exception))
        self.assertEqual(self.clips(), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, voice_agent.VOICE_MANIFEST_NAME)))

    def test_half_of_the_lines_failing_is_already_too_many(self):
        self.fake.fail = lambda text: MARKS[2] in text or MARKS[4] in text
        with self.assertRaises(TTSUnavailableError) as caught:
            self.agent.generate_voiceover(self.state)
        self.assertIn("failed for 2 of 4", str(caught.exception))
        # The silent stand-ins are gone; the two clips that really were synthesized stay (and are vouched for), so the next
        # run only voices the lines that failed.
        self.assertEqual(self.clips(), ["scene_0001.mp3", "scene_0003.mp3"])
        self.assertEqual(sorted(voice_agent.VoiceAgent._load_manifest(self.out)), ["scene_0001.mp3", "scene_0003.mp3"])
        self.fake.fail = lambda text: False
        self.fake.calls.clear()
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.speak_calls()), 2)
        self.assertEqual(len(self.clips()), 4)

    def test_a_minority_failing_finishes_with_a_warning_that_names_the_line(self):
        self.fake.fail = lambda text: MARKS[3] in text
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.state.warnings), 1)
        self.assertIn("1 of 4", self.state.warnings[0])
        self.assertIn("scene 3", self.state.warnings[0])
        self.assertEqual(len(self.clips()), 4)                            # the silent stand-in keeps the timeline in step ...
        manifest = voice_agent.VoiceAgent._load_manifest(self.out)
        self.assertNotIn("scene_0003.mp3", manifest)                      # ... but is never vouched for
        self.assertEqual(sorted(manifest), ["scene_0001.mp3", "scene_0002.mp3", "scene_0004.mp3"])

    def test_the_failed_line_is_synthesized_again_on_the_next_run(self):
        self.fake.fail = lambda text: MARKS[3] in text
        self.agent.generate_voiceover(self.state)
        self.fake.fail = lambda text: False
        self.fake.calls.clear()
        state = MovieState(movie_name="Film")
        state.generated_script = self.state.generated_script
        self.agent.generate_voiceover(state)
        self.assertEqual(len(self.speak_calls()), 1)                      # only the line that had failed
        self.assertIn(MARKS[3], self.speak_calls()[0])
        self.assertEqual(state.warnings, [])
        self.assertEqual(sorted(voice_agent.VoiceAgent._load_manifest(self.out)), [f"scene_000{n}.mp3" for n in (1, 2, 3, 4)])

    def test_the_real_error_is_logged_instead_of_swallowed(self):
        self.fake.fail = lambda text: MARKS[3] in text
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.agent.generate_voiceover(self.state)
        output = buffer.getvalue()
        self.assertIn("Scene 3 TTS failed after 3 attempts: RuntimeError: 403", output)
        self.assertNotIn("wss://", output)

    def test_lines_that_are_silent_on_purpose_do_not_count_as_failures(self):
        self.state.generated_script[1] = line(2, "Left in English", untranslated=True)
        self.fake.fail = lambda text: MARKS[3] in text
        self.agent.generate_voiceover(self.state)                         # 1 failure out of 3 spoken lines: a warning, not a stop
        self.assertIn("1 of 3", self.state.warnings[0])


class ExplainTests(unittest.TestCase):
    def test_forbidden_means_update_edge_tts(self):
        text = explain_tts_error(FORBIDDEN, "my-MM-ThihaNeural")
        self.assertIn("HTTP 403", text)
        self.assertIn("pip install -U edge-tts", text)
        self.assertIn("installed:", text)
        self.assertNotIn("TrustedClientToken", text)

    def test_timeouts_and_connection_errors_point_at_the_network(self):
        for error in (TimeoutError(), ConnectionError("Cannot connect to host"), OSError("getaddrinfo failed: DNS resolve")):
            self.assertIn("internet connection", explain_tts_error(error, "en-US-GuyNeural"), repr(error))

    def test_anything_else_still_says_what_to_do(self):
        text = explain_tts_error(ValueError("weird"), "en-US-GuyNeural")
        self.assertIn("ValueError: weird", text)
        self.assertIn("try again", text)

    def test_the_message_is_one_bounded_line(self):
        text = explain_tts_error(RuntimeError("x\n" * 500), "my-MM-ThihaNeural")
        self.assertNotIn("\n", text)
        self.assertLess(len(text), 700)


if __name__ == "__main__":
    unittest.main()
