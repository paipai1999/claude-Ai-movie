"""
Single-Pass FFmpeg Compositor (FFmpeg တစ်ကြိမ်တည်း ပေါင်းစပ်ဖန်တီးခြင်း)
=====================================================================
Step 9 of the merge: ONE ffmpeg invocation builds both the finished recap video and the
"clean" canvas (no watermark / subtitles) used later for the 9:16 Reels export.

  _single_pass_inputs   -> the `-i` list and the input index of every optional stream
  _video_filter_parts   -> crop / mirror / zoom / blur / grade / watermark / subtitles
  _audio_filter_parts   -> background bed + voiceover mix with sidechain ducking
  _render_single_pass   -> hardware encode, CPU fallback, blur-less retry
  _apply_intro_outro    -> thumbnail intro and branded outro card

Merge ၏ အဆင့် ၉ - ffmpeg တစ်ကြိမ်တည်းဖြင့် recap ဗီဒီယိုနှင့် Reels အတွက် clean canvas နှစ်ခုလုံးကို ထုတ်သည်။
"""

from brain.diagnostics import log_suppressed
import os
import shutil
import subprocess

from agents.merger.encoder import _get_ffmpeg_bin, detect_hardware_encoder
from agents.merger.plan import MergePlan
from core import resolution as res_presets


class SinglePassMixin:
    """Mixin for VideoMergerAgent – turns a filled MergePlan into the rendered video."""

    # ── ffmpeg inputs ────────────────────────────────────────────────────────────
    def _single_pass_inputs(self, plan: MergePlan, movie_path: str):
        """Returns (ffmpeg_input_args, {"vo","bg","bg_extra","wm": input index or None})."""
        sp_inputs = ["-i", os.path.abspath(movie_path)]
        next_idx = 1
        idx = {"vo": None, "bg": None, "bg_extra": None, "wm": None}

        if plan.has_voiceover:
            sp_inputs.extend(["-i", os.path.abspath(plan.assembled_vo_path)])
            idx["vo"] = next_idx
            next_idx += 1

        if plan.bg_source_type in ["demucs", "bgm", "both"] and plan.bg_audio_file and os.path.exists(plan.bg_audio_file):
            sp_inputs.extend(["-i", os.path.abspath(plan.bg_audio_file)])
            idx["bg"] = next_idx
            next_idx += 1
        if plan.bg_source_type == "both" and plan.bg_extra_file and os.path.exists(plan.bg_extra_file):
            sp_inputs.extend(["-i", os.path.abspath(plan.bg_extra_file)])
            idx["bg_extra"] = next_idx
            next_idx += 1

        if plan.wm_png and os.path.exists(plan.wm_png):
            sp_inputs.extend(["-i", os.path.abspath(plan.wm_png)])
            idx["wm"] = next_idx
            next_idx += 1

        return sp_inputs, idx

    # ── video filter chain ───────────────────────────────────────────────────────
    def _video_filter_parts(self, plan: MergePlan, state, include_blur: bool, wm_input_idx, has_ass: bool):
        """Returns (filter_parts, label_of_the_subtitled_stream)."""
        # 30 FPS Cap: Cinema standard is 24-30 FPS. Capping 60 FPS down to 30 FPS cuts frames & encode time by 50%
        fps_cap = "fps=30," if float(plan.video_fps) > 32.0 else ""
        scale_flt = f"scale=-2:{res_presets.short_side(self.resolution)}," if self.resolution in ("720p", "480p") else ""
        flt_parts = [
            f"[0:v]{fps_cap}{scale_flt}crop=w='trunc(iw/2)*2':h='trunc(ih/2)*2'[v_base]"
        ]
        last_v = "[v_base]"

        if plan.copyright_enabled or getattr(state, "mirror", False):
            mirror_enabled = bool(getattr(state, "mirror", False) or plan.copyright_cfg.get("mirror_video", False))
            if mirror_enabled:
                flt_parts.append(f"{last_v}hflip[v_flipped]")
                last_v = "[v_flipped]"
            resize_factor = float(plan.copyright_cfg.get("resize_factor", 1.02)) if plan.copyright_enabled else 1.0
            if resize_factor != 1.0:
                flt_parts.append(f"{last_v}scale=iw*{resize_factor}:ih*{resize_factor},crop=iw/{resize_factor}:ih/{resize_factor}[v_resized]")
                last_v = "[v_resized]"

        if include_blur and plan.subtitle_found:
            r = plan.blur_strength
            blur_seg = (
                f"{last_v}split=2[v_orig][v_sub_crop];"
                f"[v_sub_crop]crop=iw:'trunc(ih*{plan.height_pct:.3f}/2)*2':0:'trunc(ih*{plan.start_y_pct:.3f}/2)*2',"
                f"boxblur=luma_radius={r}:luma_power=2:chroma_radius={max(1,r//2)}:chroma_power=2[v_blurred_sub];"
                f"[v_orig][v_blurred_sub]overlay=0:'trunc(H*{plan.start_y_pct:.3f}/2)*2'[v_blended]"
            )
            flt_parts.append(blur_seg)
            last_v = "[v_blended]"

        if plan.color_enabled:
            cg_str = (
                f"{last_v}eq=brightness={plan.cg_brightness:.3f}:contrast={plan.cg_contrast:.3f}:saturation={plan.cg_saturation:.3f},"
                f"noise=alls=2:allf=t,vignette=PI/4[v_graded]"
            )
            flt_parts.append(cg_str)
            last_v = "[v_graded]"

        # Anti-Subscribe Edge Zoom: subtly zoom in 9% during the closing 12 seconds to push YouTube end-screen cards offscreen
        if plan.anti_sub_zoom_enabled and plan.effective_video_dur > 25.0:
            zoom_start_t = max(0.0, plan.effective_video_dur - 12.0)
            flt_parts.append(
                f"{last_v}crop=w='if(gte(t,{zoom_start_t:.2f}), trunc(iw*0.91/2)*2, trunc(iw/2)*2)':"
                f"h='if(gte(t,{zoom_start_t:.2f}), trunc(ih*0.91/2)*2, trunc(ih/2)*2)':"
                f"x='(iw-ow)/2':y='(ih-oh)/2',scale=iw:ih[v_zoomed]"
            )
            last_v = "[v_zoomed]"

        # Split clean stream BEFORE applying 16:9 watermark and subtitles so persistent_clean_path
        # is truly clean (no duplicate watermark when exported to 9:16 Reels Canvas)
        flt_parts.append(f"{last_v}split=2[v_for_wm][v_for_clean]")

        if wm_input_idx is not None:
            wm_margin = plan.wm_margin
            if plan.wm_pos == "bottom_left":
                pos_str = f"{wm_margin}:main_h-overlay_h-{wm_margin}"
            elif plan.wm_pos == "bottom_right":
                pos_str = f"main_w-overlay_w-{wm_margin}:main_h-overlay_h-{wm_margin}"
            elif plan.wm_pos == "top_left":
                pos_str = f"{wm_margin}:{wm_margin}"
            elif plan.wm_pos == "top_center":
                pos_str = f"(main_w-overlay_w)/2:{wm_margin}"
            else:
                pos_str = f"main_w-overlay_w-{wm_margin}:{wm_margin}"
            flt_parts.append(f"[v_for_wm][{wm_input_idx}:v]overlay={pos_str}[v_wm_done]")
            v_sub_in = "[v_wm_done]"
        else:
            v_sub_in = "[v_for_wm]"

        if has_ass:
            flt_parts.append(f"{v_sub_in}{self._build_ass_filter_str(plan.target_ass_path)}[v_subbed]")
            r_stream = "[v_subbed]"
        else:
            r_stream = v_sub_in

        return flt_parts, r_stream

    # ── audio filter chain ───────────────────────────────────────────────────────
    @staticmethod
    def _loudness_suffix(plan: MergePlan, state) -> str:
        """
        Brings the finished mix to a normal listening level. The narration clips are quiet (about -24 dB) and ``amix``
        halves every input, so the video came out near -29 LUFS: far below what YouTube / TikTok / Reels play at, and
        viewers had to turn the volume up. Not applied to 100% original audio (the film's own mix is left alone);
        ``audio_ducking.normalize_loudness: false`` turns it off, ``audio_ducking.target_lufs`` changes the target.
        """
        duck_cfg = plan.config_data.get("audio_ducking", {})
        if getattr(state, "audio_mode", "ai_voiceover") == "original" or not duck_cfg.get("normalize_loudness", True):
            return ""
        target = max(-30.0, min(-9.0, float(duck_cfg.get("target_lufs", -16.0))))
        return f",loudnorm=I={target:g}:LRA=11:TP=-1.5,aresample=48000"

    def _audio_filter_parts(self, plan: MergePlan, state, vo_input_idx, bg_input_idx, bg_extra_idx):
        """Dynamic audio ducking & compositing. Always ends with the [a_master1][a_master2] split."""
        flt_parts = []
        bg_source_type = plan.bg_source_type
        duck_cfg = plan.config_data.get("audio_ducking", {})
        duck_enabled = duck_cfg.get("enabled", True)
        if getattr(state, "audio_mode", "ai_voiceover") == "original":
            default_vol = 1.0
        else:
            default_vol = duck_cfg.get("ambient_volume", 0.35)
        ambient_vol = float(getattr(state, "sfx_volume", None) or default_vol)
        target_dur_str = f"{plan.effective_video_dur:.2f}" if plan.effective_video_dur > 0 else "600.00"
        tempo_flt = ",atempo=1.008" if getattr(state, "audio_anti_copyright", False) else ""
        loud = self._loudness_suffix(plan, state)

        if vo_input_idx is not None and (bg_input_idx is not None or bg_source_type == "orig"):
            if bg_source_type == "both":
                flt_parts.append(f"[{bg_input_idx}:a]apad=whole_dur={target_dur_str},atrim=0:{target_dur_str},volume={ambient_vol:.2f}[sfx_pre]")
                flt_parts.append(f"[{bg_extra_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{target_dur_str},volume={ambient_vol * 0.8:.2f}[bgm_pre]")
                flt_parts.append("[sfx_pre][bgm_pre]amix=inputs=2:duration=first:dropout_transition=0[bg_raw]")
            elif bg_source_type == "bgm":
                flt_parts.append(f"[{bg_input_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{target_dur_str},volume={ambient_vol:.2f}[bg_raw]")
            elif bg_source_type == "demucs":
                flt_parts.append(f"[{bg_input_idx}:a]apad=whole_dur={target_dur_str},atrim=0:{target_dur_str},volume={ambient_vol:.2f}[bg_raw]")
            else:
                flt_parts.append(f"[0:a]apad=whole_dur={target_dur_str},atrim=0:{target_dur_str},volume={ambient_vol:.2f}[bg_raw]")

            if duck_enabled:
                flt_parts.append(
                    f"[bg_raw][{vo_input_idx}:a]sidechaincompress=threshold=0.08:ratio=8:attack=100:release=400[ducked_bg]"
                )
                flt_parts.append(
                    f"[ducked_bg][{vo_input_idx}:a]amix=inputs=2:duration=first:dropout_transition=0{tempo_flt}{loud},asplit=2[a_master1][a_master2]"
                )
            else:
                flt_parts.append(
                    f"[bg_raw][{vo_input_idx}:a]amix=inputs=2:duration=first:dropout_transition=0{tempo_flt}{loud},asplit=2[a_master1][a_master2]"
                )
        elif vo_input_idx is not None:
            flt_parts.append(f"[{vo_input_idx}:a]{tempo_flt.lstrip(',') if tempo_flt else 'anull'}{loud},asplit=2[a_master1][a_master2]")
        elif bg_input_idx is not None or bg_source_type == "orig":
            if bg_source_type == "both":
                flt_parts.append(f"[{bg_input_idx}:a]apad=whole_dur={target_dur_str},atrim=0:{target_dur_str},volume={ambient_vol:.2f}[sfx_pre]")
                flt_parts.append(f"[{bg_extra_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{target_dur_str},volume={ambient_vol * 0.8:.2f}[bgm_pre]")
                flt_parts.append(f"[sfx_pre][bgm_pre]amix=inputs=2:duration=first:dropout_transition=0{tempo_flt},asplit=2[a_master1][a_master2]")
            elif bg_source_type == "bgm":
                flt_parts.append(f"[{bg_input_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{target_dur_str},volume={ambient_vol:.2f}{tempo_flt},asplit=2[a_master1][a_master2]")
            elif bg_source_type == "demucs":
                flt_parts.append(f"[{bg_input_idx}:a]apad=whole_dur={target_dur_str},atrim=0:{target_dur_str},volume={ambient_vol:.2f}{tempo_flt},asplit=2[a_master1][a_master2]")
            else:
                flt_parts.append(f"[0:a]apad=whole_dur={target_dur_str},atrim=0:{target_dur_str},volume={ambient_vol:.2f}{tempo_flt},asplit=2[a_master1][a_master2]")
        else:
            flt_parts.append(f"aevalsrc=0:d={target_dur_str},asplit=2[a_master1][a_master2]")

        return flt_parts

    # ── render ───────────────────────────────────────────────────────────────────
    def _render_single_pass(self, plan: MergePlan, state, movie_path: str) -> bool:
        """
        Renders final_recap.mp4 + the clean canvas in one ffmpeg run.
        Tries the hardware encoder (with blur), then libx264, then repeats without blur.
        Returns True only when a usable output file exists. Raises InterruptedError on Stop.
        """
        ffmpeg_bin = _get_ffmpeg_bin()
        if not ffmpeg_bin:
            return False

        final_output = plan.final_output
        persistent_clean_path = plan.persistent_clean_path
        clean_video_path = plan.clean_video_path

        enc_info = detect_hardware_encoder()
        codec = enc_info.get("codec", "libx264")
        preset = enc_info.get("preset", "faster")
        quality_args = ["-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M"] if enc_info.get("type") == "gpu" else ["-crf", "20"]
        print(f"[*] VideoMerger (Single-Pass Engine): Assembling unified Filtergraph using {enc_info.get('label', codec)} [{codec}]...")

        sp_inputs, idx = self._single_pass_inputs(plan, movie_path)
        has_ass = bool(plan.burn_subs and plan.target_ass_path and os.path.exists(plan.target_ass_path))
        ass_dir = (
            os.path.dirname(os.path.abspath(plan.target_ass_path))
            if (has_ass and plan.target_ass_path)
            else (os.path.abspath(plan.temp_dir) if os.path.exists(plan.temp_dir) else os.path.abspath(plan.output_dir))
        )

        def _build_filtergraph(include_blur: bool):
            video_parts, r_stream = self._video_filter_parts(plan, state, include_blur, idx["wm"], has_ass)
            audio_parts = self._audio_filter_parts(plan, state, idx["vo"], idx["bg"], idx["bg_extra"])
            return ";".join(video_parts + audio_parts), r_stream

        dur_sec = plan.effective_video_dur if plan.effective_video_dur > 0 else (getattr(state, "duration_sec", 0.0) if state else 0.0)
        dyn_timeout = max(2400, int((dur_sec or 600.0) * 4.0))

        def _build_sp_cmd(curr_codec, curr_preset, curr_quality, curr_blur):
            flt_str, r_stream = _build_filtergraph(curr_blur)
            return [
                ffmpeg_bin, "-y",
                *sp_inputs,
                "-filter_complex", flt_str,
                "-map", r_stream, "-map", "[a_master1]",
                "-t", f"{plan.effective_video_dur:.2f}",
                "-c:v", curr_codec, "-preset", curr_preset, *curr_quality,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "aac", "-b:a", "192k",
                os.path.abspath(final_output),
                "-map", "[v_for_clean]", "-map", "[a_master2]",
                "-t", f"{plan.effective_video_dur:.2f}",
                "-c:v", curr_codec, "-preset", curr_preset, *curr_quality,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "aac", "-b:a", "192k",
                os.path.abspath(persistent_clean_path),
            ]

        def _output_ok() -> bool:
            return os.path.exists(final_output) and os.path.getsize(final_output) > 1000

        def _publish_clean_copy() -> None:
            try:
                shutil.copy2(persistent_clean_path, clean_video_path)
            except Exception as _exc:
                log_suppressed("agents.merger.single_pass._publish_clean_copy", _exc)
            state.clean_video_path = persistent_clean_path

        # Attempt single-pass: first with blur (if enabled), fallback without blur if filter fails
        blur_attempts = [True, False] if (plan.do_blur and plan.subtitle_found) else [False]

        for attempt_idx, curr_blur in enumerate(blur_attempts):
            sp_cmd = _build_sp_cmd(codec, preset, quality_args, curr_blur)

            print(f"[*] VideoMerger (Single-Pass Engine): Rendering both Recap & Clean Canvas (Blur={curr_blur}, Encoder={codec})...")
            try:
                res = subprocess.run(
                    sp_cmd, cwd=ass_dir, capture_output=True, text=True,
                    timeout=dyn_timeout, encoding="utf-8", errors="replace"
                )
                if os.environ.get("CURRENT_JOB_CANCELLED") == "1":
                    print("\n🛑 [STOP] VideoMerger: FFmpeg single-pass cancelled by user.")
                    raise InterruptedError("Video rendering was cancelled by user.")
                if res.returncode == 0 and _output_ok():
                    _publish_clean_copy()
                    print(f"🚀 [OK] VideoMerger: Pure FFmpeg Single-Pass Video & Audio Compositing COMPLETE! ({codec})")
                    return True

                err_snippet = res.stderr[-500:] if res.stderr else "Unknown error"
                print(f"[WARN] VideoMerger: Single-pass hardware render failed ({err_snippet}). Retrying with CPU libx264...")
                fb_cmd = _build_sp_cmd("libx264", "superfast", ["-crf", "20"], curr_blur)
                res_cpu = subprocess.run(
                    fb_cmd, cwd=ass_dir, capture_output=True, text=True,
                    timeout=dyn_timeout, encoding="utf-8", errors="replace"
                )
                if os.environ.get("CURRENT_JOB_CANCELLED") == "1":
                    print("\n🛑 [STOP] VideoMerger: FFmpeg CPU single-pass cancelled by user.")
                    raise InterruptedError("Video rendering was cancelled by user.")
                if res_cpu.returncode == 0 and _output_ok():
                    _publish_clean_copy()
                    print("🚀 [OK] VideoMerger: Pure FFmpeg Single-Pass CPU Video & Audio Compositing COMPLETE!")
                    return True
                if curr_blur and attempt_idx == 0 and len(blur_attempts) > 1:
                    print("[WARN] VideoMerger: Blur filter failed. Retrying Pure FFmpeg without blur filter to prevent slow MoviePy fallback...")
            except InterruptedError:
                raise
            except Exception as spe:
                print(f"[WARN] Single-pass execution exception: {spe}")

        return False

    # ── intro / outro cards ──────────────────────────────────────────────────────
    def _apply_intro_outro(self, plan: MergePlan, state) -> None:
        """Prepends the thumbnail intro and appends the branded outro card (recap + clean canvas)."""
        final_output = plan.final_output
        persistent_clean_path = plan.persistent_clean_path
        clean_video_path = plan.clean_video_path

        if plan.has_thumb_intro:
            print(f"[*] VideoMerger: Prepending {plan.thumb_duration:.1f}s thumbnail intro with Pure FFmpeg...")
            ok1 = self._prepend_thumbnail_intro_ffmpeg(plan.thumbnail_path, final_output, thumb_duration=plan.thumb_duration)
            ok2 = False
            if os.path.exists(persistent_clean_path):
                ok2 = self._prepend_thumbnail_intro_ffmpeg(plan.thumbnail_path, persistent_clean_path, thumb_duration=plan.thumb_duration)
                try:
                    shutil.copy2(persistent_clean_path, clean_video_path)
                except Exception as _exc:
                    log_suppressed("agents.merger.single_pass._apply_intro_outro", _exc)
            state.thumbnail_intro_applied = bool(ok1 or ok2)
        else:
            state.thumbnail_intro_applied = False

        if plan.has_outro_card:
            print("[*] VideoMerger: Appending 3.0s 'Pai AI Movie Studio' branded Outro Card...")
            ok_out1 = self._append_outro_card_ffmpeg(final_output, outro_duration=3.0)
            ok_out2 = False
            if os.path.exists(persistent_clean_path):
                ok_out2 = self._append_outro_card_ffmpeg(persistent_clean_path, outro_duration=3.0)
                try:
                    shutil.copy2(persistent_clean_path, clean_video_path)
                except Exception as _exc:
                    log_suppressed("agents.merger.single_pass._apply_intro_outro", _exc)
            state.outro_card_applied = bool(ok_out1 or ok_out2)
        else:
            state.outro_card_applied = False
