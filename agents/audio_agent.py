from brain.diagnostics import log_suppressed
import os
from brain.memory import MovieState, TranscriptSegment
from core.whisper_settings import resolve_whisper_model, resolve_beam_size

class AudioAgent:
    def __init__(self, movie_path: str):
        self.movie_path = movie_path

    def extract_audio(self, state: MovieState, output_dir: str, skip_demucs: bool = None) -> MovieState:
        """Extracts audio from video file into a standalone WAV file using MoviePy or FFmpeg fallback."""
        print(f"[*] AudioAgent: Extracting audio from {self.movie_path}...")
        os.makedirs(output_dir, exist_ok=True)
        audio_filename = f"{state.movie_name}.wav"
        audio_path = os.path.join(output_dir, audio_filename)

        def _wav_ready() -> bool:
            return os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000

        if _wav_ready():
            # Extracted on a previous run: reuse the WAV but still go through vocal separation below
            # (it has its own cache) so a resumed run feeds Whisper the same audio as the first run.
            print(f"[*] AudioAgent: Audio already exists, reusing -> {audio_path}")
            state.audio_path = audio_path
        else:
            # Direct FFmpeg extraction first (instant C-speed, zero RAM overhead)
            self._ffmpeg_extract_audio(audio_path)
            if _wav_ready():
                state.audio_path = audio_path
                print(f"[*] AudioAgent: Audio extracted via FFmpeg -> {audio_path}")
            else:
                # Fallback to MoviePy if FFmpeg direct extraction failed
                try:
                    try:
                        from moviepy.editor import VideoFileClip
                    except ImportError:
                        from moviepy import VideoFileClip

                    video = VideoFileClip(self.movie_path)
                    if video.audio is not None:
                        video.audio.write_audiofile(audio_path, logger=None)
                        state.audio_path = audio_path
                        print(f"[*] AudioAgent: Audio extracted via MoviePy fallback -> {audio_path}")
                    else:
                        print("[!] AudioAgent: No audio stream detected in the video.")
                    video.close()
                except Exception as e:
                    print(f"[!] AudioAgent: MoviePy fallback failed ({e}). Transcription will be skipped.")

        if getattr(state, 'audio_path', None):
            should_skip = skip_demucs if skip_demucs is not None else getattr(state, 'skip_demucs', None)
            state.audio_path = self.separate_vocals(state.audio_path, output_dir, skip_demucs=should_skip)

        return state

    def separate_vocals(self, audio_path: str, output_dir: str, skip_demucs: bool = None) -> str:
        """Separates vocals from background music using Demucs to improve Whisper accuracy."""
        import subprocess, shutil, sys

        import brain.config as cfg
        config_data = cfg.load_config()
        use_demucs_cfg = config_data.get("pipeline", {}).get("use_demucs", True)
        if skip_demucs is True or (skip_demucs is None and (os.getenv("SKIP_DEMUCS") == "true" or not use_demucs_cfg)):
            print("[*] AudioAgent: Skipping vocal separation (Demucs disabled) -> using direct audio for Whisper.")
            return audio_path

        # CPU Bottleneck Guard: Demucs on CPU takes 20-40 mins per video. Auto-skip on CPU unless CUDA or explicitly forced.
        try:
            import torch
            has_cuda = torch.cuda.is_available()
        except Exception:
            has_cuda = False

        force_cpu = config_data.get("pipeline", {}).get("force_cpu_demucs", False) or os.getenv("FORCE_DEMUCS") == "true"
        if not has_cuda and not force_cpu:
            print("[*] AudioAgent: No CUDA GPU detected (CPU mode). Skipping Demucs to save 20-40 mins of CPU processing.")
            print("    💡 Tip: Demucs requires an NVIDIA GPU for fast separation. Whisper will transcribe original audio directly.")
            return audio_path

        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        cached_vocals = os.path.join(output_dir, "htdemucs", base_name, "vocals.wav")
        cached_no_vocals = os.path.join(output_dir, "htdemucs", base_name, "no_vocals.wav")
        if os.path.exists(cached_vocals) and os.path.exists(cached_no_vocals) and os.path.getsize(cached_vocals) > 1000:
            print(f"[*] AudioAgent: Reusing existing Demucs separated audio -> {cached_vocals}")
            return cached_vocals
        
        demucs_cmd = None
        if shutil.which("demucs"):
            demucs_cmd = ["demucs"]
        else:
            # Fallback to python module execution (for virtualenv without global PATH)
            for mod in ["demucs.separate", "demucs"]:
                try:
                    r = subprocess.run([sys.executable, "-m", mod, "--help"], capture_output=True, timeout=15)
                    if r.returncode == 0:
                        demucs_cmd = [sys.executable, "-m", mod]
                        break
                except Exception:
                    continue

        if not demucs_cmd:
            print("[!] AudioAgent: 'demucs' not found in PATH or environment. Skipping vocal separation.")
            return audio_path
            
        try:
            import torch
            cpu_jobs = str(max(1, (os.cpu_count() or 4) - 1))
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                print(f"[*] AudioAgent (Demucs): 🚀 Separating vocals via NVIDIA GPU ({gpu_name}) [CUDA Active]...")
                device_flag = ["-d", "cuda"]
            else:
                print(f"[*] AudioAgent (Demucs): 💻 Separating vocals via CPU Multi-Core ({cpu_jobs} worker threads)...")
                device_flag = ["-d", "cpu", "-j", cpu_jobs]
            cmd = [*demucs_cmd, "--two-stems=vocals", "-n", "htdemucs", *device_flag, audio_path, "-o", output_dir]
            subprocess.run(cmd, check=True)
            
            base_name = os.path.splitext(os.path.basename(audio_path))[0]
            vocals_path = os.path.join(output_dir, "htdemucs", base_name, "vocals.wav")
            if os.path.exists(vocals_path):
                print(f"[*] AudioAgent: Vocal separation successful -> {vocals_path}")
                return vocals_path
        except Exception as e:
            print(f"[!] AudioAgent: Demucs vocal separation failed: {e}. Falling back to original audio.")
            
        return audio_path

    def _ffmpeg_extract_audio(self, audio_path: str):
        """Direct FFmpeg fallback — works even if MoviePy is not configured correctly."""
        import subprocess, shutil
        ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
        if not ffmpeg_bin:
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                ffmpeg_bin = get_ffmpeg_exe()
            except Exception:
                ffmpeg_bin = None
        if not ffmpeg_bin:
            print("[!] AudioAgent: ffmpeg not found in PATH.")
            return None
        try:
            cmd = [
                ffmpeg_bin, "-y", "-i", self.movie_path,
                "-vn",                      # no video
                "-acodec", "pcm_s16le",     # WAV format
                "-ar", "44100",             # 44.1kHz — required for high quality Demucs separation
                "-ac", "2",                 # Stereo — required for Demucs
                audio_path
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                encoding="utf-8", errors="replace"
            )
            if result.returncode == 0 and os.path.exists(audio_path):
                return audio_path
            print(f"[!] AudioAgent FFmpeg stderr: {result.stderr[-300:]}")
            return None
        except Exception as e:
            print(f"[!] AudioAgent FFmpeg exception: {e}")
            return None

    def _parse_srt_file(self, srt_path: str):
        """Parses an SRT or VTT subtitle file into a list of TranscriptSegment."""
        import re
        if not os.path.exists(srt_path):
            return []

        content = ""
        for encoding in ["utf-8-sig", "utf-8", "gb18030", "cp1252", "latin-1"]:
            try:
                with open(srt_path, "r", encoding=encoding) as f:
                    content = f.read()
                break
            except Exception:
                continue
        if not content:
            return []

        # Matches: 00:01:23,456 --> 00:01:25,789 or 01:23.456 --> 01:25.789
        time_pattern = re.compile(
            r'(?:(\d{1,2}):)?(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[,.](\d{3})'
        )

        segments = []
        blocks = re.split(r'\n\s*\n', content.strip())
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            time_match = None
            text_lines = []
            for line in lines:
                m = time_pattern.search(line)
                if m:
                    time_match = m
                elif not time_match:
                    continue
                else:
                    cleaned_line = re.sub(r'<[^>]+>', '', line).strip()
                    if cleaned_line:
                        text_lines.append(cleaned_line)

            if time_match and text_lines:
                g = time_match.groups()
                h1, m1, s1, ms1 = int(g[0] or 0), int(g[1]), int(g[2]), int(g[3])
                h2, m2, s2, ms2 = int(g[4] or 0), int(g[5]), int(g[6]), int(g[7])
                start_sec = round(h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0, 2)
                end_sec = round(h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0, 2)
                full_text = " ".join(text_lines)
                segments.append(TranscriptSegment(start=start_sec, end=end_sec, text=full_text))

        return segments

    def transcribe_audio(self, state: MovieState, model_size: str = "small") -> MovieState:
        """
        Transcribes audio via faster-whisper running in a child subprocess.
        This isolates any native-library crash (e.g. ctranslate2 on Python 3.14)
        so the main pipeline always continues even if Whisper fails.
        """
        # 1. First check if external subtitle file exists (.srt / .vtt) to bypass Whisper
        movie_base, _ = os.path.splitext(self.movie_path) if self.movie_path else ("", "")
        movie_dir = os.path.dirname(os.path.abspath(self.movie_path)) if self.movie_path else ""
        candidate_subs = [
            f"{movie_base}.srt",
            f"{movie_base}.vtt",
            os.path.join(movie_dir, "subtitles.srt"),
            os.path.join(movie_dir, "subtitle.srt"),
            os.path.join(movie_dir, "movie.srt"),
            os.path.join(movie_dir, f"{state.movie_name}.srt"),
        ]
        for sub_file in candidate_subs:
            if sub_file and os.path.exists(sub_file):
                parsed = self._parse_srt_file(sub_file)
                if parsed:
                    print(f"[*] AudioAgent: 🎯 Found external subtitle ({len(parsed)} segments) -> {sub_file}. Bypassing Whisper STT!")
                    state.transcript = parsed
                    return state

        if not state.audio_path or not os.path.exists(state.audio_path):
            print("[!] AudioAgent: No audio file found for transcription. Skipping STT.")
            return state

        source_lang = getattr(state, "source_language", "auto") or "auto"
        model_size = resolve_whisper_model(model_size)   # "auto" -> medium on GPU / base on CPU
        print(f"[*] AudioAgent: Starting local Speech-to-Text (Whisper model: {model_size}, source_lang: {source_lang})...")

        import subprocess, sys, json, tempfile

        # Find Python interpreter with faster-whisper available
        def _find_python312():
            # 1. First test if the current active Python interpreter has faster_whisper
            try:
                r = subprocess.run([sys.executable, "-c", "import faster_whisper"], capture_output=True, timeout=10)
                if r.returncode == 0:
                    return sys.executable
            except Exception as _exc:
                log_suppressed("agents.audio_agent._find_python312", _exc)

            # 2. If current interpreter is Python 3.10-3.12, use it
            if sys.version_info[:2] in [(3, 10), (3, 11), (3, 12)]:
                return sys.executable

            try:
                r = subprocess.run(
                    ["py", "-3.12", "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace"
                )
                if r.returncode == 0 and os.path.exists(r.stdout.strip()):
                    return r.stdout.strip()
            except Exception as _exc:
                log_suppressed("agents.audio_agent._find_python312", _exc)

            for candidate in [
                r"C:\Python312\python.exe",
                r"C:\Users\\" + os.environ.get("USERNAME", "") + r"\AppData\Local\Programs\Python\Python312\python.exe",
            ]:
                if os.path.exists(candidate):
                    return candidate
            return sys.executable

        python_exe = _find_python312()
        print(f"[*] AudioAgent: Using Python interpreter -> {python_exe}")
        if sys.version_info[:2] < (3, 10):
            print("[WARN] AudioAgent: Python 3.10+ recommended for best Whisper compatibility.")

        # Write a self-contained transcription helper script to a temp file
        helper_script = """
import os, sys, json
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

try:
    from imageio_ffmpeg import get_ffmpeg_exe
    _ff_dir = os.path.dirname(get_ffmpeg_exe())
    if _ff_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ff_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

audio_path  = sys.argv[1]
model_size  = sys.argv[2]
output_path = sys.argv[3]
movie_name  = sys.argv[4]
source_lang = sys.argv[5] if len(sys.argv) > 5 else "auto"
beam_size   = int(sys.argv[6]) if len(sys.argv) > 6 else 1
whisper_lang = None if source_lang in ["auto", "", None] else source_lang

results = []
detected_lang = whisper_lang or "en"

# Try 1: Faster-Whisper (CUDA / INT8)
try:
    from faster_whisper import WhisperModel
    import torch
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        device = "cuda"
        compute_type = "float16"
        print(f"[*] AudioAgent (Faster-Whisper): 🚀 Active Hardware -> NVIDIA GPU ({gpu_name}) [CUDA float16 Tensor Cores]")
    else:
        device = "cpu"
        compute_type = "int8"
        print("[*] AudioAgent (Faster-Whisper): 💻 Active Hardware -> CPU Multi-Core [INT8 Quantized Mode]")

    num_threads = min(8, os.cpu_count() or 4)
    model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=num_threads)
    segments, info = model.transcribe(
        audio_path,
        language=whisper_lang,
        beam_size=beam_size,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        initial_prompt=f"This is a dialogue transcript for the movie {movie_name}."
    )
    detected_lang = whisper_lang or info.language
    for seg in segments:
        results.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})

except Exception as fw_err:
    print(f"[*] AudioAgent: Falling back to standard Whisper engine ({fw_err})...")
    import whisper
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] AudioAgent (Whisper): Running on device: {device}")
    model = whisper.load_model(model_size, device=device)
    use_fp16 = bool(torch.cuda.is_available())
    res = model.transcribe(
        audio_path,
        language=whisper_lang,
        fp16=use_fp16,
        initial_prompt=f"This is a dialogue transcript for the movie {movie_name}."
    )
    detected_lang = whisper_lang or res.get("language", "en")
    for seg in res.get("segments", []):
        results.append({"start": round(float(seg["start"]), 2), "end": round(float(seg["end"]), 2), "text": seg["text"].strip()})

with open(output_path, "w", encoding="utf-8") as f:
    json.dump({"language": detected_lang, "segments": results}, f, ensure_ascii=False)
print(f"[Whisper] Transcribed {len(results)} segments in language: {detected_lang}")
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(helper_script)
            helper_path = tmp.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as out_tmp:
            result_path = out_tmp.name

        whisper_timeout = int(os.getenv("WHISPER_TIMEOUT", "1800"))
        try:
            import brain.config as _cfg
            whisper_beam = resolve_beam_size(_cfg.load_config().get("pipeline", {}))
        except Exception:
            whisper_beam = 1
        try:
            proc = subprocess.run(
                [python_exe, helper_path, state.audio_path, model_size, result_path, state.movie_name, source_lang, str(whisper_beam)],
                capture_output=True, text=True, timeout=whisper_timeout,
                encoding="utf-8", errors="replace"
            )
            if proc.stdout:
                print(f"[*] AudioAgent: {proc.stdout.strip()}")

            if proc.returncode == 0 and os.path.exists(result_path):
                with open(result_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                transcript_list = [
                    TranscriptSegment(start=s["start"], end=s["end"], text=s["text"])
                    for s in data.get("segments", [])
                ]
                state.transcript = transcript_list
                print(f"[*] AudioAgent completed: Transcribed {len(transcript_list)} dialogue segments "
                      f"(lang: {data.get('language','?')}).")
            else:
                err = (proc.stderr or "")[-400:]
                print(f"[WARN] AudioAgent: Whisper subprocess failed (exit {proc.returncode}): {err}. The pipeline will stop: no transcript, nothing to translate.")
                state.transcript = []
        except subprocess.TimeoutExpired:
            print(f"[WARN] AudioAgent: Whisper timed out after {whisper_timeout}s (raise WHISPER_TIMEOUT or use a smaller model). The pipeline will stop: no transcript.")
            state.transcript = []
        except Exception as e:
            print(f"[WARN] AudioAgent: Unexpected error during transcription: {e}. The pipeline will stop: no transcript.")
            state.transcript = []
        finally:
            for f in [helper_path, result_path]:
                try:
                    os.unlink(f)
                except Exception:
                    pass

        return state

    def correct_transcript(self, state: MovieState) -> MovieState:
        """Uses LLM to correct spelling and character names in the transcript without truncating dialogue."""
        if not state.transcript:
            return state

        print(f"[*] AudioAgent: Running LLM error correction on {len(state.transcript)} transcript segments...")
        try:
            from brain.gemini_client import call_gemini
            from brain import config as cfg
            import json, re
            config_data = cfg.load_config()
            gemini_cfg = config_data.get("gemini", {})
            if not gemini_cfg.get("enabled", False):
                return state
                
            api_key = gemini_cfg.get("api_keys") or os.getenv("GEMINI_API_KEY")
            if not api_key:
                return state
                
            model = gemini_cfg.get("model", "gemini-3.5-flash-lite")
            CHUNK_SIZE = 80
            total_segments = len(state.transcript)
            all_corrected_segments = []

            for start_idx in range(0, total_segments, CHUNK_SIZE):
                chunk = state.transcript[start_idx:start_idx + CHUNK_SIZE]
                chunk_text = "\n".join([f"[{s.start:.2f}-{s.end:.2f}] {s.text}" for s in chunk])

                sys_prompt = "You are an AI assistant that corrects movie transcripts."
                user_prompt = (
                    f"Correct obvious spelling errors and ensure character names are spelled correctly for the movie '{state.movie_name}'. "
                    f"Return ONLY valid JSON as an array of objects. Each object must have keys "
                    f"'start' (number), 'end' (number), and 'text' (string). "
                    f"Keep the original timestamps intact and do not add markdown blocks.\n\n"
                    f"{chunk_text}"
                )

                chunk_corrected = []
                try:
                    raw, _ = call_gemini(sys_prompt, user_prompt, api_key, model=model, temperature=0.1)
                    cleaned = raw.strip().strip("`")
                    if cleaned.lower().startswith("json"):
                        cleaned = cleaned[4:].strip()

                    try:
                        parsed = json.loads(cleaned)
                        if isinstance(parsed, dict):
                            parsed = parsed.get("segments", [])
                        if isinstance(parsed, list):
                            for item in parsed:
                                if not isinstance(item, dict):
                                    continue
                                try:
                                    chunk_corrected.append(
                                        TranscriptSegment(
                                            start=float(item["start"]),
                                            end=float(item["end"]),
                                            text=str(item["text"]).strip(),
                                        )
                                    )
                                except Exception:
                                    continue
                    except Exception:
                        for line in raw.split("\n"):
                            m = re.match(r'\[([\d.]+)-([\d.]+)\]\s*(.*)', line.strip())
                            if m:
                                chunk_corrected.append(TranscriptSegment(start=float(m.group(1)), end=float(m.group(2)), text=m.group(3).strip()))
                except Exception as chunk_err:
                    print(f"    [!] AudioAgent: Batch correction error for segments {start_idx+1}-{start_idx+len(chunk)}: {chunk_err}")

                # If LLM correction succeeded and has segments, use it; otherwise fallback to original chunk
                if chunk_corrected and len(chunk_corrected) >= int(len(chunk) * 0.7):
                    all_corrected_segments.extend(chunk_corrected)
                else:
                    all_corrected_segments.extend(chunk)

            if all_corrected_segments:
                state.transcript = all_corrected_segments
                print(f"[OK] AudioAgent: Transcript corrected successfully ({len(all_corrected_segments)} segments preserved).")
        except Exception as e:
            print(f"[!] AudioAgent: Transcript correction failed: {e}")
            
        return state

    @staticmethod
    def detect_pitch_gender(audio_path: str, start_sec: float, end_sec: float) -> str:
        return detect_pitch_gender(audio_path, start_sec, end_sec)


def detect_pitch_gender(audio_path: str, start_sec: float, end_sec: float) -> str:
    """
    Estimates speaker biological gender (male vs female) using fundamental pitch (F0)
    analysis via normalized autocorrelation across speech frames.
    Male speech F0: typically 85 Hz - 165 Hz
    Female speech F0: typically 165 Hz - 275 Hz
    Returns: 'female', 'male', or 'unknown'
    """
    if not audio_path or not os.path.exists(audio_path) or end_sec <= start_sec:
        return "unknown"
    try:
        import soundfile as sf
        import numpy as np

        info = sf.info(audio_path)
        sr = info.samplerate
        total_frames = info.frames

        s_frame = max(0, int(start_sec * sr))
        e_frame = min(total_frames, int(end_sec * sr))
        if e_frame - s_frame < int(0.2 * sr):
            return "unknown"

        data, _ = sf.read(audio_path, start=s_frame, stop=e_frame, dtype='float32')
        if data.ndim > 1:
            data = np.mean(data, axis=1)

        win_size = int(0.04 * sr)  # 40ms window
        hop_size = int(0.02 * sr)  # 20ms hop
        f0_candidates = []

        min_lag = int(sr / 350)  # 350 Hz max human vocal fundamental
        max_lag = int(sr / 70)   # 70 Hz min human vocal fundamental

        for i in range(0, len(data) - win_size, hop_size):
            frame = data[i:i + win_size]
            energy = np.sum(frame ** 2)
            if energy < 1e-4:
                continue
            frame = frame - np.mean(frame)
            corr = np.correlate(frame, frame, mode='full')
            corr = corr[len(corr) // 2:]
            if max_lag >= len(corr) or corr[0] <= 1e-6:
                continue
            peak_offset = np.argmax(corr[min_lag:max_lag])
            peak_lag = min_lag + peak_offset
            norm_peak = corr[peak_lag] / corr[0]
            if norm_peak > 0.32:
                f0 = sr / peak_lag
                if 70.0 <= f0 <= 350.0:
                    f0_candidates.append(f0)

        if len(f0_candidates) >= 3:
            median_f0 = float(np.median(f0_candidates))
            if median_f0 >= 165.0:
                return "female"
            else:
                return "male"
    except Exception as _exc:
        log_suppressed("agents.audio_agent.detect_pitch_gender", _exc)
    return "unknown"

