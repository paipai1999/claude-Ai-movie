"""
Gemini text-to-speech: a narrator the user can pick (``gemini:Kore``) and the stand-in that keeps a video from going
silent when Microsoft's speech service (Edge TTS) is down. Nothing here reaches the network: the service is replaced by
fakes, the sound is real (encoded by the bundled FFmpeg).
"""

import base64
import io
import json
import math
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
from shutil import rmtree as shutil_rmtree
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import gemini_tts as gt  # noqa: E402
from agents import voice_agent  # noqa: E402
from agents.voice_agent import TTSUnavailableError, VoiceAgent  # noqa: E402
from brain.memory import MovieState  # noqa: E402
from tests import test_tts_guard as guard  # noqa: E402

MP3 = guard.MP3


def pcm(seconds=0.4, rate=24000) -> bytes:
    """A tone as raw 16-bit mono PCM, the way the service sends it."""
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 330 * n / rate))) for n in range(int(seconds * rate)))


def reply(audio=None, rate=24000) -> dict:
    audio = pcm(rate=rate) if audio is None else audio
    part = {"inlineData": {"mimeType": f"audio/L16;codec=pcm;rate={rate}", "data": base64.b64encode(audio).decode()}}
    return {"candidates": [{"content": {"parts": [part]}}]}


def http_error(code, message="", status=""):
    body = json.dumps({"error": {"code": code, "message": message, "status": status}}).encode()
    return urllib.error.HTTPError("https://example.test", code, message, {}, io.BytesIO(body))


class NoSleep(unittest.TestCase):
    """The waits between rounds are skipped (they are seconds long)."""

    def setUp(self):
        patcher = mock.patch("brain.gemini_client._interruptible_sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        import imageio_ffmpeg
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.tmp = tempfile.mkdtemp(prefix="pai_gtts_")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "line.mp3")


class VoiceNameTests(unittest.TestCase):
    def test_names_are_matched_without_regard_to_case(self):
        self.assertEqual(gt.normalize_voice("gemini:kore"), "gemini:Kore")
        self.assertEqual(gt.normalize_voice(" GEMINI:Charon "), "gemini:Charon")
        self.assertEqual(gt.voice_name("gemini:puck"), "Puck")
        self.assertEqual(gt.canonical_voice("gemini:zephyr"), "gemini:Zephyr")

    def test_other_voices_are_left_alone(self):
        self.assertIsNone(gt.normalize_voice("my-MM-ThihaNeural"))
        self.assertIsNone(gt.normalize_voice("gemini:NoSuchVoice"))
        self.assertEqual(gt.canonical_voice("my-MM-ThihaNeural"), "my-MM-ThihaNeural")
        self.assertFalse(gt.is_gemini_voice("my-MM-ThihaNeural"))
        self.assertTrue(gt.is_gemini_voice("gemini:anything"))
        with self.assertRaises(ValueError):
            gt.voice_name("gemini:NoSuchVoice")

    def test_there_are_thirty_voices_and_the_shortcuts_name_real_ones(self):
        self.assertEqual(len(gt.VOICES), 30)
        self.assertEqual(len(set(gt.VOICES)), 30)
        for shortcut in gt.SHORTCUTS.values():
            self.assertIsNotNone(gt.normalize_voice(shortcut))

    def test_the_stand_in_matches_the_gender_of_the_edge_voice(self):
        self.assertEqual(gt.fallback_voice_for("my-MM-ThihaNeural"), "gemini:Charon")
        self.assertEqual(gt.fallback_voice_for("my-MM-NilarNeural"), "gemini:Kore")
        self.assertEqual(gt.fallback_voice_for("en-US-JennyNeural"), "gemini:Kore")
        self.assertEqual(gt.fallback_voice_for("en-US-GuyNeural"), "gemini:Charon")


class RequestTests(unittest.TestCase):
    def test_the_narration_is_sent_as_plain_text(self):
        # A "Say in a calm voice: ..." prefix is read ALOUD by the current models (measured on the real service).
        self.assertEqual(gt.build_prompt("ဒီစာကြောင်း"), "ဒီစာကြောင်း")
        self.assertEqual(gt.build_prompt("ဒီစာကြောင်း", style="   "), "ဒီစာကြောင်း")

    def test_a_configured_style_goes_before_every_line_in_the_same_layout(self):
        first, second = gt.build_prompt("one", "a deep, serious voice"), gt.build_prompt("two", "a deep, serious voice")
        self.assertTrue(first.startswith("### DIRECTOR'S NOTES\nStyle: a deep, serious voice\n\n#### TRANSCRIPT\n"))
        self.assertEqual(first.rsplit("\n", 1)[0], second.rsplit("\n", 1)[0])
        self.assertTrue(first.endswith("one") and second.endswith("two"))

    def test_the_job_speed_becomes_an_ffmpeg_tempo(self):
        self.assertAlmostEqual(gt.tempo_for("+18%"), 1.18)
        self.assertAlmostEqual(gt.tempo_for("-20%"), 0.8)
        self.assertAlmostEqual(gt.tempo_for(10), 1.1)
        for nothing in (None, "", "+0%", "+2%", "abc"):
            self.assertEqual(gt.tempo_for(nothing), 1.0)
        self.assertEqual(gt.tempo_for("+100%"), 1.4)             # never an absurd stretch
        self.assertEqual(gt.tempo_for("-50%"), 0.7)

    def test_the_request_asks_for_audio_with_the_named_voice(self):
        body = gt.build_request("Say in x: hi", "gemini:Kore")
        config = body["generationConfig"]
        self.assertEqual(config["responseModalities"], ["AUDIO"])
        self.assertEqual(config["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Kore")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "Say in x: hi")

    def test_models_come_from_the_environment_then_the_config_then_the_defaults(self):
        with mock.patch.dict(os.environ, {"GEMINI_TTS_MODEL": "a-tts, b-tts"}):
            self.assertEqual(gt.configured_models({"models": ["c"]}), ["a-tts", "b-tts"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_TTS_MODEL", None)
            self.assertEqual(gt.configured_models({"models": ["c-tts"]}), ["c-tts"])
            self.assertEqual(gt.configured_models({}), list(gt.DEFAULT_MODELS))
            self.assertEqual(gt.configured_models(None), list(gt.DEFAULT_MODELS))

    def test_discovery_prefers_the_newest_full_flash_model_and_is_skipped_when_a_model_is_named(self):
        listing = {"models": [
            {"name": "models/gemini-3.8-flash-lite-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro-preview-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-tts-preview", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
        ]}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(listing).encode()
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(gt.list_tts_models("k"), ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
                                                       "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"])
            with mock.patch.dict(os.environ, {"GEMINI_TTS_MODEL": ""}):
                self.assertEqual(gt.resolve_models(["k"], {})[0], "gemini-3.8-flash-tts")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with mock.patch.dict(os.environ, {"GEMINI_TTS_MODEL": ""}):
                self.assertEqual(gt.resolve_models(["k"], {}), list(gt.DEFAULT_MODELS))
        with mock.patch("urllib.request.urlopen") as never, mock.patch.dict(os.environ, {"GEMINI_TTS_MODEL": ""}):
            self.assertEqual(gt.resolve_models(["k"], {"models": ["mine"]}), ["mine"])
            never.assert_not_called()


class ResponseTests(NoSleep):
    def test_the_audio_and_its_sample_rate_are_read_from_the_answer(self):
        audio, rate, mime = gt.audio_from_response(reply(b"\x01\x02" * 100, rate=16000))
        self.assertEqual((audio, rate), (b"\x01\x02" * 100, 16000))
        self.assertIn("L16", mime)

    def test_an_answer_without_audio_is_an_error_that_can_be_retried(self):
        for answer in ({}, {"candidates": []}, {"candidates": [{"content": {"parts": [{"text": "I cannot"}]}, "finishReason": "OTHER"}]},
                       {"promptFeedback": {"blockReason": "SAFETY"}}):
            with self.assertRaises(gt.GeminiTTSError) as caught:
                gt.audio_from_response(answer)
            self.assertEqual(caught.exception.kind, "no_audio")
            self.assertTrue(caught.exception.retryable)

    def test_pcm_becomes_an_mp3(self):
        gt.pcm_to_mp3(pcm(0.5), 24000, self.out, self.ffmpeg)
        self.assertGreater(os.path.getsize(self.out), 800)
        self.assertFalse(os.path.exists(self.out + ".part"))
        with open(self.out, "rb") as fh:
            head = fh.read(3)
        self.assertTrue(head == b"ID3" or head[0] == 0xFF, "an mp3 starts with a tag or a frame sync")

    def test_a_faster_tempo_makes_a_shorter_clip(self):
        slow, fast = os.path.join(self.tmp, "slow.mp3"), os.path.join(self.tmp, "fast.mp3")
        gt.pcm_to_mp3(pcm(1.5), 24000, slow, self.ffmpeg)
        gt.pcm_to_mp3(pcm(1.5), 24000, fast, self.ffmpeg, tempo=1.4)
        self.assertAlmostEqual(self.seconds(slow), 1.5, delta=0.15)
        self.assertAlmostEqual(self.seconds(fast), 1.5 / 1.4, delta=0.15)

    def seconds(self, path):
        """Length of an audio file, read from what ffmpeg prints about it."""
        import re
        import subprocess
        shown = subprocess.run([self.ffmpeg, "-i", path, "-hide_banner"], capture_output=True, text=True).stderr
        h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", shown).groups()
        return int(h) * 3600 + int(m) * 60 + float(s)

    def test_a_clip_that_is_nearly_empty_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaises(gt.GeminiTTSError) as caught:
            gt.pcm_to_mp3(b"\x00\x00" * 10, 24000, self.out, self.ffmpeg)
        self.assertEqual(caught.exception.kind, "no_audio")
        self.assertFalse(os.path.exists(self.out))


class SynthesizerTests(NoSleep):
    def make(self, keys=("k1", "k2", "k3"), models=("tts-a", "tts-b")):
        return gt.GeminiSynthesizer(list(keys), list(models), ffmpeg=self.ffmpeg)

    def script(self, synth, behaviour):
        """Replaces the HTTP call: ``behaviour(key, model)`` returns an answer or raises."""
        calls = []

        def request(key, model, payload):
            calls.append((key, model))
            return behaviour(key, model)

        synth._request = request
        return calls

    def test_the_first_key_that_answers_wins_and_the_model_is_reported(self):
        synth = self.make()
        calls = self.script(synth, lambda key, model: reply())
        self.assertEqual(synth.synthesize("hello", "gemini:Kore", self.out), "tts-a")
        self.assertEqual(len(calls), 1)
        self.assertGreater(os.path.getsize(self.out), 800)

    def test_the_load_is_spread_over_the_keys(self):
        synth = self.make()
        calls = self.script(synth, lambda key, model: reply())
        for _ in range(3):
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual(sorted(key for key, _model in calls), ["k1", "k2", "k3"])

    def test_a_key_with_no_daily_quota_left_is_skipped_for_the_rest_of_the_run(self):
        synth = self.make(keys=("k1", "k2"), models=("tts-a",))

        def behaviour(key, model):
            if key == "k1":
                raise http_error(429, "You exceeded your current quota: GenerateRequestsPerDayPerProjectPerModel", "RESOURCE_EXHAUSTED")
            return reply()

        calls = self.script(synth, behaviour)
        for _ in range(4):
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual([key for key, _m in calls].count("k1"), 1, "k1 is tried once, then left alone")
        self.assertEqual([key for key, _m in calls].count("k2"), 4)

    def fake_time(self, synth):
        """A clock the (patched) sleep advances, so the waits can be checked without waiting."""
        clock = {"now": 1000.0, "slept": []}
        synth._clock = lambda: clock["now"]
        self.sleep.side_effect = lambda seconds: (clock["slept"].append(seconds), clock.update(now=clock["now"] + seconds))
        return clock

    def test_a_per_minute_limit_is_waited_out_exactly_and_then_the_line_goes_on(self):
        synth = self.make(keys=("k1",), models=("tts-a",))
        clock = self.fake_time(synth)
        state = {"n": 0}

        def behaviour(key, model):
            state["n"] += 1
            if state["n"] == 1:
                raise http_error(429, "Resource has been exhausted (e.g. check quota): rate limit per minute", "RESOURCE_EXHAUSTED")
            return reply()

        self.script(synth, behaviour)
        self.assertEqual(synth.synthesize("hello", "gemini:Kore", self.out), "tts-a")
        self.assertEqual(clock["slept"], [gt.RATE_COOLDOWN_SEC])            # one wait, as long as the cooldown, not a guess
        self.assertEqual(state["n"], 2)                                      # and no request while the key was cooling down

    def test_a_key_that_is_cooling_down_is_left_alone_while_another_one_works(self):
        synth = self.make(keys=("k1", "k2"), models=("tts-a",))
        clock = self.fake_time(synth)

        def behaviour(key, model):
            if key == "k1":
                raise http_error(429, "rate limit per minute", "RESOURCE_EXHAUSTED")
            return reply()

        calls = self.script(synth, behaviour)
        for _ in range(4):
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual([k for k, _m in calls].count("k1"), 1, "k1 is asked once, then left alone for the cooldown")
        self.assertEqual(clock["slept"], [])                                 # nobody had to wait

    def test_a_key_that_cooled_down_is_used_again(self):
        synth = self.make(keys=("k1", "k2"), models=("tts-a",))
        clock = self.fake_time(synth)
        limited = {"k1": True}

        def behaviour(key, model):
            if key == "k1" and limited["k1"]:
                raise http_error(429, "rate limit per minute", "RESOURCE_EXHAUSTED")
            return reply()

        calls = self.script(synth, behaviour)
        synth.synthesize("a", "gemini:Kore", self.out)
        synth.synthesize("b", "gemini:Kore", self.out)
        limited["k1"] = False
        clock["now"] += gt.RATE_COOLDOWN_SEC + 1
        for _ in range(3):
            synth.synthesize("c", "gemini:Kore", self.out)
        self.assertIn("k1", [k for k, _m in calls[-3:]])

    def test_when_every_key_stays_limited_the_line_gives_up_after_a_few_waits(self):
        synth = self.make(keys=("k1",), models=("tts-a",))
        clock = self.fake_time(synth)
        self.script(synth, lambda key, model: (_ for _ in ()).throw(http_error(429, "rate limit per minute", "RESOURCE_EXHAUSTED")))
        with self.assertRaises(gt.GeminiTTSError) as caught:
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertTrue(caught.exception.retryable)
        self.assertLessEqual(len(clock["slept"]), gt.MAX_WAITS)

    def test_a_model_that_times_out_is_not_asked_again_with_the_other_keys_and_is_tried_last_afterwards(self):
        synth = self.make(keys=("k1", "k2", "k3"), models=("slow-tts", "fast-tts"))
        said = []
        synth._log = said.append

        def behaviour(key, model):
            if model == "slow-tts":
                raise TimeoutError("timed out")
            return reply()

        calls = self.script(synth, behaviour)
        self.assertEqual(synth.synthesize("hello", "gemini:Kore", self.out), "fast-tts")
        self.assertEqual([m for _k, m in calls].count("slow-tts"), 1, "one 90 s wait, not one per key")
        calls.clear()
        self.assertEqual(synth.synthesize("hello again", "gemini:Kore", self.out), "fast-tts")
        self.assertEqual([m for _k, m in calls], ["fast-tts"], "the slow model is tried last from now on")
        self.assertEqual(sum("did not answer in time" in line for line in said), 1, "said once, not for every line")

    def test_the_log_says_what_happened_once(self):
        synth = self.make(keys=("k1", "k2"), models=("gone-tts", "tts-b"))
        said = []
        synth._log = said.append

        def behaviour(key, model):
            if model == "gone-tts":
                raise http_error(404, "models/gone-tts is not found", "NOT_FOUND")
            if key == "k1":
                raise http_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT")
            return reply()

        self.script(synth, behaviour)
        for _ in range(3):
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual(sum("not available to these keys" in line for line in said), 1)
        self.assertEqual(sum("key #1 is refused" in line for line in said), 1)
        self.assertTrue(all("k1" not in line and "k2" not in line.replace("key #2", "") for line in said))     # never a key

    def test_a_daily_quota_is_said_with_the_key_number_not_the_key(self):
        synth = self.make(keys=("secret-one", "secret-two"), models=("tts-a",))
        said = []
        synth._log = said.append

        def behaviour(key, model):
            if key == "secret-one":
                raise http_error(429, "quota exceeded: per day", "RESOURCE_EXHAUSTED")
            return reply()

        self.script(synth, behaviour)
        synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual(len(said), 1)
        self.assertIn("key #1", said[0])
        self.assertNotIn("secret", said[0])

    def test_a_model_the_service_does_not_know_is_skipped(self):
        synth = self.make(models=("gone-tts", "tts-b"))

        def behaviour(key, model):
            if model == "gone-tts":
                raise http_error(404, "models/gone-tts is not found", "NOT_FOUND")
            return reply()

        calls = self.script(synth, behaviour)
        self.assertEqual(synth.synthesize("hello", "gemini:Kore", self.out), "tts-b")
        self.assertEqual(sum(1 for _k, m in calls if m == "gone-tts"), 1, "a dead model is asked once, not once per key")

    def test_a_refused_key_is_dropped_and_the_next_one_used(self):
        synth = self.make(keys=("bad", "good"), models=("tts-a",))

        def behaviour(key, model):
            if key == "bad":
                raise http_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT")
            return reply()

        calls = self.script(synth, behaviour)
        synth.synthesize("hello", "gemini:Kore", self.out)
        synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual([key for key, _m in calls].count("bad"), 1)

    def test_when_every_key_is_out_of_quota_the_error_says_so_and_is_not_retried(self):
        synth = self.make(keys=("k1", "k2"), models=("tts-a",))
        calls = self.script(synth, lambda key, model: (_ for _ in ()).throw(http_error(429, "quota: per day", "RESOURCE_EXHAUSTED")))
        with self.assertRaises(gt.GeminiTTSError) as caught:
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual((caught.exception.kind, caught.exception.retryable), ("quota", False))
        self.assertIn("daily quota", str(caught.exception))
        before = len(calls)
        with self.assertRaises(gt.GeminiTTSError):
            synth.synthesize("hello again", "gemini:Kore", self.out)
        self.assertEqual(len(calls), before, "later lines fail at once, without another request")

    def test_an_unsupported_location_is_reported_with_the_fix(self):
        synth = self.make(keys=("k1",), models=("tts-a",))
        self.script(synth, lambda key, model: (_ for _ in ()).throw(
            http_error(400, "User location is not supported for the API use.", "FAILED_PRECONDITION")))
        with self.assertRaises(gt.GeminiTTSError) as caught:
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual((caught.exception.kind, caught.exception.retryable), ("region", False))
        self.assertIn("VPN", str(caught.exception))

    def test_an_overloaded_service_is_waited_for_then_reported_as_retryable(self):
        synth = self.make(keys=("k1",), models=("tts-a",))
        calls = self.script(synth, lambda key, model: (_ for _ in ()).throw(http_error(503, "The model is overloaded", "UNAVAILABLE")))
        with self.assertRaises(gt.GeminiTTSError) as caught:
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual((caught.exception.kind, caught.exception.retryable), ("unavailable", True))
        self.assertEqual(len(calls), gt.MAX_ROUNDS)
        self.assertEqual(self.sleep.call_count, gt.MAX_ROUNDS)

    def test_a_network_failure_is_retryable(self):
        synth = self.make(keys=("k1",), models=("tts-a",))
        self.script(synth, lambda key, model: (_ for _ in ()).throw(urllib.error.URLError("no route")))
        with self.assertRaises(gt.GeminiTTSError) as caught:
            synth.synthesize("hello", "gemini:Kore", self.out)
        self.assertTrue(caught.exception.retryable)

    def test_text_instead_of_audio_is_retryable_and_moves_to_the_next_key(self):
        synth = self.make(keys=("k1", "k2"), models=("tts-a",))

        def behaviour(key, model):
            return {"candidates": [{"content": {"parts": [{"text": "Sure!"}]}}]} if key == "k1" else reply()

        self.script(synth, behaviour)
        self.assertEqual(synth.synthesize("hello", "gemini:Kore", self.out), "tts-a")

    def test_no_key_at_all_is_a_clear_error(self):
        with self.assertRaises(gt.GeminiTTSError) as caught:
            gt.GeminiSynthesizer([], ["m"], ffmpeg=self.ffmpeg).synthesize("hello", "gemini:Kore", self.out)
        self.assertEqual((caught.exception.kind, caught.exception.retryable), ("key", False))

    def test_the_request_carries_the_key_in_a_header_not_in_the_address(self):
        synth = self.make(keys=("secret-key",), models=("tts-a",))
        seen = {}

        def urlopen(request, timeout):
            seen["url"], seen["headers"] = request.full_url, dict(request.header_items())
            seen["body"] = json.loads(request.data)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(reply()).encode()
            return response

        with mock.patch("urllib.request.urlopen", side_effect=urlopen), mock.patch.object(gt, "pcm_to_mp3", wraps=gt.pcm_to_mp3) as encode:
            synth.synthesize("hello", "gemini:Kore", self.out, rate="+18%")
        self.assertNotIn("secret-key", seen["url"])
        self.assertEqual(seen["headers"].get("X-goog-api-key"), "secret-key")
        self.assertTrue(seen["url"].endswith("/models/tts-a:generateContent"))
        self.assertEqual(seen["body"]["contents"][0]["parts"][0]["text"], "hello", "plain text, no spoken instruction")
        self.assertAlmostEqual(encode.call_args[0][5], 1.18)


class CheckCommandTests(NoSleep):
    def test_the_check_reports_success(self):
        fake = lambda self_, text, voice, out, rate=None: (open(out, "wb").write(MP3), "tts-a")[1]  # noqa: E731
        with mock.patch("brain.config.load_config", return_value={"gemini": {"api_keys": ["k"]}, "voice": {}}), \
                mock.patch.object(gt, "list_tts_models", return_value=["tts-a"]), \
                mock.patch.object(gt.GeminiSynthesizer, "synthesize", fake):
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                code = gt._main(["--check", "--out", self.out])
        self.assertEqual(code, 0)
        self.assertIn("OK: gemini:Kore spoke with tts-a", buffer.getvalue())
        self.assertNotIn("k1", buffer.getvalue())

    def test_the_check_needs_a_key_and_never_prints_one(self):
        with mock.patch("brain.config.load_config", return_value={"gemini": {"api_keys": []}, "voice": {}}):
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                code = gt._main(["--check"])
        self.assertEqual(code, 2)
        self.assertIn("Gemini API keys found: 0", buffer.getvalue())


# ─────────────────────────────────────────────────────────────────────────────
# The VoiceAgent
# ─────────────────────────────────────────────────────────────────────────────
class AgentCase(guard.GuardCase):
    """The guard tests' agent (a fake Edge service, real FFmpeg), with Gemini wired in as a fake service too."""

    voice = "gemini:Kore"

    def setUp(self):
        super().setUp()
        self.agent.voice = self.voice
        self.agent.requested_voice = self.voice
        self.agent.gemini_cfg, self.agent.gemini_keys = {}, ["k1", "k2"]
        self.gemini_calls = []
        self.gemini_error = None
        self.gemini_failures = []
        for patcher in (
            mock.patch.object(gt.GeminiSynthesizer, "synthesize", autospec=True, side_effect=self._speak),
            mock.patch.object(gt, "resolve_models", return_value=["tts-a"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _speak(self, synthesizer, text, voice, output_path, rate=None):
        self.gemini_calls.append((text, voice, rate))
        if self.gemini_failures:
            raise self.gemini_failures.pop(0)
        if self.gemini_error is not None:
            raise self.gemini_error
        with open(output_path, "wb") as fh:
            fh.write(MP3)
        return "tts-a"

    def edge_calls(self):
        return [text for text, _probe in self.fake.calls]


class GeminiVoiceTests(AgentCase):
    def test_every_line_is_voiced_by_gemini_and_edge_is_never_asked(self):
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.clips()), 4)
        self.assertEqual(len(self.gemini_calls), 4)
        self.assertEqual({voice for _t, voice, _r in self.gemini_calls}, {"gemini:Kore"})
        self.assertEqual(self.edge_calls(), [])
        self.assertEqual(self.state.warnings, [])
        self.assertIsNone(VoiceAgent._fallback_marker(self.out))

    def test_the_narrator_is_one_voice_whatever_the_gender_of_the_line(self):
        self.state.generated_script[1]["gender"] = "female"
        self.agent.generate_voiceover(self.state)
        self.assertEqual({voice for _t, voice, _r in self.gemini_calls}, {"gemini:Kore"})

    def test_edge_defaults_are_not_applied_to_gemini_but_the_jobs_own_speed_is(self):
        self.agent.rate_mm = self.agent.rate_en = "+18%"             # the Edge default of config.json
        self.agent.generate_voiceover(self.state)
        self.assertEqual({rate for _t, _v, rate in self.gemini_calls}, {None})
        shutil_rmtree(self.out)
        self.gemini_calls.clear()
        self.agent.explicit_rate = "+12%"                               # the job asked for it (--tts-rate 12)
        self.agent.generate_voiceover(self.state)
        self.assertEqual({rate for _t, _v, rate in self.gemini_calls}, {"+12%"})

    def test_the_text_is_not_chopped_up_the_way_edge_text_is(self):
        long_line = "ဒီအခန်းမှာ " + "ဇာတ်ကောင်က နောက်ဆုံးတော့ အမှန်တရားကို ရှာတွေ့သွားပါတယ်၊ " * 4 + "**ပြီးပါပြီ**"
        self.state.generated_script[0] = guard.line(1, long_line)
        self.agent.generate_voiceover(self.state)
        spoken = self.gemini_calls[0][0]
        self.assertNotIn("\n", spoken)
        self.assertNotIn("*", spoken)
        self.assertIn("ပြီးပါပြီ", spoken)

    def test_the_cached_clips_are_reused_and_not_paid_for_again(self):
        self.agent.generate_voiceover(self.state)
        self.gemini_calls.clear()
        self.agent.generate_voiceover(self.state)
        self.assertEqual(self.gemini_calls, [])

    def test_a_changed_style_voices_the_lines_again(self):
        self.agent.generate_voiceover(self.state)
        self.gemini_calls.clear()
        self.agent.gemini_cfg = {"style": "a deep, serious voice"}
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.gemini_calls), 4)

    def test_no_key_stops_the_run_with_a_clear_message(self):
        self.agent.gemini_keys = []
        with mock.patch.dict(os.environ, {"PAI_SKIP_TTS_PREFLIGHT": "0"}):
            with self.assertRaises(TTSUnavailableError) as caught:
                self.agent.generate_voiceover(self.state)
        self.assertIn("no Gemini API key", str(caught.exception))
        self.assertEqual(self.gemini_calls, [])

    def test_a_used_up_quota_stops_the_run_at_once_and_names_the_fix(self):
        self.gemini_error = gt.GeminiTTSError("daily quota of every key is used up", kind="quota", retryable=False)
        with self.assertRaises(TTSUnavailableError) as caught:
            self.agent.generate_voiceover(self.state)
        message = str(caught.exception)
        self.assertIn("failed for 4 of 4", message)
        self.assertIn("Gemini keys, their quota and the VPN", message)
        self.assertNotIn("pip install -U edge-tts", message)
        self.assertEqual(len(self.gemini_calls), 4, "one attempt per line, not three")
        self.assertEqual(self.clips(), [])

    def test_a_transient_failure_is_retried(self):
        self.gemini_failures = [gt.GeminiTTSError("busy")]            # the first attempt fails, the second works
        self.state.generated_script = self.state.generated_script[:1]
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.gemini_calls), 2)
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(self.state.warnings, [])


class StandInTests(AgentCase):
    """Edge TTS is down: a Gemini narrator takes over, says so, and keeps the project consistent."""

    voice = "my-MM-ThihaNeural"

    def setUp(self):
        super().setUp()
        self.agent.fallback_voice = "gemini:Charon"
        self.fake.probe_fails = True
        voice_agent._tts_verified_at.clear()
        os.environ["PAI_SKIP_TTS_PREFLIGHT"] = "0"

    def test_a_dead_edge_service_hands_over_to_gemini(self):
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.clips()), 4)
        self.assertEqual(self.speak_calls(), [])                              # no Edge line was ever tried
        self.assertEqual({voice for _t, voice, _r in self.gemini_calls}, {"gemini:Charon"})
        self.assertTrue(self.agent.fallback_used)
        self.assertEqual(self.agent.voice, "gemini:Charon")
        self.assertEqual(self.agent.requested_voice, "my-MM-ThihaNeural")

    def test_the_finished_run_says_which_voice_was_used_and_why(self):
        self.agent.generate_voiceover(self.state)
        self.assertEqual(len(self.state.warnings), 1)
        self.assertIn("Edge TTS was not working", self.state.warnings[0])
        self.assertIn("gemini:Charon", self.state.warnings[0])
        self.assertIn("my-MM-ThihaNeural", self.state.warnings[0])

    def test_the_cause_is_in_the_log(self):
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            self.agent.generate_voiceover(self.state)
        self.assertIn("HTTP 403", buffer.getvalue())
        self.assertIn("narration is made with Gemini (gemini:Charon)", buffer.getvalue())
        self.assertNotIn("wss://", buffer.getvalue())

    def test_without_a_stand_in_the_run_stops_as_before(self):
        self.agent.fallback_voice = None
        with self.assertRaises(TTSUnavailableError) as caught:
            self.agent.generate_voiceover(self.state)
        self.assertIn("pip install -U edge-tts", str(caught.exception))
        self.assertEqual(self.gemini_calls, [])

    def test_a_working_edge_service_is_never_replaced(self):
        self.fake.probe_fails = False
        self.agent.generate_voiceover(self.state)
        self.assertFalse(self.agent.fallback_used)
        self.assertEqual(self.gemini_calls, [])
        self.assertEqual(len(self.speak_calls()), 4)
        self.assertEqual(self.state.warnings, [])

    def test_the_manifest_remembers_that_the_stand_in_made_the_clips(self):
        self.agent.generate_voiceover(self.state)
        self.assertEqual(VoiceAgent._fallback_marker(self.out), "my-MM-ThihaNeural")

    def test_a_second_run_while_edge_is_still_down_reuses_the_gemini_clips(self):
        self.agent.generate_voiceover(self.state)
        self.gemini_calls.clear()
        again = self.new_agent()
        again.generate_voiceover(MovieState(movie_name="Film", generated_script=self.state.generated_script))
        self.assertEqual(self.gemini_calls, [], "nothing is spent twice")
        self.assertTrue(again.fallback_used)

    def test_when_edge_is_back_the_whole_film_is_voiced_again_with_the_chosen_voice(self):
        self.agent.generate_voiceover(self.state)
        self.gemini_calls.clear()
        self.fake.probe_fails = False
        voice_agent._tts_verified_at.clear()
        again = self.new_agent()
        state = MovieState(movie_name="Film", generated_script=self.state.generated_script)
        again.generate_voiceover(state)
        self.assertFalse(again.fallback_used)
        self.assertEqual(len(self.speak_calls()), 4, "one narrator all film long")
        self.assertEqual(self.gemini_calls, [])
        self.assertEqual(state.warnings, [])
        self.assertIsNone(VoiceAgent._fallback_marker(self.out))

    def test_clips_an_edge_voice_made_earlier_are_not_mixed_into_the_gemini_narration(self):
        self.fake.probe_fails = False
        self.agent.generate_voiceover(self.state)                              # a normal run: four Edge clips
        for number in (3, 4):
            os.remove(os.path.join(self.out, f"scene_000{number}.mp3"))        # ... two of which are lost
        self.fake.probe_fails = True
        voice_agent._tts_verified_at.clear()
        again = self.new_agent()
        again.generate_voiceover(MovieState(movie_name="Film", generated_script=self.state.generated_script))
        self.assertTrue(again.fallback_used)
        self.assertEqual(len(self.gemini_calls), 4)

    def new_agent(self):
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.__dict__.update({k: v for k, v in self.agent.__dict__.items()
                               if k not in ("voice", "fallback_used", "_tts_failures", "_fallback_clips", "_gemini", "_fallback_cause")})
        agent.voice = agent.requested_voice
        return agent


class FallbackChoiceTests(unittest.TestCase):
    def agent(self, voice="my-MM-ThihaNeural", keys=("k",), cfg=None):
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.voice, agent.gemini_keys, agent.gemini_cfg = voice, list(keys), cfg or {}
        return agent

    def test_the_stand_in_follows_the_gender_of_the_voice(self):
        with mock.patch.dict(os.environ, {"PAI_TTS_FALLBACK": ""}):
            self.assertEqual(self.agent()._pick_fallback_voice(), "gemini:Charon")
            self.assertEqual(self.agent("my-MM-NilarNeural")._pick_fallback_voice(), "gemini:Kore")

    def test_the_configured_stand_in_wins(self):
        with mock.patch.dict(os.environ, {"PAI_TTS_FALLBACK": ""}):
            self.assertEqual(self.agent(cfg={"fallback_voice": "gemini:puck"})._pick_fallback_voice(), "gemini:Puck")
            self.assertEqual(self.agent(cfg={"fallback_voice": "nonsense"})._pick_fallback_voice(), "gemini:Charon")

    def test_it_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"PAI_TTS_FALLBACK": "off"}):
            self.assertIsNone(self.agent()._pick_fallback_voice())
        with mock.patch.dict(os.environ, {"PAI_TTS_FALLBACK": ""}):
            self.assertIsNone(self.agent(cfg={"fallback": False})._pick_fallback_voice())

    def test_there_is_no_stand_in_without_a_key_or_when_gemini_is_already_the_voice(self):
        with mock.patch.dict(os.environ, {"PAI_TTS_FALLBACK": ""}):
            self.assertIsNone(self.agent(keys=())._pick_fallback_voice())
            self.assertIsNone(self.agent("gemini:Kore")._pick_fallback_voice())

    def test_a_gemini_voice_survives_voice_resolution(self):
        agent = VoiceAgent.__new__(VoiceAgent)
        self.assertEqual(agent._resolve_voice("gemini:kore", {}), "gemini:Kore")
        self.assertEqual(agent._resolve_voice("gemini:NoSuchVoice", {}), "my-MM-ThihaNeural")

    def test_edge_clip_fingerprints_are_unchanged(self):
        import hashlib
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.engine, agent.voice = "edge_tts", "my-MM-ThihaNeural"
        item = {"narration": "စာကြောင်း", "gender": "Male", "emotion": "Sad"}
        expected = hashlib.sha1("|".join(["edge_tts", "my-MM-ThihaNeural", "male", "sad", "စာကြောင်း"]).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(agent._clip_fingerprint(item), expected)


class ShortcutTests(unittest.TestCase):
    def test_the_dashboard_shortcuts_pick_a_gemini_narrator(self):
        from services import job_runtime as rt
        self.assertEqual(rt._resolve_voice_mapping("burmese_gemini_female", None), ("burmese", "gemini:Kore"))
        self.assertEqual(rt._resolve_voice_mapping("burmese_gemini_male", None), ("burmese", "gemini:Charon"))
        self.assertEqual(rt._resolve_voice_mapping("burmese", "gemini:puck"), ("burmese", "gemini:Puck"))
        self.assertEqual(rt._resolve_voice_mapping("burmese_thiha", None), ("burmese", "my-MM-ThihaNeural"))

    def test_the_command_line_shortcuts_pick_a_gemini_narrator(self):
        import main
        pick = lambda language, voice=None: main.resolve_language_and_voice(SimpleNamespace(language=language, tts_voice=voice))  # noqa: E731
        self.assertEqual(pick("burmese_gemini_male"), ("burmese", "gemini:Charon"))
        self.assertEqual(pick("burmese_gemini_female"), ("burmese", "gemini:Kore"))
        self.assertEqual(pick("burmese", "gemini:leda"), ("burmese", "gemini:Leda"))
        self.assertEqual(pick("burmese_nilar"), ("burmese", "my-MM-NilarNeural"))

    def test_the_option_model_and_the_dashboard_offer_them(self):
        from core import options
        language = next(o for o in options.OPTIONS if o.name == "language")
        for shortcut in gt.SHORTCUTS:
            self.assertIn(shortcut, language.choices)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "templates", "index.html"), encoding="utf-8").read()
        for shortcut in gt.SHORTCUTS:
            self.assertIn(f'value="{shortcut}"', html)

    def test_the_default_config_documents_the_section(self):
        import brain.config as cfg
        section = cfg.DEFAULT_CONFIG["voice"]["gemini_tts"]
        self.assertTrue(section["fallback"])
        self.assertEqual(section["models"], [])


if __name__ == "__main__":
    unittest.main()
