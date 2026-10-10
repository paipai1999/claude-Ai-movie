"""
Regression tests for resume integrity and pipeline correctness fixes:
  - drama_novel style reaches the WriterAgent through MasterAgent
  - changed style/language/voice invalidates cached script + voiceover (no stale reuse)
  - Phase 2/3 audio path is propagated; empty transcript fails loudly
  - per-clip voice cache (manifest) + F5 speaker-key lookup
  - HardsubEngine: project_name traversal, no CPU re-render after Stop
  - cookie discovery shared by every engine, atomic quota reservation
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.master import MasterAgent, PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE
from agents.voice_agent import VoiceAgent, VOICE_MANIFEST_NAME


class _TempCwd(unittest.TestCase):
    """Runs every test inside a throw-away working directory (relative outputs/ temp/ paths)."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pai_resume_")
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        import brain.config as cfg
        cfg._config_cache = None
        os.environ["CURRENT_JOB_CANCELLED"] = "0"

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.work, ignore_errors=True)
        import brain.config as cfg
        cfg._config_cache = None


class TestMasterStyleMapping(_TempCwd):

    def test_drama_novel_style_is_not_downgraded_to_recap(self):
        for alias in ("drama_novel", "audio_drama", "chinese_drama"):
            master = MasterAgent("clip.mp4", translation_style=alias)
            self.assertEqual(master.translation_style, "drama_novel")
            self.assertEqual(master.script_engine, "translate")
            self.assertEqual(master.state.translation_style, "drama_novel")

    def test_other_styles_unchanged(self):
        self.assertEqual(MasterAgent("clip.mp4", translation_style="wuxia").translation_style, "wuxia")
        self.assertEqual(MasterAgent("clip.mp4", translation_style="persona").translation_style, "persona")
        self.assertEqual(MasterAgent("clip.mp4", translation_style="dialogue").translation_style, "dialogue")
        self.assertEqual(MasterAgent("clip.mp4").translation_style, "recap")


class TestResumeInvalidation(_TempCwd):

    def _seed_previous_run(self, **kwargs):
        """Runs a 'previous job': builds a master, fakes finished phases + voice clips, saves state."""
        first = MasterAgent("clip.mp4", **kwargs)
        first.state.generated_script = [{"scene_id": "1", "narration": "မင်္ဂလာပါ", "start_sec": 0.0, "end_sec": 2.0}]
        first.state.completed_phases = [PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE]
        vo_dir = os.path.join("outputs", first.state.project_dir, "voiceover")
        os.makedirs(vo_dir, exist_ok=True)
        with open(os.path.join(vo_dir, "scene_0001.mp3"), "wb") as f:
            f.write(b"\x00" * 4000)
        with open(os.path.join(vo_dir, VOICE_MANIFEST_NAME), "w") as f:
            json.dump({"clips": {"scene_0001.mp3": "abc"}}, f)
        first.save_state()
        return vo_dir

    def test_same_options_keep_cached_script_and_voice(self):
        vo_dir = self._seed_previous_run(translation_style="persona")
        again = MasterAgent("clip.mp4", translation_style="persona")
        self.assertTrue(again.state.generated_script)
        self.assertIn(PHASE_5_VOICE, again.state.completed_phases)
        self.assertTrue(os.path.exists(os.path.join(vo_dir, "scene_0001.mp3")))

    def test_style_change_drops_script_voice_and_downstream_phases(self):
        vo_dir = self._seed_previous_run(translation_style="persona")
        changed = MasterAgent("clip.mp4", translation_style="wuxia")
        self.assertEqual(changed.state.generated_script, [])
        for phase in (PHASE_4_SCRIPT, PHASE_5_VOICE, PHASE_6_MERGE):
            self.assertNotIn(phase, changed.state.completed_phases)
        self.assertFalse(os.path.exists(os.path.join(vo_dir, "scene_0001.mp3")))
        self.assertFalse(os.path.exists(os.path.join(vo_dir, VOICE_MANIFEST_NAME)))

    def test_context_hint_change_regenerates_script(self):
        self._seed_previous_run(translation_style="persona", context_hint="hero is Zhang Wei")
        changed = MasterAgent("clip.mp4", translation_style="persona", context_hint="hero is Li Na")
        self.assertEqual(changed.state.generated_script, [])

    def test_voice_change_keeps_script_but_drops_voiceover(self):
        vo_dir = self._seed_previous_run(translation_style="persona", tts_voice="my-MM-ThihaNeural")
        changed = MasterAgent("clip.mp4", translation_style="persona", tts_voice="my-MM-NilarNeural")
        self.assertTrue(changed.state.generated_script)                 # script still valid
        self.assertIn(PHASE_4_SCRIPT, changed.state.completed_phases)
        self.assertNotIn(PHASE_5_VOICE, changed.state.completed_phases)
        self.assertNotIn(PHASE_6_MERGE, changed.state.completed_phases)
        self.assertFalse(os.path.exists(os.path.join(vo_dir, "scene_0001.mp3")))

    def test_effective_voice_is_recorded_in_state(self):
        master = MasterAgent("clip.mp4", tts_voice="my-MM-NilarNeural")
        self.assertEqual(master.state.tts_voice, "my-MM-NilarNeural")


class TestPipelineStageGuards(_TempCwd):

    def _prepared_master(self, transcript, audio_path=None):
        master = MasterAgent("clip.mp4", detect_scenes=False, resume=False)

        def fake_metadata(state):
            state.duration_sec = 60.0
            state.resolution = "1920x1080"
            return state

        def fake_extract(state, out_dir, skip_demucs=None):
            if audio_path:
                state.audio_path = audio_path
            return state

        def fake_transcribe(state, model):
            state.transcript = list(transcript)
            return state

        master.video_agent.analyze_metadata = fake_metadata
        master.audio_agent.extract_audio = fake_extract
        master.audio_agent.transcribe_audio = fake_transcribe
        master.audio_agent.correct_transcript = lambda state: state
        return master

    def test_empty_transcript_fails_the_job_instead_of_completing(self):
        master = self._prepared_master(transcript=[])
        with self.assertRaises(RuntimeError) as ctx:
            master.run_pipeline()
        self.assertIn("No dialogue", str(ctx.exception))
        self.assertEqual(master.state.pipeline_status, "FAILED")

    def test_audio_path_is_propagated_from_the_audio_worker(self):
        from brain.memory import TranscriptSegment

        class Halt(Exception):
            pass

        wav = os.path.join(self.work, "extracted.wav")
        open(wav, "wb").write(b"\x00" * 2000)
        master = self._prepared_master([TranscriptSegment(start=0.0, end=2.0, text="hello there")], audio_path=wav)
        master.writer_agent.generate_script = MagicMock(side_effect=Halt())   # stop right after Phase 2/3
        with self.assertRaises(Halt):
            master.run_pipeline()
        self.assertEqual(master.state.audio_path, wav)


class TestVoiceClipManifest(_TempCwd):

    def setUp(self):
        super().setUp()
        self.vo_dir = os.path.join(self.work, "voiceover")
        os.makedirs(self.vo_dir)
        self.agent = VoiceAgent(voice="my-MM-ThihaNeural", output_dir=self.vo_dir, engine="edge_tts")
        self.script = [
            {"scene_id": "1", "narration": "ပထမစာကြောင်း", "gender": "male", "emotion": "normal"},
            {"scene_id": "2", "narration": "ဒုတိယစာကြောင်း", "gender": "male", "emotion": "normal"},
        ]
        for i in range(2):
            with open(os.path.join(self.vo_dir, f"scene_{i + 1:04d}.mp3"), "wb") as f:
                f.write(b"\x00" * 3000)

    def test_manifest_records_clips_and_full_match_is_reused_without_synthesis(self):
        self.agent._save_manifest(self.vo_dir, self.script)
        manifest = json.load(open(os.path.join(self.vo_dir, VOICE_MANIFEST_NAME)))["clips"]
        self.assertEqual(set(manifest), {"scene_0001.mp3", "scene_0002.mp3"})

        from brain.memory import MovieState
        state = MovieState(movie_name="clipx")
        state.generated_script = self.script
        with patch.object(self.agent, "_generate_edge_voiceover", side_effect=AssertionError("must reuse cache")):
            self.agent.generate_voiceover(state)

    def test_changed_text_only_invalidates_that_clip(self):
        self.agent._save_manifest(self.vo_dir, self.script)
        edited = [dict(self.script[0]), dict(self.script[1], narration="အသစ်ပြင်ထားသော စာကြောင်း")]
        self.agent._purge_stale_clips(self.vo_dir, edited)
        self.assertTrue(os.path.exists(os.path.join(self.vo_dir, "scene_0001.mp3")))
        self.assertFalse(os.path.exists(os.path.join(self.vo_dir, "scene_0002.mp3")))

    def test_legacy_folder_without_manifest_is_left_alone(self):
        self.agent._purge_stale_clips(self.vo_dir, self.script)
        self.assertTrue(os.path.exists(os.path.join(self.vo_dir, "scene_0001.mp3")))
        self.assertTrue(os.path.exists(os.path.join(self.vo_dir, "scene_0002.mp3")))

    def test_voice_change_changes_every_fingerprint(self):
        before = self.agent._clip_fingerprint(self.script[0])
        self.agent.voice = "my-MM-NilarNeural"
        self.assertNotEqual(before, self.agent._clip_fingerprint(self.script[0]))

    def test_f5_uses_character_clip_for_capitalised_speaker_names(self):
        ref = os.path.join(self.work, "john_ref.wav")
        open(ref, "wb").write(b"\x00" * 2000)
        agent = self.agent
        agent.engine = "f5_tts"
        agent.f5_cfg = {"auto_character_cloning": True, "default_ref_audio": os.path.join(self.work, "missing.wav")}
        agent.f5_engine = MagicMock()
        agent.f5_engine.generate_speech.return_value = True
        agent._extract_character_reference_clips = lambda state: {"john": {"audio_path": ref, "text": "hi"}}

        from brain.memory import MovieState
        state = MovieState(movie_name="f5clip")
        state.generated_script = [{"scene_id": "1", "narration": "Hello", "character": "John",
                                   "start_sec": 0.0, "end_sec": 2.0}]
        out_dir = os.path.join(self.work, "f5out")
        os.makedirs(out_dir)
        agent._generate_f5_voiceover(state, out_dir)
        kwargs = agent.f5_engine.generate_speech.call_args.kwargs
        self.assertEqual(kwargs["ref_audio_path"], ref)


class TestHardsubFixes(_TempCwd):

    def _engine(self):
        from hardsub_engine import HardsubEngine
        return HardsubEngine(output_base_dir=os.path.join(self.work, "outputs"))

    def test_project_name_cannot_escape_the_output_folder(self):
        engine = self._engine()

        class Stop(Exception):
            pass

        engine._ingest_video = MagicMock(side_effect=Stop())
        victim_parent = os.path.join(self.work, "escaped_here")
        for hostile in ("../escaped_here", "..\\..\\escaped_here", victim_parent):
            with self.assertRaises(Stop):
                engine.run(input_source="movie.mp4", project_name=hostile)
        self.assertFalse(os.path.exists(victim_parent))
        created = os.listdir(engine.output_base_dir)
        self.assertTrue(created)
        for name in created:
            self.assertNotIn("..", name)
            self.assertNotIn(os.sep, name)

    def test_no_cpu_rerender_after_stop(self):
        engine = self._engine()
        evt = threading.Event()
        engine.cancel_event = evt
        video = os.path.join(self.work, "in.mp4")
        ass = os.path.join(self.work, "sub.ass")
        for p in (video, ass):
            open(p, "w").write("x")
        render_calls = []

        def fake_run(cmd, *args, **kwargs):
            if "-filter_complex" in cmd:
                render_calls.append(cmd)
                evt.set()                                   # user hits Stop while the first render runs
            return MagicMock(returncode=1, stderr="killed", stdout="")

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(InterruptedError):
                engine._render_hardsub_video(
                    video_path=video, output_path=os.path.join(self.work, "out.mp4"), ass_path=ass,
                    blur_info=(0.82, 0.18, False), aspect_ratio="16:9", resolution="1080p",
                )
        self.assertEqual(len(render_calls), 1)              # the CPU fallback never started


class TestCookieDiscovery(_TempCwd):

    def test_shared_cookie_discovery_reaches_every_engine(self):
        from agents.downloader_agent import DownloaderAgent
        from hardsub_engine import HardsubEngine
        from subtitle_engine import SubtitleEngine

        cookie = os.path.join(self.work, "my_cookies.txt")
        open(cookie, "w").write("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n")
        with patch.dict(os.environ, {"MOVIE_COOKIES_PATH": cookie}):
            self.assertEqual(DownloaderAgent.find_cookies_file(), cookie)
            self.assertEqual(HardsubEngine(output_base_dir=self.work).cookies_path, cookie)
            self.assertEqual(SubtitleEngine(output_base_dir=self.work).cookies_path, cookie)
            explicit = os.path.join(self.work, "explicit.txt")
            self.assertEqual(HardsubEngine(output_base_dir=self.work, cookies_path=explicit).cookies_path, explicit)


class TestQuotaReservationIsAtomic(_TempCwd):

    def test_concurrent_reservations_never_exceed_the_limit(self):
        from brain.sqlite_store import reserve_sqlite_model_usage
        out_dir = os.path.join(self.work, "quota_outputs")
        limit = 60
        results = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                ok = reserve_sqlite_model_usage("AIzaSy...abcd", "gemini-x", limit, "2026-01-01", output_dir=out_dir)
                with lock:
                    results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]   # 160 attempts for 60 slots
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r), limit)


if __name__ == "__main__":
    unittest.main()
