from brain.diagnostics import log_suppressed
import os
import re
import json
import hashlib
import shutil
import asyncio
import subprocess
from brain.memory import MovieState
from agents.f5_tts_engine import F5TTSEngine

_CACHED_AVAILABLE_VOICES = None

# Per-project record of WHICH text/voice each scene_XXXX.mp3 was synthesized from.
# Lets a resume reuse only clips that still match the current script (and re-synthesize the rest).
VOICE_MANIFEST_NAME = "voice_manifest.json"
_KNOWN_EDGE_VOICES = {
    "my-mm-thihaneural",
    "my-mm-nilarneural",
    "en-us-guyneural",
    "en-us-jennyneural",
    "en-us-arianeural",
    "en-us-christopherneural",
    "en-us-ericneural",
    "zh-cn-xiaoxianeural",
    "zh-cn-yunjianneural",
}

class VoiceAgent:
    def __init__(self, voice: str = None, output_dir: str = "outputs", engine: str = None, tts_engine: str = None):
        """
        VoiceAgent supports:
          1. Microsoft Edge TTS (100% free cloud TTS - Thiha / Nilar / Guy)
          2. F5-TTS (Zero-Shot Flow-Matching Voice Cloning with Auto Character Slicing)
        """
        import brain.config as cfg
        config_data = cfg.load_config()
        voice_cfg = config_data.get("voice", {})
        
        self.engine = engine or tts_engine or os.getenv("TTS_ENGINE") or voice_cfg.get("engine", "edge_tts")
        self.tts_engine = self.engine
        self.voice = self._resolve_voice(voice, voice_cfg)
        self.rate_mm = os.getenv("EDGE_TTS_RATE_MM") or voice_cfg.get("tts_rate_mm", "+8%")
        self.rate_en = os.getenv("EDGE_TTS_RATE_EN") or voice_cfg.get("tts_rate_en", "+15%")
        self.output_dir = output_dir

        self.f5_cfg = voice_cfg.get("f5_tts", {})
        self.f5_engine = None
        if self.engine == "f5_tts":
            force_f5 = self.f5_cfg.get("force_engine", False) or self.f5_cfg.get("allow_burmese", False)
            is_burmese = str(self.voice).startswith("my-") or config_data.get("pipeline", {}).get("language", "").lower() in ("burmese", "mm", "myanmar")
            if is_burmese and not force_f5:
                print("[*] VoiceAgent: Burmese language detected with default F5-TTS model (pretrained for EN/ZH). Routing to Microsoft Neural Edge-TTS for authentic Myanmar narration.")
                print("    💡 Tip: To force F5-TTS anyway (e.g. with custom fine-tuned Myanmar weights), set \"force_engine\": true under \"voice.f5_tts\" in config.json.")
                self.engine = "edge_tts"
                self.tts_engine = "edge_tts"
            else:
                self.f5_engine = F5TTSEngine(
                    model_type=self.f5_cfg.get("model_type", "F5-TTS"),
                    device=self.f5_cfg.get("device", "auto"),
                    speed=float(self.f5_cfg.get("speed", 1.0)),
                    output_dir=os.path.join(self.output_dir, "f5_reference_clips")
                )
                if self.f5_engine.device == "cpu":
                    print("💡 [F5-TTS NOTICE] Running on CPU. For 50x faster zero-shot voice cloning, consider using Google Colab T4 GPU or Edge-TTS.")

        print(f"[*] VoiceAgent: Active TTS Engine: {self.engine.upper()} (Voice: {self.voice})")

    # ─────────────────────────────────────────────────────────────────────────
    # Clip cache integrity (voice clip cache ကို စာသားနှင့် တိုက်ဆိုင်စစ်ဆေးခြင်း)
    # ─────────────────────────────────────────────────────────────────────────
    def _clip_fingerprint(self, item: dict) -> str:
        """Stable hash of everything that determines how a clip sounds."""
        raw = "|".join([
            str(self.engine),
            str(self.voice),
            str(item.get("gender", "")).strip().lower(),
            str(item.get("emotion", "")).strip().lower(),
            str(item.get("narration", "")).strip(),
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _clip_name(idx: int) -> str:
        return f"scene_{(idx + 1):04d}.mp3"

    @staticmethod
    def _load_manifest(audio_out_dir: str):
        """Returns {clip_name: fingerprint}, or None for legacy folders that have no manifest."""
        path = os.path.join(audio_out_dir, VOICE_MANIFEST_NAME)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clips = data.get("clips") if isinstance(data, dict) else None
            return clips if isinstance(clips, dict) else {}
        except Exception:
            return {}

    def _save_manifest(self, audio_out_dir: str, script: list) -> None:
        """Records the fingerprint of every clip that exists on disk (atomic write)."""
        clips = {}
        for idx, item in enumerate(script or []):
            if not isinstance(item, dict) or not str(item.get("narration", "")).strip():
                continue
            path = os.path.join(audio_out_dir, self._clip_name(idx))
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                clips[self._clip_name(idx)] = self._clip_fingerprint(item)
        if not clips:
            return
        target = os.path.join(audio_out_dir, VOICE_MANIFEST_NAME)
        tmp = target + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"clips": clips}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, target)
        except OSError as e:
            print(f"[WARN] VoiceAgent: Could not write voice manifest: {e}")

    def _purge_stale_clips(self, audio_out_dir: str, script: list) -> None:
        """
        Deletes clips whose recorded fingerprint no longer matches the current script, so the
        per-clip resume logic re-synthesizes exactly those scenes (and only those).
        """
        manifest = self._load_manifest(audio_out_dir)
        if manifest is None:  # legacy folder: cannot verify, keep previous behaviour
            return
        purged = 0
        for idx, item in enumerate(script or []):
            if not isinstance(item, dict):
                continue
            path = os.path.join(audio_out_dir, self._clip_name(idx))
            if os.path.exists(path) and manifest.get(self._clip_name(idx)) != self._clip_fingerprint(item):
                try:
                    os.remove(path)
                    purged += 1
                except OSError as _exc:
                    log_suppressed("agents.voice_agent._purge_stale_clips", _exc)
        if purged:
            print(f"[*] VoiceAgent: {purged} cached clip(s) no longer match the script/voice -> re-synthesizing them.")

    def _resolve_voice(self, preferred_voice: str, voice_cfg: dict) -> str:
        """Prefer a configured voice but gracefully fall back to a known-good Edge TTS voice."""
        candidates = []
        for candidate in [
            preferred_voice,
            os.getenv("EDGE_TTS_VOICE"),
            voice_cfg.get("tts_voice_mm"),
            voice_cfg.get("tts_voice_en"),
            voice_cfg.get("tts_voice"),
            "my-MM-ThihaNeural",
            "en-US-GuyNeural",
        ]:
            value = str(candidate or "").strip()
            if value and value not in candidates:
                candidates.append(value)

        # 1. Instant check against known standard Edge voices (zero network delay)
        for candidate in candidates:
            if candidate.lower() in _KNOWN_EDGE_VOICES:
                return candidate

        # 2. Module-level cached check for custom/uncommon voices
        global _CACHED_AVAILABLE_VOICES
        if _CACHED_AVAILABLE_VOICES is None:
            try:
                import edge_tts
                import asyncio
                available_voices = []
                
                async def get_voices():
                    return await edge_tts.list_voices()
                
                try:
                    voices_list = asyncio.run(get_voices())
                except RuntimeError:
                    try:
                        import nest_asyncio; nest_asyncio.apply()
                        voices_list = asyncio.get_event_loop().run_until_complete(get_voices())
                    except Exception:
                        voices_list = []
                for item in voices_list:
                    if isinstance(item, dict):
                        available_voices.append(str(item.get("ShortName") or item.get("Name") or "").strip())
                    else:
                        available_voices.append(str(item).strip())
                _CACHED_AVAILABLE_VOICES = {v.lower() for v in available_voices if v}
            except Exception:
                _CACHED_AVAILABLE_VOICES = set()

        if _CACHED_AVAILABLE_VOICES:
            for candidate in candidates:
                if candidate.lower() in _CACHED_AVAILABLE_VOICES:
                    return candidate

        # 3. If offline or unlisted, use known-good default.
        for candidate in candidates:
            if candidate.lower().startswith("my-"):
                return candidate
        return "en-US-GuyNeural"

    def _extract_character_reference_clips(self, state: MovieState) -> dict:
        """
        Extracts 3-10s clean reference voice clips for each detected character from Demucs vocals.wav.
        Returns: {character_name: {"audio_path": ..., "text": ...}}
        """
        character_clips = {}
        movie_path = getattr(state, "movie_path", None) or getattr(state, "file_path", None)
        if not movie_path:
            return character_clips

        base_candidates = [
            getattr(state, "movie_name", None),
            os.path.splitext(os.path.basename(movie_path))[0],
        ]
        vocals_path = None
        for b_name in base_candidates:
            if not b_name:
                continue
            cand = os.path.join("temp", state.project_dir, "audio", "htdemucs", b_name, "vocals.wav")
            if os.path.exists(cand):
                vocals_path = cand
                break

        if not vocals_path or not os.path.exists(vocals_path):
            vocals_path = getattr(state, "audio_path", None)
            if not vocals_path or not os.path.exists(vocals_path):
                return character_clips

        ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE") or "ffmpeg"
        voices_temp_dir = os.path.join("temp", state.project_dir, "voices")
        os.makedirs(voices_temp_dir, exist_ok=True)

        speaker_segments = getattr(state, "speaker_transcript", []) or []
        if not speaker_segments and state.transcript:
            # Fallback to general transcript segments
            speaker_segments = [{"speaker": "Narrator", "text": getattr(s, "text", ""), "start_sec": getattr(s, "start", 0.0), "end_sec": getattr(s, "end", 0.0)} for s in state.transcript]

        # Group candidate segments per speaker
        by_speaker = {}
        for seg in speaker_segments:
            if not isinstance(seg, dict):
                continue
            spkr = str(seg.get("speaker", "Unknown")).strip()
            if not spkr or spkr.lower() == "unknown":
                spkr = "Narrator"
            start_s = float(seg.get("start_sec", 0.0))
            end_s = float(seg.get("end_sec", start_s))
            dur = end_s - start_s
            text = str(seg.get("text", "")).strip()

            if 2.5 <= dur <= 12.0 and text and len(text) > 8:
                if spkr not in by_speaker:
                    by_speaker[spkr] = []
                by_speaker[spkr].append({"start": start_s, "end": end_s, "dur": dur, "text": text})

        print(f"[*] VoiceAgent: Slicing character reference audio for {len(by_speaker)} speaker(s)...")

        for spkr, candidates in by_speaker.items():
            # Pick candidate closest to 5.0 seconds
            best = min(candidates, key=lambda c: abs(c["dur"] - 5.0))
            raw_spkr = str(spkr or "").strip()
            ascii_spkr = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_spkr).strip('_')
            spkr_hash = hashlib.md5(raw_spkr.encode('utf-8', errors='replace')).hexdigest()[:8]
            safe_name = f"{ascii_spkr[:16]}_{spkr_hash}" if ascii_spkr else f"spkr_{spkr_hash}"
            out_clip = os.path.join(voices_temp_dir, f"{safe_name}_ref.wav")

            cmd = [
                ffmpeg_bin, "-y",
                "-ss", str(best["start"]),
                "-to", str(best["end"]),
                "-i", vocals_path,
                "-acodec", "pcm_s16le",
                "-ar", "24000", # 24kHz optimal for F5-TTS
                "-ac", "1",
                out_clip
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                if os.path.exists(out_clip) and os.path.getsize(out_clip) > 1000:
                    character_clips[spkr.lower()] = {
                        "audio_path": out_clip,
                        "text": best["text"]
                    }
                    print(f"    - [OK] Extracted {spkr} voice ({best['dur']:.1f}s) -> '{best['text'][:40]}...'")
            except Exception as e:
                print(f"    - [WARN] Failed to slice reference for {spkr}: {e}")

        return character_clips

    def generate_voiceover(self, state: MovieState) -> MovieState:
        """Converts the generated narration script to MP3 audio files using Edge TTS or F5-TTS."""
        if not state.generated_script:
            print("[!] VoiceAgent: No script found. Skipping TTS generation.")
            return state

        if self.output_dir.endswith("voiceover"):
            audio_out_dir = self.output_dir
        elif state.project_dir in self.output_dir:
            audio_out_dir = os.path.join(self.output_dir, "voiceover")
        else:
            audio_out_dir = os.path.join(self.output_dir, state.project_dir, "voiceover")
        os.makedirs(audio_out_dir, exist_ok=True)

        # Drop clips that were synthesized from different text / voice, then check completeness.
        self._purge_stale_clips(audio_out_dir, state.generated_script)
        needed = [
            idx for idx, item in enumerate(state.generated_script)
            if isinstance(item, dict) and str(item.get("narration", "")).strip()
        ]
        valid_clips = [
            idx for idx in needed
            if os.path.exists(os.path.join(audio_out_dir, self._clip_name(idx)))
            and os.path.getsize(os.path.join(audio_out_dir, self._clip_name(idx))) > 1000
        ]
        existing_mp3s = [self._clip_name(idx) for idx in valid_clips]
        if needed and len(valid_clips) >= len(needed):
            print(f"[*] VoiceAgent: Reusing {len(existing_mp3s)} cached voiceover clips from {audio_out_dir}...")
            self._save_manifest(audio_out_dir, state.generated_script)
            return state

        # Granular Checkpoint Resume: Clean up only corrupted/empty mp3 clips (<= 1000 bytes).
        # Preserve valid scene_*.mp3 clips so subsequent loop can skip them!
        for old_file in os.listdir(audio_out_dir):
            if old_file.endswith(".mp3"):
                f_path = os.path.join(audio_out_dir, old_file)
                try:
                    if os.path.getsize(f_path) <= 1000:
                        os.remove(f_path)
                except Exception as _exc:
                    log_suppressed("agents.voice_agent.generate_voiceover", _exc)
        if existing_mp3s:
            print(f"[*] VoiceAgent: Found {len(existing_mp3s)} valid cached voiceover clips. Granular checkpoint resume active!")

        # Check if F5-TTS engine is requested
        use_f5 = self.engine == "f5_tts"
        if use_f5:
            force_f5 = self.f5_cfg.get("force_engine", False) or self.f5_cfg.get("allow_burmese", False)
            sample_text = "".join([str(item.get("narration", "")) for item in (state.generated_script or [])[:5]])
            burmese_chars = sum(1 for char in sample_text if ('\u1000' <= char <= '\u109F' or '\uAA60' <= char <= '\uAA7F'))
            is_majority_burmese = (len(sample_text) > 0 and (burmese_chars / len(sample_text) > 0.3)) or getattr(state, "language", "").lower() in ("burmese", "mm", "myanmar")
            if is_majority_burmese and not force_f5:
                print("[*] VoiceAgent: Burmese narration detected with default F5-TTS model.")
                print("[*] VoiceAgent: Automatically routing to Microsoft Neural Edge-TTS (my-MM-ThihaNeural / my-MM-NilarNeural) for natural Myanmar voiceover.")
                print("    💡 Tip: Set voice.f5_tts.force_engine=true in config.json to force custom F5-TTS.")
                use_f5 = False
            elif not self.f5_engine or not self.f5_engine.is_available():
                print("[WARN] VoiceAgent: F5-TTS is requested but not installed. Falling back to Edge TTS.")
                print("[TIP] To use F5-TTS Voice Cloning, run: pip install f5-tts soundfile")
                use_f5 = False

        if use_f5:
            result_state = self._generate_f5_voiceover(state, audio_out_dir)
        else:
            result_state = self._generate_edge_voiceover(state, audio_out_dir)
        self._save_manifest(audio_out_dir, state.generated_script)
        return result_state

    def _run_async(self, coro):
        """Safely executes an async coroutine, handling nested event loops in Jupyter / FastAPI."""
        try:
            return asyncio.run(coro)
        except RuntimeError as _loop_err:
            if "already running" in str(_loop_err).lower() or "event loop" in str(_loop_err).lower():
                try:
                    import nest_asyncio
                    nest_asyncio.apply()
                    return asyncio.get_event_loop().run_until_complete(coro)
                except Exception:
                    import concurrent.futures as _cf
                    def _run_in_new_loop():
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            return loop.run_until_complete(coro)
                        finally:
                            loop.close()
                    with _cf.ThreadPoolExecutor(max_workers=1) as _exe:
                        return _exe.submit(_run_in_new_loop).result()
            else:
                raise

    def _generate_f5_voiceover(self, state: MovieState, audio_out_dir: str) -> MovieState:
        """Generates voiceover using F5-TTS Zero-Shot Voice Cloning."""
        print(f"[*] VoiceAgent: Starting F5-TTS Voice Cloning (Model: {self.f5_cfg.get('model_type', 'F5-TTS')})...")
        
        # 1. Extract character reference clips from Demucs isolated vocals
        char_clips = {}
        if self.f5_cfg.get("auto_character_cloning", True):
            char_clips = self._extract_character_reference_clips(state)

        default_ref = self.f5_cfg.get("default_ref_audio", "assets/voices/default_ref.wav")
        default_ref_text = self.f5_cfg.get("default_ref_text", "")

        audio_files = []
        for idx, item in enumerate(state.generated_script):
            narration = item.get("narration", "").strip()
            if not narration:
                continue

            scene_id = item.get("scene_id", idx + 1)
            speaker = item.get("character", "Narrator")
            out_file = os.path.join(audio_out_dir, f"scene_{(idx+1):04d}.mp3")

            # NOTE: start_sec == 0.0 is a valid (first) scene - test for None, not truthiness.
            target_dur = None
            if item.get("end_sec") is not None and item.get("start_sec") is not None:
                target_dur = max(0.5, float(item["end_sec"]) - float(item["start_sec"]))

            # Granular Checkpoint Resume: Skip synthesis if this scene's mp3 already exists and is non-empty
            if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
                print(f"    ⏩ [Resume]: Block {idx+1} ({os.path.basename(out_file)}) already synthesized ({os.path.getsize(out_file)} bytes), skipping F5-TTS.")
                audio_files.append(out_file)
                continue

            clean_narration = self._prepare_tts_text(narration)

            # Determine reference audio for this speaker
            speaker_key = str(speaker).strip().lower()   # clips are stored under lower-cased names
            ref_audio = char_clips.get(speaker_key, {}).get("audio_path", default_ref)
            ref_text = char_clips.get(speaker_key, {}).get("text", default_ref_text)

            success = False
            if os.path.exists(ref_audio):
                dur_label = f"{target_dur:.1f}s" if target_dur is not None else "auto"
                print(f"[*] VoiceAgent (F5-TTS): Scene {scene_id} [Speaker: {speaker}] -> target_dur={dur_label}...")
                success = self.f5_engine.generate_speech(
                    text=clean_narration,
                    ref_audio_path=ref_audio,
                    ref_text=ref_text,
                    output_path=out_file,
                    target_dur=target_dur
                )

            # Fallback to Edge TTS if F5-TTS synthesis failed or ref audio was missing
            if not success:
                print(f"[!] VoiceAgent: F5-TTS fallback to Edge TTS for Scene {scene_id}...")
                is_myanmar = str(self.voice).startswith("my-")
                gender = str(item.get("gender", "Unknown")).strip().lower()
                voice_override = "my-MM-NilarNeural" if (is_myanmar and gender == "female") else self.voice
                rate = self.rate_mm if str(voice_override).startswith("my-") else self.rate_en
                emotion = item.get("emotion", "normal").lower()
                self._run_async(self._speak_with_retry(clean_narration, out_file, scene_id, emotion, rate=rate, target_dur=target_dur, voice_override=voice_override))

            if os.path.exists(out_file):
                audio_files.append(out_file)

        print(f"[OK] VoiceAgent: Generated {len(audio_files)} audio segments via F5-TTS in -> {audio_out_dir}")
        return state

    def _generate_edge_voiceover(self, state: MovieState, audio_out_dir: str) -> MovieState:
        """Converts the generated narration script to MP3 audio files using Edge TTS ($0 cost)."""
        selected_rate = self.rate_mm if str(self.voice).startswith("my-") else self.rate_en
        print(f"[*] VoiceAgent: Starting Text-to-Speech using Edge TTS (Voice: {self.voice}, Pace: {selected_rate})...")

        try:
            import edge_tts
            del edge_tts
        except ImportError:
            print("[!] VoiceAgent: 'edge-tts' is not installed. Please run: pip install edge-tts")
            return state

        audio_files = []
        
        async def _run_all(tasks):
            sem = asyncio.Semaphore(5)
            async def _wrap(task):
                async with sem:
                    return await task
            return await asyncio.gather(*[_wrap(t) for t in tasks], return_exceptions=True)
            
        tasks = []
        output_files = []

        for idx, item in enumerate(state.generated_script):
            narration = item.get("narration", "").strip()
            emotion = item.get("emotion", "normal").lower()
            gender = str(item.get("gender", "male")).strip().lower()
            character = str(item.get("character", "Narrator")).strip()
            scene_id = item.get("scene_id", idx + 1)
            if not narration:
                continue
            
            is_myanmar = str(self.voice).startswith("my-") or getattr(self, "language", "") == "burmese"
            voice_override = self.voice
            if is_myanmar:
                if self.voice == "my-MM-NilarNeural":
                    voice_override = "my-MM-NilarNeural"
                elif gender == "female":
                    voice_override = "my-MM-NilarNeural"
                    print(f"    👩 [Multi-Voice]: Block {idx+1} ({character}) -> Female Voice (my-MM-NilarNeural)")
                else:
                    voice_override = "my-MM-ThihaNeural"
            else:
                if self.voice == "en-US-JennyNeural":
                    voice_override = "en-US-JennyNeural"
                elif gender == "female":
                    voice_override = "en-US-JennyNeural"
                    print(f"    👩 [Multi-Voice]: Block {idx+1} ({character}) -> Female Voice (en-US-JennyNeural)")
                else:
                    voice_override = "en-US-GuyNeural"
            
            selected_rate = self.rate_mm if str(voice_override).startswith("my-") else self.rate_en
            
            # GET EXACT TIMESTAMPS
            start_sec = float(item.get("start_sec") or 0.0)
            end_sec = float(item.get("end_sec") or (start_sec + 3.0))
            target_dur = max(0.5, end_sec - start_sec)
            
            clean_narration = self._prepare_tts_text(narration)
            out_file = os.path.join(audio_out_dir, f"scene_{(idx+1):04d}.mp3")

            # Granular Checkpoint Resume: If clip already synthesized and valid (>1000 bytes), skip TTS
            if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
                print(f"    ⏩ [Resume]: Block {idx+1} ({os.path.basename(out_file)}) already synthesized ({os.path.getsize(out_file)} bytes), skipping Edge-TTS.")
                audio_files.append(out_file)
                continue

            print(f"[*] VoiceAgent: Generating audio block {idx+1} (Scene {scene_id}) voice={voice_override} target_dur={target_dur:.1f}s...")
            
            tasks.append(self._speak_with_retry(clean_narration, out_file, scene_id, emotion, rate=selected_rate, target_dur=target_dur, voice_override=voice_override))
            output_files.append(out_file)

        if tasks:
            results = self._run_async(_run_all(tasks))
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"[WARN] VoiceAgent: Block {i} TTS failed: {result}")
                elif result:
                    audio_files.append(output_files[i])
        else:
            print(f"[*] VoiceAgent: All {len(audio_files)} audio blocks were already synthesized. Resuming without re-generating.")

        audio_files.sort()
        print(f"[OK] VoiceAgent: Generated/reused {len(audio_files)} audio segments in -> {audio_out_dir}")
        return state

    async def _speak_with_retry(self, text: str, output_file: str, scene_id, emotion: str = "normal", rate: str = "+15%", target_dur: float = None, voice_override: str = None) -> bool:
        """Async helper to synthesize a single narration chunk via Edge TTS with retry logic and emotion tuning."""
        import edge_tts
        import subprocess
        try:
            from moviepy import AudioFileClip
        except ImportError:
            from moviepy.editor import AudioFileClip

        # Emotion-driven dynamic voice modulation
        pitch = "+2Hz"
        volume = "+30%"
        em_lower = str(emotion).lower().strip()
        if em_lower in ("excited", "angry", "intense", "scared", "urgent"):
            pitch = "+5Hz"
            volume = "+35%"
        elif em_lower in ("sad", "whisper", "somber", "melancholy"):
            pitch = "-2Hz"
            volume = "+20%"

        success = False
        voice_to_use = voice_override if voice_override else self.voice
        for attempt in range(1, 4):
            try:
                communicate = edge_tts.Communicate(text, voice_to_use, rate=rate, pitch=pitch, volume=volume)
                await communicate.save(output_file)
                success = True
                break
            except Exception:
                print(f"[RETRY] VoiceAgent: Retrying Scene {scene_id} (attempt {attempt}/3)...")
                await asyncio.sleep(2 ** attempt)
        
        if not success:
            print(f"[WARN] VoiceAgent: Scene {scene_id} TTS failed after 3 attempts.")
            print(f"[*] VoiceAgent: FALLBACK -> Generating silent audio of {target_dur or 3.0}s to preserve video sync.")
            try:
                dur = target_dur if target_dur else 3.0
                ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE") or "ffmpeg"
                cmd = [
                    ffmpeg_bin, "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", str(dur),
                    "-codec:a", "libmp3lame",
                    output_file
                ]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0 and os.path.exists(output_file):
                    success = True
                else:
                    try:
                        import soundfile as sf
                        import numpy as np
                        sf.write(output_file, np.zeros(int(dur * 44100), dtype=np.float32), 44100)
                        success = True
                    except Exception:
                        try:
                            from moviepy.audio.AudioClip import AudioClip
                        except ImportError:
                            from moviepy.editor import AudioClip
                        make_frame = lambda t: [0, 0]
                        silent_clip = AudioClip(make_frame, duration=dur, fps=44100)
                        silent_clip.write_audiofile(output_file, fps=44100, logger=None)
                        success = True
            except Exception as e:
                print(f"[ERROR] VoiceAgent: Fallback silent audio failed: {e}")
                return False

        # --- EXACT SYNC: Pitch-Preserving FFmpeg atempo Stretch ---
        if target_dur is not None and os.path.exists(output_file):
            try:
                raw_dur = 0.0
                try:
                    import soundfile as sf
                    raw_dur = float(sf.info(output_file).duration)
                except Exception:
                    try:
                        import wave
                        with wave.open(output_file, 'rb') as wf:
                            raw_dur = float(wf.getnframes()) / float(wf.getframerate())
                    except Exception:
                        try:
                            clip = AudioFileClip(output_file)
                            raw_dur = clip.duration
                            clip.close()
                        except Exception:
                            raw_dur = 0.0

                if raw_dur > 0:
                    stretch_ratio = target_dur / raw_dur
                    effective_ratio = min(1.15, max(0.78, stretch_ratio))

                    if abs(effective_ratio - 1.0) > 0.05:
                        base, ext = os.path.splitext(output_file)
                        temp_file = base + "_temp" + ext
                        os.replace(output_file, temp_file)
                        
                        atempo_val = 1.0 / effective_ratio
                        if atempo_val > 2.0:
                            filter_str = f"atempo=2.0,atempo={atempo_val/2.0:.3f}"
                        elif atempo_val < 0.5:
                            filter_str = f"atempo=0.5,atempo={atempo_val/0.5:.3f}"
                        else:
                            filter_str = f"atempo={atempo_val:.3f}"
                        
                        ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
                        if not ffmpeg_bin or not os.path.exists(ffmpeg_bin):
                            try:
                                from imageio_ffmpeg import get_ffmpeg_exe
                                ffmpeg_bin = get_ffmpeg_exe()
                            except Exception:
                                ffmpeg_bin = "ffmpeg"
                        cmd = [
                            ffmpeg_bin, "-y", "-i", temp_file,
                            "-filter:a", filter_str,
                            output_file
                        ]
                        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                        if os.path.exists(temp_file):
                            try: os.remove(temp_file)
                            except Exception: pass
                        print(f"[*] VoiceAgent (Exact Sync): Scene {scene_id} stretched (raw: {raw_dur:.1f}s -> target: {target_dur:.1f}s | filter: {filter_str})") 
            except Exception as stretch_err:
                print(f"[WARN] VoiceAgent: FFmpeg atempo stretch failed for Scene {scene_id}: {stretch_err}")
                base, ext = os.path.splitext(output_file)
                temp_fallback = base + "_temp" + ext
                if not os.path.exists(output_file) and os.path.exists(temp_fallback):
                    os.replace(temp_fallback, output_file)

        return True

    def _prepare_tts_text(self, text: str) -> str:
        """Normalize script text so Edge TTS / F5-TTS reads narration naturally."""
        is_burmese = str(self.voice).startswith("my-") or getattr(self, "language", "") == "burmese"
        if is_burmese:
            try:
                from brain.burmese_utils import (
                    replace_numbers_with_burmese,
                    transliterate_english_acronyms,
                    convert_to_tts_phonetic_burmese,
                )
                text = replace_numbers_with_burmese(text)
                text = transliterate_english_acronyms(text)
                text = convert_to_tts_phonetic_burmese(text)
            except Exception as e:
                print(f"[WARN] VoiceAgent: Failed to normalize text with burmese_utils: {e}")

        text = re.sub(r'[\*#_~`]', '', str(text))
        if is_burmese:
            text = text.replace("…", "။")
        else:
            text = text.replace("…", "...")
        
        # English letter handling for Myanmar TTS
        if str(self.voice).startswith("my-"):
            # First pass: transliterate acronyms and common movie character names
            try:
                from brain.burmese_utils import transliterate_english_acronyms
                text = transliterate_english_acronyms(text)
            except Exception as _exc:
                log_suppressed("agents.voice_agent._prepare_tts_text", _exc)
            # Only strip remaining stray single latin characters if Burmese letters exist
            # Avoids leaving awkward blank gaps in sentences
            stripped = re.sub(r'\b[A-Za-z]{1,2}\b', '', text)
            stripped = re.sub(r'\s+', ' ', stripped).strip()
            if stripped and re.search(r'[\u1000-\u109F]', stripped):
                text = stripped

        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            return ""

        if is_burmese:
            text = re.sub(r'\s*([,;:])\s*', r'၊ ', text)
            text = re.sub(r'\s*([!?])\s*', r'။ ', text)
            text = re.sub(r'(?<!\d)\.(?!\d)', '။ ', text)
            text = re.sub(r'\s*\.\s*$', r'။', text)
            parts = [p.strip() for p in re.split(r'(?<=[။!?])\s+', text) if self._has_meaningful_text(p)]
        else:
            text = re.sub(r'\s*([,;:])\s*', r', ', text)
            text = re.sub(r'\s*([!?])\s*', r'\1 ', text)
            parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+', text) if p.strip()]

        if not parts:
            parts = [text]

        normalized_parts = []
        for part in parts:
            if len(part) <= 120:
                normalized_parts.append(part)
                continue

            split_pattern = r'\s*([၊])\s*' if is_burmese else r'\s*([,])\s*'
            clauses = [c.strip() for c in re.split(split_pattern, part) if c and c.strip()]
            if len(clauses) > 1:
                rebuilt = []
                current = ""
                comma_char = "၊" if is_burmese else ","
                for clause in clauses:
                    if clause == comma_char:
                        current = current.rstrip() + comma_char
                        continue
                    candidate = f"{current} {clause}".strip() if current else clause
                    if len(candidate) > 80 and current:
                        rebuilt.extend(self._chunk_text(current.strip(), 70))
                        current = clause
                    else:
                        current = candidate
                if current:
                    rebuilt.extend(self._chunk_text(current.strip(), 70))
                normalized_parts.extend(rebuilt)
            else:
                normalized_parts.extend(self._chunk_text(part, 70))

        text = "\n".join(normalized_parts)
        if is_burmese:
            if text and text[-1] not in "။!?":
                text += "။"
        else:
            if text and text[-1] not in ".!?":
                text += "."
        return text

    def _chunk_text(self, text: str, chunk_size: int) -> list[str]:
        """Break a long clause into short, speakable chunks.

        FIX-BUG5: Splits at word boundary (space) or Burmese punctuation (၊ ။)
        nearest to the chunk_size limit. If no space exists, splits at clean
        Burmese syllable boundaries so syllables are never cut mid-character.
        """
        text = text.strip()
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        # Burmese combining marks, medials, tone marks, virama that CANNOT start a chunk
        BURMESE_NON_INITIALS = set(
            [chr(c) for c in range(0x102B, 0x103F)] +
            ['\uAA7B', '\uAA7C', '\uAA7D']
        )

        chunks = []
        start = 0
        while start < len(text):
            if len(text) - start <= chunk_size:
                piece = text[start:].strip()
                if piece:
                    chunks.append(piece)
                break

            end = start + chunk_size

            # Priority 1: Search backwards for space or punctuation
            cut = -1
            for i in range(end, start + 5, -1):
                if text[i - 1] in (' ', '\u104A', '\u104B', ',', '.', '!', '?', ';', ':'):
                    cut = i
                    break

            # Priority 2: Search backwards for a clean Burmese syllable boundary
            # The character starting the next chunk must NOT be a combining mark,
            # and the character ending the current chunk must not be a prefix vowel or virama.
            if cut == -1:
                for i in range(end, start + 5, -1):
                    if text[i] not in BURMESE_NON_INITIALS and text[i - 1] not in ('\u1031', '\u1039'):
                        cut = i
                        break

            if cut == -1:
                cut = end

            piece = text[start:cut].strip()
            if piece:
                if piece[-1] not in ('၊', '။', '!', '?', '.', ',', ' '):
                    piece += '၊'
                chunks.append(piece)
            start = cut

        return chunks

    def _has_meaningful_text(self, text: str) -> bool:
        """Return True when a fragment contains real text, not just punctuation."""
        if not text:
            return False
        return bool(re.search(r'[A-Za-z0-9\u1000-\u109F]', str(text)))
