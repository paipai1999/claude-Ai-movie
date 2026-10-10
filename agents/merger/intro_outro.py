"""
Intro / Outro Cards & Watermark (Intro၊ Outro card နှင့် Watermark)
===================================================================
Thumbnail intro, branded outro card and the adaptive watermark overlay image.

Thumbnail intro၊ Outro card နှင့် Watermark ပုံ တည်ဆောက်ခြင်း။
"""

import os
import shutil
import subprocess

from agents.merger.encoder import _get_ffmpeg_bin, detect_hardware_encoder


class IntroOutroMixin:
    """Mixin for VideoMergerAgent – methods are used through `self`."""

    @staticmethod
    def _prepend_thumbnail_intro_ffmpeg(thumb_path: str, video_path: str, thumb_duration: float = 3.0) -> bool:
        """Prepends a 3-second thumbnail intro to the rendered video using fast FFmpeg concat."""
        ffmpeg_bin = _get_ffmpeg_bin()
        if not ffmpeg_bin or not os.path.exists(thumb_path) or not os.path.exists(video_path):
            return False

        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
            if fps <= 0 or fps > 120:
                fps = 24.0
            frame_cnt = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            vid_duration = (frame_cnt / fps) if fps > 0 and frame_cnt > 0 else 600.0
            cap.release()
        except Exception:
            w, h, fps, vid_duration = 1920, 1080, 24.0, 600.0

        tmp_dir = os.path.dirname(os.path.abspath(video_path))
        base_name, _ = os.path.splitext(os.path.basename(video_path))
        intro_ts = os.path.join(tmp_dir, f"{base_name}_intro_tmp.mp4")
        concat_out = os.path.join(tmp_dir, f"{base_name}_with_intro.mp4")

        try:
            intro_cmd = [
                ffmpeg_bin, "-y",
                "-loop", "1", "-framerate", str(fps), "-t", str(thumb_duration),
                "-i", os.path.abspath(thumb_path),
                "-f", "lavfi", "-t", str(thumb_duration), "-i", "anullsrc=r=44100:cl=stereo",
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                intro_ts
            ]
            res1 = subprocess.run(intro_cmd, capture_output=True, timeout=60)
            if res1.returncode != 0 or not os.path.exists(intro_ts):
                err = (res1.stderr.decode("utf-8", errors="replace") if isinstance(res1.stderr, bytes) else str(res1.stderr or ""))[-300:]
                print(f"[WARN] Failed to generate FFmpeg thumbnail intro: {err}")
                return False

            enc_info = detect_hardware_encoder()
            codec = enc_info.get("codec", "libx264")
            preset = enc_info.get("preset", "veryfast")
            quality_args = ["-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M"] if enc_info.get("type") == "gpu" else ["-crf", "20"]

            concat_cmd = [
                ffmpeg_bin, "-y",
                "-i", intro_ts,
                "-i", os.path.abspath(video_path),
                "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", codec, "-preset", preset, *quality_args,
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                concat_out
            ]
            concat_timeout = max(1800, int(vid_duration * 3.0))
            res2 = subprocess.run(concat_cmd, capture_output=True, timeout=concat_timeout)
            if res2.returncode == 0 and os.path.exists(concat_out) and os.path.getsize(concat_out) > 1000:
                shutil.move(concat_out, video_path)
                print(f"🎉 [OK] VideoMerger: Prepended {thumb_duration:.1f}s thumbnail intro with Pure FFmpeg!")
                return True
            else:
                err2 = (res2.stderr.decode("utf-8", errors="replace") if isinstance(res2.stderr, bytes) else str(res2.stderr or ""))[-400:]
                print(f"[WARN] VideoMerger: FFmpeg thumbnail intro concat failed (code={res2.returncode}): {err2}")
        except Exception as te:
            print(f"[WARN] VideoMerger: FFmpeg thumbnail intro prepend failed: {te}")
        finally:
            for p in [intro_ts, concat_out]:
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
        return False

    @staticmethod
    def _append_outro_card_ffmpeg(video_path: str, outro_duration: float = 3.0) -> bool:
        """Appends a 3-second 'Pai AI Movie Studio' branded Outro Card to the rendered video."""
        ffmpeg_bin = _get_ffmpeg_bin()
        if not ffmpeg_bin or not os.path.exists(video_path):
            return False

        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
            if fps <= 0 or fps > 120:
                fps = 24.0
            frame_cnt = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            vid_duration = (frame_cnt / fps) if fps > 0 and frame_cnt > 0 else 600.0
            cap.release()
        except Exception:
            w, h, fps, vid_duration = 1920, 1080, 24.0, 600.0

        tmp_dir = os.path.dirname(os.path.abspath(video_path))
        base_name, _ = os.path.splitext(os.path.basename(video_path))
        outro_img_path = os.path.join(tmp_dir, f"{base_name}_outro_card.png")
        outro_ts = os.path.join(tmp_dir, f"{base_name}_outro_tmp.mp4")
        concat_out = os.path.join(tmp_dir, f"{base_name}_with_outro.mp4")

        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new('RGB', (w, h), (15, 23, 42))
            draw = ImageDraw.Draw(img)

            # Responsive badge and text layout based on aspect ratio (16:9 vs 9:16)
            is_portrait = h > w
            if is_portrait:
                badge_w = int(w * 0.84)
                badge_h = int(badge_w * 0.22)
                bx = (w - badge_w) // 2
                by = (h - badge_h) // 2 - int(h * 0.06)
                radius = 28
                border_w = 4
                font_title_sz = max(18, int(w * 0.055))
                font_thanks_sz = max(16, int(w * 0.048))
                font_sub_sz = max(14, int(w * 0.038))
                sub_offset = int(h * 0.09)
            else:
                scale_factor = min(w / 1920.0, h / 1080.0)
                badge_w = int(720 * scale_factor)
                badge_h = int(140 * scale_factor)
                bx = (w - badge_w) // 2
                by = (h - badge_h) // 2 - int(60 * scale_factor)
                radius = int(24 * scale_factor)
                border_w = max(2, int(4 * scale_factor))
                font_title_sz = max(16, int(52 * scale_factor))
                font_thanks_sz = max(14, int(42 * scale_factor))
                font_sub_sz = max(12, int(32 * scale_factor))
                sub_offset = int(120 * scale_factor)

            draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=radius, fill=(30, 41, 59), outline=(234, 179, 8), width=border_w)

            font_path = os.path.join('assets', 'fonts', 'Padauk.ttf')
            try:
                font_title = ImageFont.truetype(font_path, font_title_sz)
                font_thanks = ImageFont.truetype(font_path, font_thanks_sz)
                font_sub = ImageFont.truetype(font_path, font_sub_sz)
            except Exception:
                font_title = font_thanks = font_sub = ImageFont.load_default()

            draw.text((w // 2, by + badge_h // 2), "Pai Ai Movie Studio", fill=(255, 255, 255), font=font_title, anchor="mm")
            draw.text((w // 2, by + badge_h + (int(h * 0.04) if is_portrait else int(60 * scale_factor))), "ကျေးဇူးတင်ပါတယ်", fill=(234, 179, 8), font=font_thanks, anchor="mm")
            subtext = "နောက်ထပ် ဇာတ်ကားကောင်းများစွာအတွက်\nLike & Follow လုပ်ထားပေးကြပါဦးခင်ဗျား" if is_portrait else "နောက်ထပ် ဇာတ်ကားကောင်းများစွာအတွက် Like & Follow လုပ်ထားပေးကြပါဦးခင်ဗျား"
            draw.multiline_text((w // 2, by + badge_h + sub_offset), subtext, fill=(203, 213, 225), font=font_sub, anchor="mm", align="center", spacing=12)

            img.save(outro_img_path, "PNG")

            # Create 3-second video clip with fade in & fade out
            outro_cmd = [
                ffmpeg_bin, "-y",
                "-loop", "1", "-framerate", str(fps), "-t", str(outro_duration),
                "-i", os.path.abspath(outro_img_path),
                "-f", "lavfi", "-t", str(outro_duration), "-i", "anullsrc=r=44100:cl=stereo",
                "-vf", f"scale={w}:{h},fade=t=in:st=0:d=0.5,fade=t=out:st={outro_duration-0.5:.2f}:d=0.5,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                outro_ts
            ]
            res1 = subprocess.run(outro_cmd, capture_output=True, timeout=60)
            if res1.returncode != 0 or not os.path.exists(outro_ts):
                err = (res1.stderr.decode("utf-8", errors="replace") if isinstance(res1.stderr, bytes) else str(res1.stderr or ""))[-300:]
                print(f"[WARN] Failed to generate FFmpeg outro card: {err}")
                return False

            enc_info = detect_hardware_encoder()
            codec = enc_info.get("codec", "libx264")
            preset = enc_info.get("preset", "veryfast")
            quality_args = ["-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M"] if enc_info.get("type") == "gpu" else ["-crf", "20"]

            concat_cmd = [
                ffmpeg_bin, "-y",
                "-i", os.path.abspath(video_path),
                "-i", outro_ts,
                "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", codec, "-preset", preset, *quality_args,
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                concat_out
            ]
            concat_timeout = max(1800, int(vid_duration * 3.0))
            res2 = subprocess.run(concat_cmd, capture_output=True, timeout=concat_timeout)
            if res2.returncode == 0 and os.path.exists(concat_out) and os.path.getsize(concat_out) > 1000:
                shutil.move(concat_out, video_path)
                print(f"🎉 [OK] VideoMerger: Appended {outro_duration:.1f}s 'Pai AI Movie Studio' Outro Card!")
                return True
            else:
                err2 = (res2.stderr.decode("utf-8", errors="replace") if isinstance(res2.stderr, bytes) else str(res2.stderr or ""))[-400:]
                print(f"[WARN] VideoMerger: FFmpeg outro card concat failed (code={res2.returncode}): {err2}")
        except Exception as te:
            print(f"[WARN] VideoMerger: FFmpeg outro card append failed: {te}")
        finally:
            for p in [outro_img_path, outro_ts, concat_out]:
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
        return False

    # ─────────────────────────────────────────────────────
    # WATERMARK GENERATION (Adaptive Contrast)
    # ─────────────────────────────────────────────────────

    def _create_watermark_image(
        self,
        text: str,
        font_size: int = 28,
        opacity: float = 0.85,
        style: str = "badge",
        logo_path: str = ""
    ) -> str:
        """
        Creates a transparent PNG with custom channel branding badge or watermark.
        Styles:
          - 'badge': Sleek dark rounded badge pill with gold/white text for masking underlying watermarks.
          - 'transparent': Simple semi-transparent text with drop shadow.
        """
        from PIL import Image, ImageDraw, ImageFont
        import os
        
        temp_dir = os.path.abspath("temp")
        os.makedirs(temp_dir, exist_ok=True)
        out_path = os.path.join(temp_dir, "watermark.png")
        
        # If logo image file is provided and exists, use it directly
        if logo_path and os.path.exists(logo_path):
            try:
                logo_img = Image.open(logo_path).convert("RGBA")
                if opacity < 1.0:
                    alpha = logo_img.split()[3].point(lambda p: int(p * opacity))
                    logo_img.putalpha(alpha)
                logo_img.save(out_path, "PNG")
                return out_path
            except Exception as le:
                print(f"[WARN] VideoMerger: Failed to load logo image {logo_path}: {le}")

        font_found = self._find_myanmar_font()
        try:
            font = ImageFont.truetype(font_found or "arialbd.ttf", font_size)
        except IOError:
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except IOError:
                font = ImageFont.load_default()

        # Measure text
        dummy_img = Image.new('RGBA', (1, 1))
        draw = ImageDraw.Draw(dummy_img)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = max(1, bbox[2] - bbox[0])
            text_height = max(1, bbox[3] - bbox[1])
        except Exception:
            try:
                text_width, text_height = draw.textsize(text, font=font)
            except Exception:
                text_width, text_height = max(1, len(text) * 14), 28

        pad_x = 18
        pad_y = 8
        img_width = text_width + (pad_x * 2)
        img_height = text_height + (pad_y * 2)

        img = Image.new('RGBA', (img_width, img_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        if style == "badge":
            # Draw sleek rounded dark pill with border to completely cover source watermark
            radius = min(img_height // 2, 10)
            bg_color = (18, 18, 18, int(230 * opacity))
            border_color = (255, 215, 0, int(200 * opacity)) # Gold accent border
            draw.rounded_rectangle([(0, 0), (img_width - 1, img_height - 1)], radius=radius, fill=bg_color, outline=border_color, width=1)
            # Gold / Pure White text
            text_color = (255, 255, 255, int(255 * opacity))
            draw.text((pad_x, pad_y - 2), text, font=font, fill=text_color)
        else:
            # Transparent shadow style
            shadow_offset = (2, 2)
            shadow_color = (0, 0, 0, int(200 * opacity))
            text_color = (255, 255, 255, int(255 * opacity))
            draw.text((pad_x + shadow_offset[0], pad_y + shadow_offset[1]), text, font=font, fill=shadow_color)
            draw.text((pad_x, pad_y), text, font=font, fill=text_color)

        img.save(out_path, "PNG")
        return out_path

    # ─────────────────────────────────────────────────────
    # MERGE STEPS 7 & 8a — watermark overlay + thumbnail-intro decision
    # ─────────────────────────────────────────────────────

    def _prepare_watermark(self, plan, state) -> None:
        """Creates the watermark PNG (or leaves plan.wm_png = None when disabled / failed)."""
        wm_cfg = plan.config_data.get("watermark", {})
        wm_override = getattr(state, "watermark_override", {}) or {}
        wm_enabled = wm_override.get("enabled", wm_cfg.get("enabled", False))
        wm_png = None
        wm_pos = "bottom_left"
        wm_margin = 25
        # Apply the requested watermark to every rendered format, including
        # 9:16-only exports. Previously reels-only jobs silently skipped it.
        if wm_enabled:
            wm_text = wm_override.get("text") or wm_cfg.get("text", "Pai Ai Movie Studio")
            wm_opacity = float(wm_override.get("opacity") if wm_override.get("opacity") is not None else wm_cfg.get("opacity", 0.85))
            wm_font_size = int(wm_override.get("font_size") or wm_cfg.get("font_size", 28))
            wm_margin = int(wm_override.get("margin") or wm_cfg.get("margin", 25))
            wm_pos = str(wm_override.get("position") or wm_cfg.get("position", "bottom_left")).lower()
            wm_style = str(wm_override.get("style") or wm_cfg.get("style", "badge")).lower()
            wm_logo_path = wm_override.get("logo_path") or wm_cfg.get("logo_path", "")
            try:
                wm_png = self._create_watermark_image(
                    text=wm_text,
                    font_size=wm_font_size,
                    opacity=wm_opacity,
                    style=wm_style,
                    logo_path=wm_logo_path
                )
                if not (wm_png and os.path.exists(wm_png)):
                    wm_png = None
            except Exception as e:
                print(f"[WARN] VideoMerger: Failed to apply watermark: {e}")
                wm_png = None

        plan.wm_png = wm_png
        plan.wm_pos = wm_pos
        plan.wm_margin = wm_margin

    def _plan_thumbnail_intro(self, plan, state) -> None:
        """Decides whether the 3-second thumbnail intro will be prepended."""
        thumb_intro_cfg = plan.config_data.get("thumbnail_intro", {})
        thumb_intro_enabled = getattr(state, "thumbnail_intro_enabled", None)
        if thumb_intro_enabled is None:
            thumb_intro_enabled = thumb_intro_cfg.get("enabled", False)
        plan.thumb_duration = float(thumb_intro_cfg.get("duration_sec", 3.0))
        plan.thumbnail_path = os.path.join(plan.output_dir, "thumbnail.jpg")
        plan.has_thumb_intro = bool(thumb_intro_enabled and os.path.exists(plan.thumbnail_path))
