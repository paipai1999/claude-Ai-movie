from brain.diagnostics import log_suppressed
import os
import shutil
import subprocess
from typing import Optional

# Safe torchaudio.load patch with soundfile fallback to avoid torchcodec crash on Windows
try:
    import torchaudio, torch, soundfile as sf
    _orig_torchaudio_load = torchaudio.load

    def _safe_torchaudio_load(uri, *args, **kwargs):
        try:
            data, sr = sf.read(uri)
            tensor = torch.from_numpy(data).float()
            if tensor.ndim == 1:
                tensor = tensor.unsqueeze(0)
            elif tensor.ndim == 2:
                tensor = tensor.t()
            return tensor, sr
        except Exception:
            return _orig_torchaudio_load(uri, *args, **kwargs)

    torchaudio.load = _safe_torchaudio_load
except Exception as _exc:
    log_suppressed("agents.f5_tts_engine.<module>", _exc)

class F5TTSEngine:
    """
    F5-TTS (Zero-Shot Flow-Matching Voice Cloning) Engine Wrapper.
    
    Supports:
      - Zero-shot voice cloning from 3-10s clean reference audio
      - Dynamic GPU (CUDA) / CPU detection
      - Auto-slicing and sample rate handling
      - Pitch-preserving atempo time stretch
    """

    def __init__(self, model_type: str = "F5-TTS", device: str = "auto", speed: float = 1.0, output_dir: Optional[str] = None, **kwargs):
        self.model_type = model_type
        self.speed = speed
        self.device = self._resolve_device(device)
        self.output_dir = output_dir
        self._f5_model = None
        self._is_available = None

    def _resolve_device(self, device_pref: str) -> str:
        """Resolve device to 'cuda' or 'cpu'."""
        if device_pref.lower() in ["cuda", "gpu"]:
            try:
                import torch
                return "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                return "cpu"
        elif device_pref.lower() == "cpu":
            return "cpu"
        else: # "auto"
            try:
                import torch
                return "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                return "cpu"

    def is_available(self) -> bool:
        """Check if f5-tts and its dependencies are installed and importable."""
        if self._is_available is not None:
            return self._is_available
        try:
            from f5_tts.api import F5TTS
            del F5TTS
            self._is_available = True
        except (ImportError, OSError, Exception):
            try:
                import f5_tts
                del f5_tts
                self._is_available = True
            except (ImportError, OSError, Exception):
                self._is_available = False
        return self._is_available

    def _get_model(self):
        """Lazy loader for the F5-TTS model."""
        if self._f5_model is not None:
            return self._f5_model

        if not self.is_available():
            raise ImportError("f5-tts is not installed. Run: pip install f5-tts soundfile")

        print(f"[*] F5-TTS: Initializing {self.model_type} model on device: {self.device.upper()}...")
        try:
            from f5_tts.api import F5TTS
            try:
                # Modern f5-tts (>=0.1.0) uses model='F5TTS_v1_Base'
                model_name = self.model_type if "v1" in self.model_type else "F5TTS_v1_Base"
                self._f5_model = F5TTS(model=model_name, device=self.device)
            except TypeError:
                # Older f5-tts versions used model_type
                self._f5_model = F5TTS(model_type=self.model_type, device=self.device)
            print(f"[OK] F5-TTS: Model loaded successfully on {self.device.upper()}!")
            return self._f5_model
        except Exception as e:
            print(f"[!] F5-TTS: Failed to initialize F5TTS class ({e}). Trying CLI/infer fallback...")
            raise e

    def generate_speech(
        self,
        text: str,
        ref_audio_path: str,
        ref_text: str,
        output_path: str,
        target_dur: Optional[float] = None
    ) -> bool:
        """
        Synthesize speech with character voice cloning.
        
        Args:
            text: Target narration/dialogue to speak
            ref_audio_path: Path to 3-10s reference audio of the character
            ref_text: Transcript of the reference audio (can be empty, but better if provided)
            output_path: Destination audio file (.mp3 or .wav)
            target_dur: Optional duration in seconds for video synchronization
            
        Returns:
            True if synthesis succeeded, False otherwise
        """
        if not text or not text.strip():
            print("[WARN] F5-TTS: Empty text provided. Skipping.")
            return False

        if not ref_audio_path or not os.path.exists(ref_audio_path):
            print(f"[WARN] F5-TTS: Reference audio '{ref_audio_path}' not found.")
            return False

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        base_out, _ = os.path.splitext(output_path)
        temp_wav = f"{base_out}_f5raw.wav"

        try:
            f5 = self._get_model()
            clean_text = text.strip()
            clean_ref_text = (ref_text or "").strip()

            print(f"[*] F5-TTS: Generating voice clone with ref '{os.path.basename(ref_audio_path)}'...")
            
            chunks = self._split_text_chunks(clean_text, max_chars=130)
            if len(chunks) <= 1:
                f5.infer(
                    ref_file=ref_audio_path,
                    ref_text=clean_ref_text,
                    gen_text=clean_text,
                    file_wave=temp_wav,
                    speed=self.speed
                )
            else:
                print(f"[*] F5-TTS: Long text detected ({len(clean_text)} chars). Split into {len(chunks)} chunks to prevent tensor overflow.")
                chunk_files = []
                for idx, chunk in enumerate(chunks):
                    c_wav = f"{base_out}_f5part_{idx}.wav"
                    f5.infer(
                        ref_file=ref_audio_path,
                        ref_text=clean_ref_text,
                        gen_text=chunk,
                        file_wave=c_wav,
                        speed=self.speed
                    )
                    if os.path.exists(c_wav) and os.path.getsize(c_wav) > 1000:
                        chunk_files.append(c_wav)
                    else:
                        raise RuntimeError(f"F5-TTS failed to synthesize part {idx+1}/{len(chunks)}")
                
                self._concat_wavs(chunk_files, temp_wav)
                for cf in chunk_files:
                    try: os.remove(cf)
                    except Exception: pass

            if not os.path.exists(temp_wav) or os.path.getsize(temp_wav) < 1000:
                raise RuntimeError("F5-TTS generated empty or missing output wave.")

            # Convert to MP3 / apply target duration atempo stretch
            self._finalize_audio(temp_wav, output_path, target_dur)
            return True

        except Exception as e:
            print(f"[ERROR] F5-TTS synthesis failed: {e}")
            if os.path.exists(temp_wav):
                try: os.remove(temp_wav)
                except Exception: pass
            return False
        finally:
            if os.path.exists(temp_wav) and output_path.lower().endswith(".mp3"):
                try: os.remove(temp_wav)
                except Exception: pass

    @staticmethod
    def _split_text_chunks(text: str, max_chars: int = 130) -> list:
        """Split text into manageable chunks at sentence/punctuation boundaries to stay under 8192 frame limit."""
        if len(text) <= max_chars:
            return [text]
        import re
        tokens = re.split(r'([။၊\n])', text)
        parts = []
        i = 0
        while i < len(tokens):
            part = tokens[i]
            if i + 1 < len(tokens) and tokens[i+1] in ['။', '၊', '\n']:
                part += tokens[i+1]
                i += 1
            if part.strip():
                parts.append(part)
            i += 1

        chunks = []
        current = ""
        for part in parts:
            if len(current) + len(part) <= max_chars:
                current += part
            else:
                if current.strip():
                    chunks.append(current.strip())
                current = part
        if current.strip():
            chunks.append(current.strip())
        return chunks or [text]

    @staticmethod
    def _concat_wavs(wav_paths: list, out_path: str):
        """Concatenates multiple WAV audio files sequentially using soundfile and numpy."""
        import soundfile as sf
        import numpy as np
        data_list = []
        sr = None
        for p in wav_paths:
            data, curr_sr = sf.read(p)
            sr = curr_sr
            data_list.append(data)
        if data_list and sr:
            combined = np.concatenate(data_list)
            sf.write(out_path, combined, sr)

    def _finalize_audio(self, raw_wav: str, final_path: str, target_dur: Optional[float] = None):
        """Converts raw WAV to destination format and applies atempo stretch if needed."""
        ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
        if not ffmpeg_bin:
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                ffmpeg_bin = get_ffmpeg_exe()
            except Exception:
                ffmpeg_bin = "ffmpeg"

        # Calculate duration if target_dur is given
        filter_str = None
        if target_dur is not None and target_dur > 0:
            try:
                try:
                    from moviepy.editor import AudioFileClip
                except ImportError:
                    from moviepy import AudioFileClip
                clip = AudioFileClip(raw_wav)
                raw_dur = clip.duration
                clip.close()

                if raw_dur > 0:
                    stretch_ratio = target_dur / raw_dur
                    effective_ratio = min(1.15, max(0.7, stretch_ratio))
                    if abs(effective_ratio - 1.0) > 0.05:
                        atempo_val = 1.0 / effective_ratio
                        if atempo_val > 2.0:
                            filter_str = f"atempo=2.0,atempo={atempo_val/2.0:.3f}"
                        elif atempo_val < 0.5:
                            filter_str = f"atempo=0.5,atempo={atempo_val/0.5:.3f}"
                        else:
                            filter_str = f"atempo={atempo_val:.3f}"
            except Exception as e:
                print(f"[WARN] F5-TTS: Duration measurement failed: {e}")

        cmd = [ffmpeg_bin, "-y", "-i", raw_wav]
        if filter_str:
            cmd.extend(["-filter:a", filter_str])
        cmd.extend(["-codec:a", "libmp3lame" if final_path.lower().endswith(".mp3") else "pcm_s16le", final_path])

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
