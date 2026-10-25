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
from agents.merger.media_utils import _has_audio_stream
from core.text_render import draw_text, render_lines

_CPU_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def join_clips(ffmpeg_bin: str, first: str, second: str, target: str, timeout: int):
    """
    Joins two clips of the same picture size (the film and its intro or outro card) into ``target``.
    Returns ``(ok, message)``; the message is FFmpeg's own words when it failed.

    The concat filter refuses clips whose pixel aspect ratio or audio format differ, and a film made from a source with
    a slightly non-square pixel (SAR 26100:26057) never matched the card (SAR 1:1): the intro silently never appeared.
    Both clips are normalised first. The hardware encoder is tried first, the CPU encoder after it.
    """
    audio = _has_audio_stream(first) and _has_audio_stream(second)
    video = "[0:v]setsar=1,format=yuv420p[v0];[1:v]setsar=1,format=yuv420p[v1];"
    if audio:
        graph = (video + "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a0];"
                 "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
                 "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        graph = video + "[v0][v1]concat=n=2:v=1:a=0[v]"            # a film without sound: the card's sound is dropped
        maps = ["-map", "[v]", "-an"]
    info = detect_hardware_encoder()
    hardware = ["-c:v", info.get("codec", "libx264"), "-preset", info.get("preset", "veryfast"),
                *(["-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M"] if info.get("type") == "gpu" else ["-crf", "20"])]
    attempts = [hardware] if "libx264" in hardware else [hardware, _CPU_ENCODE]
    message = ""
    for number, encoder in enumerate(attempts, 1):
        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", os.path.abspath(first), "-i", os.path.abspath(second),
               "-filter_complex", graph, *maps, *encoder, "-movflags", "+faststart", target]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if result.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 1000:
            return True, ""
        message = (result.stderr or "")[-400:]
        if os.path.exists(target):
            os.remove(target)
        if number < len(attempts):
            print("[WARN] VideoMerger: the hardware encoder failed to join the clips; retrying on the CPU.")
    return False, message


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

            joined, err2 = join_clips(ffmpeg_bin, intro_ts, video_path, concat_out, max(1800, int(vid_duration * 3.0)))
            if joined:
                shutil.move(concat_out, video_path)
                print(f"🎉 [OK] VideoMerger: Prepended {thumb_duration:.1f}s thumbnail intro with Pure FFmpeg!")
                return True
            print(f"[WARN] VideoMerger: FFmpeg thumbnail intro concat failed: {err2}")
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
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (w, h), (15, 23, 42))
            draw = ImageDraw.Draw(img)

            # Responsive badge and text layout based on aspect ratio (16:9 vs 9:16)
            is_portrait = h > w
            if is_portrait:
                badge_w = int(w * 0.84)
                badge_h = int(badge_w * 0.22)
                radius = 28
                border_w = 4
                font_title_sz = max(18, int(w * 0.055))
                font_thanks_sz = max(16, int(w * 0.048))
                font_sub_sz = max(14, int(w * 0.038))
            else:
                scale_factor = min(w / 1920.0, h / 1080.0)
                badge_w = int(720 * scale_factor)
                badge_h = int(140 * scale_factor)
                radius = int(24 * scale_factor)
                border_w = max(2, int(4 * scale_factor))
                font_title_sz = max(16, int(52 * scale_factor))
                font_thanks_sz = max(14, int(42 * scale_factor))
                font_sub_sz = max(12, int(32 * scale_factor))
            bx = (w - badge_w) // 2

            font_path = os.path.join('assets', 'fonts', 'Padauk.ttf')
            subtext = "နောက်ထပ် ဇာတ်ကားကောင်းများစွာအတွက်\nLike & Follow လုပ်ထားပေးကြပါဦးခင်ဗျား" if is_portrait else "နောက်ထပ် ဇာတ်ကားကောင်းများစွာအတွက် Like & Follow လုပ်ထားပေးကြပါဦးခင်ဗျား"

            # Myanmar needs HarfBuzz shaping, which Pillow's default layout lacks (see core.text_render).
            title_img = draw_text(ffmpeg_bin, "Pai Ai Movie Studio", font_path, font_title_sz, (255, 255, 255, 255))
            thanks_img = draw_text(ffmpeg_bin, "ကျေးဇူးတင်ပါတယ်", font_path, font_thanks_sz, (234, 179, 8, 255))
            sub_img = render_lines(ffmpeg_bin, subtext.split("\n"), font_path, font_sub_sz, (203, 213, 225, 255), spacing=int(font_sub_sz * 0.4))

            # Stack badge → thank-you → sub text with gaps that follow the type size, and centre the whole block.
            gap_badge = int(font_thanks_sz * 1.1)
            gap_text = int(font_thanks_sz * 0.7)
            thanks_h = thanks_img.height if thanks_img else 0
            sub_h = sub_img.height if sub_img else 0
            block_h = badge_h + gap_badge + thanks_h + gap_text + sub_h
            by = max(0, (h - block_h) // 2)

            draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=radius, fill=(30, 41, 59), outline=(234, 179, 8), width=border_w)
            if title_img is not None:
                img.paste(title_img, ((w - title_img.width) // 2, by + (badge_h - title_img.height) // 2), title_img)
            y = by + badge_h + gap_badge
            if thanks_img is not None:
                img.paste(thanks_img, ((w - thanks_img.width) // 2, y), thanks_img)
            y += thanks_h + gap_text
            if sub_img is not None:
                img.paste(sub_img, ((w - sub_img.width) // 2, y), sub_img)

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

            joined, err2 = join_clips(ffmpeg_bin, video_path, outro_ts, concat_out, max(1800, int(vid_duration * 3.0)))
            if joined:
                shutil.move(concat_out, video_path)
                print(f"🎉 [OK] VideoMerger: Appended {outro_duration:.1f}s 'Pai AI Movie Studio' Outro Card!")
                return True
            print(f"[WARN] VideoMerger: FFmpeg outro card concat failed: {err2}")
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
        from PIL import Image, ImageDraw
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

        # The watermark text is user supplied and usually Burmese: shape it with HarfBuzz (core.text_render).
        font_found = self._find_myanmar_font()
        text_layer = draw_text(
            _get_ffmpeg_bin(), text, font_found or "arialbd.ttf", font_size, (255, 255, 255, int(255 * opacity))
        )
        if text_layer is None:  # empty text
            text_layer = Image.new('RGBA', (1, 1), (0, 0, 0, 0))

        pad_x = 18
        pad_y = 8
        img_width = text_layer.width + (pad_x * 2)
        img_height = text_layer.height + (pad_y * 2)

        img = Image.new('RGBA', (img_width, img_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        if style == "badge":
            # Draw sleek rounded dark pill with border to completely cover source watermark
            radius = min(img_height // 2, 10)
            bg_color = (18, 18, 18, int(230 * opacity))
            border_color = (255, 215, 0, int(200 * opacity)) # Gold accent border
            draw.rounded_rectangle([(0, 0), (img_width - 1, img_height - 1)], radius=radius, fill=bg_color, outline=border_color, width=1)
            img.paste(text_layer, (pad_x, pad_y), text_layer)
        else:
            # Transparent shadow style: the shadow is the text's own coverage, tinted black and slightly fainter
            shadow = Image.new('RGBA', text_layer.size, (0, 0, 0, 0))
            shadow.putalpha(text_layer.getchannel('A').point(lambda a: a * 200 // 255))
            img.paste(shadow, (pad_x + 2, pad_y + 2), shadow)
            img.paste(text_layer, (pad_x, pad_y), text_layer)

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
