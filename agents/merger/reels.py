"""
9:16 Reels Canvas Export (Facebook Reels / TikTok 9:16 ဗီဒီယို ထုတ်လုပ်ခြင်း)
=============================================================================
Builds the vertical 9:16 canvas (blurred background + foreground + hook title + subtitles).

9:16 ဒေါင်လိုက် canvas ဗီဒီယို (blur နောက်ခံ၊ hook ခေါင်းစဉ်၊ စာတန်း) ထုတ်လုပ်ပေးသည်။
"""

from brain.diagnostics import log_suppressed
import os

import brain.config as cfg
from brain.memory import MovieState
from agents.merger.encoder import _get_ffmpeg_bin, detect_hardware_encoder
from agents.merger.media_utils import _get_safe_ascii_id, _get_video_info


class ReelsMixin:
    """Mixin for VideoMergerAgent – methods are used through `self`."""

    def generate_reels_video(
        self,
        source_video_path: str,
        output_dir: str,
        hook_title: str = "",
        subtitle_timings: list = None,
        duration_sec: float = 600.0,
        subtitle_mode: str = None,
        resolution: str = None,
        preset: str = None,
        state: MovieState = None,
    ) -> str | None:
        """
        Exports a dedicated 9:16 Vertical Video for Facebook Reels, TikTok, and Shorts.
        Layout (The 'Viral Recap Frame'):
          - Top 20%: Catchy golden Burmese Hook Title.
          - Middle 55%: Uncropped, high-quality 16:9 recap video centered over blurred dynamic video background.
          - Bottom 25%: Facebook Safe Zone for Myanmar ASS subtitles, well above bottom UI controls.
        Hardware-accelerated via NVENC on Colab or QSV/libx264 on PC.
        Supports 1080p (1080x1920), 720p (720x1280) or 480p draft (480x854) presets and hardsub on/off toggle.
        """
        import sys, subprocess, shutil

        if not os.path.exists(source_video_path):
            print(f"[WARN] ReelsExporter: Source video not found: {source_video_path}")
            return None

        os.makedirs(output_dir, exist_ok=True)
        reels_output = os.path.join(output_dir, "final_reels.mp4")
        temp_dir = os.path.abspath("temp")
        os.makedirs(temp_dir, exist_ok=True)

        config_data = cfg.load_config()
        reels_cfg = config_data.get("reels", {})
        if not reels_cfg.get("enabled", True):
            print("[*] ReelsExporter: Disabled in config.json -> reels.enabled = false")
            return None

        sub_mode = str(subtitle_mode or self.subtitle_mode or "burn").lower()
        res_mode = str(resolution or self.resolution or "1080p").lower()

        if res_mode == "480p":                      # draft quality: the 720p layout scaled by 2/3
            w_target, h_target = 480, 854
            hook_fontsize = 24
            sub_fontsize = 20
            safe_margin = 73
        elif res_mode == "720p":
            w_target = 720
            h_target = 1280
            hook_fontsize = 36
            sub_fontsize = 30
            safe_margin = 110
        else:
            w_target = int(reels_cfg.get("width", 1080))
            h_target = int(reels_cfg.get("height", 1920))
            hook_fontsize = 52
            sub_fontsize = 44
            safe_margin = int(reels_cfg.get("safe_zone_margin", 160))

        font_name = "Myanmar Text" if sys.platform == "win32" else "Padauk"
        font_found = self._find_myanmar_font()
        if font_found and os.path.exists(font_found):
            base_font = os.path.splitext(os.path.basename(font_found))[0]
            if "padauk" in base_font.lower():
                font_name = "Padauk"
            elif "mmrtext" in base_font.lower() or "myanmar" in base_font.lower():
                font_name = "Myanmar Text"

        # 1. Create Reels ASS Subtitle & Hook Title File with unique filename to prevent batch collision
        import uuid
        safe_reels_id = _get_safe_ascii_id(source_video_path, prefix="reels")
        ass_path = os.path.join(temp_dir, f"reels_subs_{safe_reels_id}_{uuid.uuid4().hex[:6]}.ass")
        title_clean = str(hook_title or "").replace("|", "-").strip()
        if not title_clean:
            title_clean = "Movie Recap"
        # Truncate title if extremely long
        if len(title_clean) > 80:
            title_clean = title_clean[:77] + "..."

        # Word wrap title for 1080px portrait canvas (approx 20-22 chars per line) using syllable awareness
        wrapped_title = self._wrap_burmese_text(title_clean, max_chars=20).replace("{\\N}", "\\N")

        # Generate ASS with three styles:
        # Style 1: ReelsBrand (Top Header Badge on Canvas, White/Gold with Dark Pill, MarginV=45)
        # Style 2: ReelsHook (Top Center, Gold/Yellow, Big, MarginV=110)
        # Style 3: ReelsSubs (Bottom Center Safe Zone, styled according to chosen preset)
        wm_cfg = config_data.get("watermark", {})
        wm_override = getattr(state, "watermark_override", {}) or {}
        wm_brand_enabled = wm_override.get("enabled", wm_cfg.get("enabled", True))
        wm_brand_text = wm_override.get("text") or wm_cfg.get("text", "Pai Ai Movie Studio")

        # Resolve subtitle preset for Reels
        sub_cfg = config_data.get("subtitle_overlay", {})
        sub_preset = preset or getattr(state, "subtitle_style_preset", None) or sub_cfg.get("style_preset", "box_black")
        from brain.config import SUBTITLE_PRESETS
        p_data = SUBTITLE_PRESETS.get(str(sub_preset).lower(), {})
        reels_sub_border = int(p_data.get("border_style", 3))
        reels_sub_outline = int(p_data.get("outline_width", 10 if reels_sub_border == 3 else 4))
        reels_sub_shadow = int(p_data.get("shadow", 2))
        reels_sub_primary = p_data.get("primary_color", "&H00FFFFFF")
        reels_sub_outline_col = p_data.get("outline_color", "&H00000000")
        reels_sub_back = p_data.get("back_color", "&HB0000000")
        
        hook_end_s = "9:59:59.99"
        if getattr(state, "outro_card", False) or getattr(state, "outro_card_applied", False):
            # End top hook title 3s before video finishes so outro card is displayed cleanly
            src_dur = 0.0
            try:
                import cv2
                cap = cv2.VideoCapture(source_video_path)
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                if fps > 0:
                    src_dur = frames / fps
                cap.release()
            except Exception as _exc:
                log_suppressed("agents.merger.reels.generate_reels_video", _exc)
            if src_dur > 5.0:
                movie_end = max(1.0, src_dur - 3.0)
                h = int(movie_end // 3600)
                m = int((movie_end % 3600) // 60)
                s = movie_end % 60
                hook_end_s = f"{h}:{m:02d}:{s:05.2f}"

        brand_events = [f"Dialogue: 0,0:00:00.00,{hook_end_s},ReelsBrand,,0,0,0,,🎬 {wm_brand_text}"] if wm_brand_enabled else []
        hook_events = [f"Dialogue: 0,0:00:00.00,{hook_end_s},ReelsHook,,0,0,0,,{wrapped_title}"]
        events_str = "\n".join(brand_events + hook_events)

        ass_content = f"""[Script Info]
Title: Facebook Reels Canvas Overlay
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {w_target}
PlayResY: {h_target}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ReelsBrand,{font_name},32,&H00FFFFFF,&H00000000,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,3,8,2,8,40,40,45,1
Style: ReelsHook,{font_name},{hook_fontsize},&H0000D7FF,&H00000000,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,3,8,40,40,110,1
Style: ReelsSubs,{font_name},{sub_fontsize},{reels_sub_primary},&H00000000,{reels_sub_outline_col},{reels_sub_back},-1,0,0,0,100,100,0,0,{reels_sub_border},{reels_sub_outline},{reels_sub_shadow},2,40,40,{safe_margin + 120},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events_str}
"""
        burn_reels_subs = sub_mode not in ["none", "off", "no"]
        is_clean_source = "_clean" in os.path.basename(source_video_path).lower()
        if not is_clean_source and burn_reels_subs:
            # If source video is already hard-subbed (e.g. fallback when clean video was deleted),
            # prevent burning duplicate overlapping subtitles
            if getattr(state, "subtitles_burned", False):
                print("[*] ReelsExporter: Source video already contains burned subtitles. Skipping duplicate subtitle burn.")
                burn_reels_subs = False

        thumb_offset = 0.0
        if getattr(state, "thumbnail_intro_applied", None) is not None:
            if state.thumbnail_intro_applied:
                thumb_cfg = config_data.get("thumbnail_intro", {})
                thumb_offset = float(thumb_cfg.get("duration_sec", 3.0))
        elif getattr(state, "thumbnail_intro_enabled", False):
            src_dur = 0.0
            try:
                import cv2
                cap = cv2.VideoCapture(source_video_path)
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                if fps > 0:
                    src_dur = frames / fps
                cap.release()
            except Exception as _exc:
                log_suppressed("agents.merger.reels.generate_reels_video", _exc)
            orig_dur = getattr(state, "duration_sec", 0.0) or 0.0
            thumb_cfg = config_data.get("thumbnail_intro", {})
            thumb_dur = float(thumb_cfg.get("duration_sec", 3.0))
            if orig_dur > 0 and src_dur >= orig_dur + (thumb_dur * 0.5):
                thumb_offset = thumb_dur

        if burn_reels_subs and subtitle_timings:
            for item in subtitle_timings:
                try:
                    start_s = float(item[0]) + thumb_offset
                    dur_s   = float(item[1])
                    raw_txt = str(item[2]).strip()
                    if not raw_txt: continue
                    chunks = self._chunk_burmese_narration(raw_txt, max_chars_per_chunk=72)
                    if not chunks: continue
                    seg_dur = dur_s / len(chunks)
                    for i, chunk in enumerate(chunks):
                        seg_start = start_s + i * seg_dur
                        seg_end   = seg_start + seg_dur - 0.05
                        safe_chunk = chunk.replace('\\', '').replace('{', '').replace('}', '').strip()
                        if not safe_chunk: continue
                        ass_text  = self._wrap_burmese_text(safe_chunk, max_chars=24)
                        t_start   = self._sec_to_ass_ts(seg_start)
                        t_end     = self._sec_to_ass_ts(seg_end)
                        ass_content += f"Dialogue: 1,{t_start},{t_end},ReelsSubs,,0,0,0,,{ass_text}\n"
                except Exception as _exc:
                    log_suppressed("agents.merger.reels.generate_reels_video", _exc)

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # 2. Build FFmpeg Filter Graph:
        ffmpeg_bin = _get_ffmpeg_bin()
        enc_info = detect_hardware_encoder()
        abs_src = os.path.abspath(source_video_path)
        temp_reels_out = os.path.join(temp_dir, "temp_reels_render.mp4")

        # 16x faster silky bokeh background: downscale to 270x480, blur lightly, then upscale
        bg_w = w_target // 4
        bg_h = h_target // 4

        # 30 FPS Cap: Reels, TikTok & Shorts are natively 30 FPS
        src_info = _get_video_info(source_video_path)
        reels_fps = src_info.get("fps", 30.0)
        reels_fps_cap = "fps=30," if float(reels_fps) > 32.0 else ""

        # Auto-detect CinemaScope (21:9) letterbox / pillarbox black bars to seamlessly merge foreground with blurred background
        from core.letterbox import detect_letterbox_crop
        letterbox_info = detect_letterbox_crop(source_video_path, ffmpeg_bin=ffmpeg_bin)
        pre_crop = ""
        if letterbox_info:
            pre_crop = f"{letterbox_info['crop_str']},"
            print(f"[*] ReelsExporter: CinemaScope/Letterbox detected ({letterbox_info['w']}x{letterbox_info['h']} from {letterbox_info['orig_w']}x{letterbox_info['orig_h']}, {letterbox_info['h_reduction_pct']}% black bars removed). Applying seamless clean crop.")

        ass_flt = self._build_ass_filter_str(ass_path)

        # Scale movie foreground to canvas width cleanly across any input resolution
        filter_complex = (
            f"[0:v]{reels_fps_cap}{pre_crop}scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
            f"crop={bg_w}:{bg_h},boxblur=12:3,"
            f"scale={w_target}:{h_target}[bg];"
            f"[0:v]{reels_fps_cap}{pre_crop}scale={w_target}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,"
            f"{ass_flt}[out]"
        )

        codec = enc_info["codec"]
        preset = enc_info.get("preset", "veryfast")
        quality_args = ["-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M"] if enc_info["type"] == "gpu" else ["-crf", "20"]
        cmd = [
            ffmpeg_bin, "-y",
            "-i", abs_src,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v", codec,
            "-preset", preset,
            *quality_args,
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            temp_reels_out
        ]

        print(f"[*] ReelsExporter: Rendering 9:16 Canvas Reels ({w_target}x{h_target}) using {enc_info['label']} [{codec}]...")
        timeout_sec = max(3600, int((duration_sec or 600.0) * 4.0))
        try:
            res = subprocess.run(cmd, cwd=temp_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_sec)
            if os.environ.get("CURRENT_JOB_CANCELLED") == "1":
                print("\n🛑 [STOP] ReelsExporter: FFmpeg cancelled by user.")
                raise InterruptedError("Reels rendering was cancelled by user.")
            if res.returncode == 0 and os.path.exists(temp_reels_out) and os.path.getsize(temp_reels_out) > 10_000:
                shutil.move(temp_reels_out, reels_output)
                print(f"🎉 [OK] ReelsExporter: Successfully created 9:16 Facebook Reels video -> {reels_output}")
                return reels_output
            else:
                err_msg = (res.stderr or "")[-500:]
                print(f"[WARN] ReelsExporter: Hardware encoding/filter failed (code={res.returncode}, {err_msg}). Retrying with CPU fallback...")
                
                # Fallback: Retry with libx264 and clean canvas (overlay only, safe against missing libass)
                clean_filter = (
                    f"[0:v]{pre_crop}scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
                    f"crop={bg_w}:{bg_h},boxblur=12:3,"
                    f"scale={w_target}:{h_target}[bg];"
                    f"[0:v]{pre_crop}scale={w_target}:-2[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[out]"
                )
                cmd_fallback = [
                    ffmpeg_bin, "-y",
                    "-i", abs_src,
                    "-filter_complex", clean_filter,
                    "-map", "[out]",
                    "-map", "0:a?",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    temp_reels_out
                ]
                res2 = subprocess.run(cmd_fallback, cwd=temp_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_sec)
                if os.environ.get("CURRENT_JOB_CANCELLED") == "1":
                    print("\n🛑 [STOP] ReelsExporter: FFmpeg CPU fallback cancelled by user.")
                    raise InterruptedError("Reels rendering was cancelled by user.")
                if res2.returncode == 0 and os.path.exists(temp_reels_out) and os.path.getsize(temp_reels_out) > 10_000:
                    shutil.move(temp_reels_out, reels_output)
                    print(f"🎉 [OK] ReelsExporter: Created 9:16 Reels video via fallback -> {reels_output}")
                    return reels_output
                else:
                    print(f"[ERROR] ReelsExporter failed completely: {(res2.stderr or '')[-400:]}")
                    return None
        except InterruptedError:
            raise
        except Exception as e:
            print(f"[ERROR] ReelsExporter encountered error: {e}")
            return None
        finally:
            if os.path.exists(temp_reels_out):
                try: os.remove(temp_reels_out)
                except Exception: pass
