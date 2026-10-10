"""
Background Audio Source Selection (နောက်ခံအသံ အရင်းအမြစ် ရွေးချယ်ခြင်း)
======================================================================
Step 5 of the merge: decides which background track (original audio, Demucs "no vocals" stem,
cinematic BGM or SFX + BGM) sits under the voiceover, based on `audio_mode` / `sfx_mode`.

Merge ၏ အဆင့် ၅ - voiceover အောက်တွင် မည်သည့်နောက်ခံအသံ (မူရင်းအသံ၊ Demucs SFX၊ BGM) ကို
သုံးမည်ကို ဆုံးဖြတ်သည်။
"""

import os

from agents.merger.media_utils import _has_audio_stream
from agents.merger.plan import MergePlan


class BackgroundAudioMixin:
    """Mixin for VideoMergerAgent – fills plan.bg_source_type / bg_audio_file / bg_extra_file."""

    def _select_background_audio(self, plan: MergePlan, state, movie_path: str) -> None:
        config_data = plan.config_data

        has_no_vocals = False
        base_candidates = [
            getattr(state, "movie_name", None),
            os.path.splitext(os.path.basename(movie_path))[0],
        ]
        no_vocals_path = None
        for b_name in base_candidates:
            if not b_name:
                continue
            cand_path = os.path.join("temp", state.project_dir, "audio", "htdemucs", b_name, "no_vocals.wav")
            if os.path.exists(cand_path):
                no_vocals_path = cand_path
                has_no_vocals = True
                break

        bgm_cfg = config_data.get("bgm", {})
        bgm_folder = bgm_cfg.get("folder", "assets/bgm")
        bgm_track = None
        if os.path.exists(bgm_folder):
            candidates = [f for f in os.listdir(bgm_folder) if f.endswith(('.wav', '.mp3'))]
            for preferred in ["scifi_tension.wav", "dark_suspense.wav", "action_pulse.wav"]:
                if preferred in candidates:
                    bgm_track = os.path.join(bgm_folder, preferred)
                    break
            if not bgm_track and candidates:
                bgm_track = os.path.join(bgm_folder, candidates[0])

        user_sfx_mode = getattr(state, "sfx_mode", None) or "original_sfx"
        if user_sfx_mode == "demucs":
            user_sfx_mode = "original_sfx"

        bg_source_type = "none"
        bg_audio_file = None
        bg_extra_file = None

        if getattr(state, "audio_mode", None) == "original":
            if _has_audio_stream(movie_path):
                bg_source_type = "orig"
                print("[*] VideoMerger: Audio Mode is 'original' (Preserving 100% original movie audio track).")
            else:
                bg_source_type = "none"
        elif user_sfx_mode == "none":
            bg_source_type = "none"
            print("[*] VideoMerger: SFX Mode is 'none' (Pure voiceover without background audio).")
        elif user_sfx_mode == "bgm":
            if bgm_track and os.path.exists(bgm_track):
                bg_source_type = "bgm"
                bg_audio_file = os.path.abspath(bgm_track)
                print(f"[*] VideoMerger: SFX Mode is 'bgm' -> Adding cinematic BGM: {os.path.basename(bgm_track)}")
            else:
                bg_source_type = "none"
                print("[*] VideoMerger: Cinematic BGM track not found in assets/bgm. Proceeding with clean voiceover.")
        elif user_sfx_mode == "both":
            if has_no_vocals and bgm_track and os.path.exists(bgm_track):
                bg_source_type = "both"
                bg_audio_file = os.path.abspath(no_vocals_path)
                bg_extra_file = os.path.abspath(bgm_track)
                print(f"[*] VideoMerger: SFX Mode is 'both' -> Merging Demucs SFX + Cinematic BGM ({os.path.basename(bgm_track)}).")
            elif has_no_vocals:
                bg_source_type = "demucs"
                bg_audio_file = os.path.abspath(no_vocals_path)
                print("[*] VideoMerger: Found Demucs SFX. Using as background audio.")
            elif bgm_track and os.path.exists(bgm_track):
                bg_source_type = "bgm"
                bg_audio_file = os.path.abspath(bgm_track)
                print(f"[*] VideoMerger: Adding cinematic BGM -> {os.path.basename(bgm_track)}")
            else:
                bg_source_type = "none"
        else:  # default "original_sfx"
            if has_no_vocals and not getattr(state, "skip_demucs", False):
                bg_source_type = "demucs"
                bg_audio_file = os.path.abspath(no_vocals_path)
                print("[*] VideoMerger: Found Demucs no_vocals.wav (Original SFX). Using as background audio.")
            elif bgm_track and os.path.exists(bgm_track):
                bg_source_type = "bgm"
                bg_audio_file = os.path.abspath(bgm_track)
                print(f"[*] VideoMerger: Demucs SFX not available -> Fallback to cinematic BGM: {os.path.basename(bgm_track)}")
            elif _has_audio_stream(movie_path) and not getattr(state, "skip_demucs", False):
                bg_source_type = "orig"
            else:
                bg_source_type = "none"

        plan.bg_source_type = bg_source_type
        plan.bg_audio_file = bg_audio_file
        plan.bg_extra_file = bg_extra_file
