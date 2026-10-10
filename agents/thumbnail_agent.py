from brain.diagnostics import log_suppressed
import os
import re
import shutil
import subprocess
from brain.memory import MovieState
from brain import config as cfg

class ThumbnailAgent:
    def __init__(self):
        config_data = cfg.load_config()
        self.output_dir = config_data.get("paths", {}).get("output_dir", "outputs")

    def extract_base_frame(self, state: MovieState, movie_path: str) -> str:
        """Extracts a frame from the movie and applies background gradient/blur. Returns temp_base path."""
        print(f"[*] ThumbnailAgent: Extracting base frame for '{state.movie_name}'...")
        try:
            from PIL import Image, ImageDraw, ImageFilter
        except ImportError:
            print("[WARN] ThumbnailAgent: Pillow (PIL) is not installed. Skipping thumbnail generation.")
            return None

        output_folder = os.path.join(self.output_dir, state.project_dir)
        os.makedirs(output_folder, exist_ok=True)
        temp_base = os.path.join(output_folder, 'temp_base.jpg')

        # 1. Extract Smart Frame (Brightest & Sharpest) via OpenCV
        try:
            import cv2
            import numpy as np
            cap = cv2.VideoCapture(movie_path)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 300)
            
            best_frame = None
            best_score = -1
            
            # Sample frames from 10% to 80% of the video
            for sample_pct in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
                frame_idx = int(total_frames * sample_pct)
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if ret and frame is not None:
                    # Calculate brightness (V in HSV)
                    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                    brightness = np.mean(hsv[:, :, 2])
                    
                    # Calculate sharpness (Laplacian variance)
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
                    
                    # Score = Brightness (40%) + Sharpness (60%)
                    score = (brightness / 255.0 * 0.4) + (min(sharpness, 2000) / 2000.0 * 0.6)
                    
                    if score > best_score:
                        best_score = score
                        best_frame = frame
            cap.release()
            
            if best_frame is not None:
                # Convert BGR (OpenCV) to RGB (PIL)
                best_frame_rgb = cv2.cvtColor(best_frame, cv2.COLOR_BGR2RGB)
                base_img = Image.fromarray(best_frame_rgb)
                print("[*] ThumbnailAgent: Smart frame extracted successfully (Highest brightness & sharpness).")
            else:
                raise ValueError("OpenCV could not read any frames.")
                
        except Exception as e:
            print(f"[WARN] ThumbnailAgent: Smart extraction failed ({e}). Attempting fallback to 30% mark...")
            try:
                try:
                    from moviepy.editor import VideoFileClip
                except ImportError:
                    from moviepy import VideoFileClip
                clip = VideoFileClip(movie_path)
                t = max(0.0, min(clip.duration * 0.3, max(0.0, clip.duration - 0.5)))
                frame = clip.get_frame(t)
                clip.close()
                base_img = Image.fromarray(frame)
            except Exception as e2:
                print(f"[WARN] ThumbnailAgent: Fallback extraction failed: {e2}")
                return None

        # 2. Smart Subtitle Blur using Vision AI
        width, height = base_img.size
        
        # Save temp image for Vision AI
        temp_thumb = os.path.join(output_folder, 'temp_thumb.jpg')
        base_img.convert('RGB').save(temp_thumb)
        
        try:
            from brain.gemini_client import call_gemini_vision
            import json
            
            gemini_cfg = cfg.load_config().get('gemini', {})
            api_keys = gemini_cfg.get('api_keys', [])
            
            prompt = (
                "Analyze this video thumbnail carefully. "
                "Are there any hardcoded subtitles, captions, or text overlay on the video itself? "
                'If YES, return JSON: {"has_subtitles": true, "start_y_pct": 0.85, "height_pct": 0.15}. '
                'If NO text, return {"has_subtitles": false}. Respond ONLY with valid JSON.'
            )
            
            print("[*] ThumbnailAgent: Checking thumbnail frame for subtitles using Vision AI...")
            res_text, _ = call_gemini_vision(
                system_prompt="You are a precise computer vision AI. Respond ONLY in valid JSON.",
                user_text=prompt,
                image_path=temp_thumb,
                api_key=api_keys,
                model=gemini_cfg.get('models', {}).get('workhorse', 'gemini-3.5-flash-lite'),
                temperature=0.05
            )
            
            import re as _re
            clean_json = _re.sub(r'(?i)^```(?:json)?\s*|\s*```$', '', res_text.strip(), flags=_re.MULTILINE).strip()
            parsed = {}
            try:
                parsed = json.loads(clean_json)
            except Exception:
                start = clean_json.find('{')
                end = clean_json.rfind('}')
                if start != -1 and end > start:
                    try:
                        parsed = json.loads(clean_json[start:end+1])
                    except Exception as _exc:
                        log_suppressed("agents.thumbnail_agent.extract_base_frame", _exc)
            if not isinstance(parsed, dict):
                parsed = {}
            
            if parsed.get('has_subtitles', False):
                sy = float(parsed.get('start_y_pct', 0.82))
                sh = float(parsed.get('height_pct', 0.18))
                
                # Expand blur box slightly for safety margin
                sy = max(0, sy - 0.02)
                sh = min(1.0 - sy, sh + 0.04)
                
                y1 = int(height * sy)
                y2 = int(height * (sy + sh))
                
                print(f"[*] ThumbnailAgent: Found subtitle at Y:{y1}-{y2}. Applying precise blur.")
                sub_box = (0, y1, width, y2)
                sub_region = base_img.crop(sub_box)
                sub_region = sub_region.filter(ImageFilter.GaussianBlur(radius=20))
                base_img.paste(sub_region, sub_box)
            else:
                print("[*] ThumbnailAgent: No subtitles found on thumbnail frame. Skipping blur.")
                
        except Exception as e:
            print(f"[WARN] ThumbnailAgent: Vision AI blur detection failed: {e}. Falling back to default bottom blur.")
            blur_height = int(height * 0.18)
            bottom_box = (0, height - blur_height, width, height)
            bottom_region = base_img.crop(bottom_box)
            bottom_region = bottom_region.filter(ImageFilter.GaussianBlur(radius=15))
            base_img.paste(bottom_region, bottom_box)
            
        finally:
            if os.path.exists(temp_thumb):
                try: os.remove(temp_thumb)
                except Exception: pass

        # Add cinematic top and bottom gradients for extreme title readability
        gradient = Image.new('RGBA', (width, height), color=(0, 0, 0, 0))
        draw = ImageDraw.Draw(gradient)
        
        # Darken the top 38% for the Myanmar Title
        top_grad_height = int(height * 0.38)
        for y in range(top_grad_height):
            alpha = int(220 * (1.0 - (y / top_grad_height)))
            draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

        # Darken the bottom 30% for cinematic framing
        bot_grad_height = int(height * 0.30)
        for y in range(bot_grad_height):
            alpha = int(180 * (y / bot_grad_height))
            draw.line([(0, height - bot_grad_height + y), (width, height - bot_grad_height + y)], fill=(0, 0, 0, alpha))
            
        base_img = Image.alpha_composite(base_img.convert('RGBA'), gradient)

        # Save temp base image without text
        base_img.convert('RGB').save(temp_base, quality=95)
        return temp_base

    def overlay_text(self, state: MovieState, temp_base: str) -> MovieState:
        """Applies ASS text over temp_base.jpg and saves to thumbnail.jpg."""
        print("[*] ThumbnailAgent: Overlaying SEO text on base frame...")
        
        output_folder = os.path.join(self.output_dir, state.project_dir)
        thumbnail_path = os.path.join(output_folder, "thumbnail.jpg")
        
        try:
            from PIL import Image
            base_img = Image.open(temp_base)
            base_img.load()   # Force load all data so we can safely close the file handle
            base_img = base_img.copy()  # Detach from the file
            width, height = base_img.size
        except Exception as e:
            print(f"[ERROR] ThumbnailAgent: Could not load temp_base {temp_base}: {e}")
            return state

        # 3. Prepare Text
        title = ""
        if state.custom_thumb_title:
            title = state.custom_thumb_title.strip()
            if title.lower() in ["none", "no", "off", "disable", "notitle", "no_title"]:
                print("[*] ThumbnailAgent: Thumbnail title mode is 'NO TITLE' — saving clean base frame without text overlay.")
                shutil.copy(temp_base, thumbnail_path)
                state.thumbnail_path = thumbnail_path
                if os.path.exists(temp_base):
                    try: os.remove(temp_base)
                    except Exception: pass
                return state
            print(f"[*] ThumbnailAgent: Using custom thumbnail text from user: '{title}'")
        else:
            # Use the Burmese clickbait title from SEO metadata if available
            if state.seo_metadata and "title" in state.seo_metadata:
                full_title = state.seo_metadata["title"]
                
                # Split by common separators to find the Burmese part
                parts = re.split(r'[|:;\-]', full_title)
                
                # Find the part with the most Burmese characters
                best_part = ""
                max_mm_chars = 0
                for part in parts:
                    mm_chars = len(re.findall(r'[\u1000-\u109F]', part))
                    if mm_chars > max_mm_chars:
                        max_mm_chars = mm_chars
                        best_part = part
                
                if best_part:
                    # Clean up leftover English letters while preserving numbers (0-9 and Myanmar digits)
                    title = re.sub(r'[A-Za-z]', '', best_part).strip()
                    # Clean up random leftover spaces or punctuation
                    title = re.sub(r'\s+', ' ', title).strip(" .,!?'\"()[]{}")
                    print(f"[*] ThumbnailAgent: Auto-generated pure Burmese thumbnail text from SEO: '{title}'")
                else:
                    title = full_title.split("|")[0].strip()
            
            if not title:
                # Fallback
                raw_title = (state.movie_name or "Movie").replace("_", " ").title()
                match = re.search(r'(.+?)[_\s-]*((?:Season\s*\d+|S\d+E\d+|Episode\s*\d+|Ep\s*\d+|Ep\s*\d+[A-Za-z]?|Part\s*\d+)(?:.*)?)$', state.movie_name or "", flags=re.IGNORECASE)
                if match:
                    base_name = match.group(1).replace("_", " ").title().strip()
                    ep_name = match.group(2).replace("_", " ").title().strip()
                    title = f"{base_name} - {ep_name}"
                else:
                    title = raw_title
                title = re.sub(r'\.(mp4|mkv|webm|avi|mov)$', '', title, flags=re.IGNORECASE)
                print(f"[*] ThumbnailAgent: Fallback thumbnail text: '{title}'")

        # 4. Burn Text using FFmpeg and libass (handles Burmese complex scripts perfectly on Windows)
        import hashlib
        
        # Write ASS to temp/ with safe ASCII filename — prevents Windows libass fopen failure on Unicode CWD
        temp_dir = os.path.abspath("temp")
        os.makedirs(temp_dir, exist_ok=True)
        safe_id = hashlib.md5((state.movie_name or "thumb").encode("utf-8", errors="replace")).hexdigest()[:8]
        ass_basename = f"thumb_{safe_id}.ass"
        ass_path = os.path.join(temp_dir, ass_basename)
        
        # Add Part/Episode number if present in movie_name
        part_match = re.search(r'(?:Part|Ep|Episode|Season\s*\d+\s*Ep|S\d+E|အပိုင်း|ပိုင်း)[_\s\-]*(\d+)', state.movie_name or "", flags=re.IGNORECASE)
        if part_match:
            part_num = part_match.group(1)
            burmese_digits = str.maketrans('0123456789', '၀၁၂၃၄၅၆၇၈၉')
            mm_part_num = part_num.translate(burmese_digits)
            part_text = f"အပိုင်း {mm_part_num}"
            if "အပိုင်း" not in title:
                title = f"{title}\\N{part_text}"

        # Smart Line-Wrapping for Myanmar script
        clean_no_n = title.replace("\\N", "").strip()
        clean_len = len(re.sub(r'[\s]', '', clean_no_n))
        
        # If long and not already broken, insert a natural line break around the middle
        if clean_len > 22 and "\\N" not in title:
            mid = len(title) // 2
            # Find nearest space or punctuation to middle
            split_idx = -1
            for offset in range(len(title) // 2):
                for candidate in [mid - offset, mid + offset]:
                    if 0 <= candidate < len(title) and title[candidate] in (' ', '၊', '။', '-'):
                        split_idx = candidate
                        break
                if split_idx != -1:
                    break
            if split_idx != -1:
                title = title[:split_idx].strip() + "\\N" + title[split_idx:].strip()

        # Dynamic Font Scaling based on character count
        if clean_len <= 14:
            font_size = int(height * 0.092)   # ~100px on 1080p
        elif clean_len <= 26:
            font_size = int(height * 0.078)   # ~84px on 1080p
        else:
            font_size = int(height * 0.064)   # ~69px on 1080p

        ass_text = title.replace('{', '').replace('}', '')
        import sys
        font_family = "Myanmar Text" if sys.platform == "win32" else "Padauk"
        
        # High-CTR YouTube Thumbnail Styling: Top-Center (Alignment: 8), Vibrant Golden Yellow, 6px Deep Black Outline
        ass_content = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 1
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_family},{font_size},&H0000F5FF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,6,4,8,60,60,55,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,60,60,55,,{ass_text}
"""
        try:
            with open(ass_path, 'w', encoding='utf-8') as f:
                f.write(ass_content)
                
            temp_base_abs = os.path.abspath(temp_base)
            thumbnail_path_abs = os.path.abspath(thumbnail_path)
            
            ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
            if not ffmpeg_bin:
                try:
                    from imageio_ffmpeg import get_ffmpeg_exe
                    ffmpeg_bin = get_ffmpeg_exe()
                except Exception:
                    ffmpeg_bin = "ffmpeg"
                    
            ffmpeg_cmd = [
                ffmpeg_bin, "-y",
                "-i", temp_base_abs,
                "-vf", f"ass={ass_basename}",
                "-frames:v", "1",
                "-update", "1",
                thumbnail_path_abs
            ]
            proc = subprocess.run(
                ffmpeg_cmd, cwd=temp_dir,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace"
            )
            if proc.returncode == 0 and os.path.exists(thumbnail_path):
                state.thumbnail_path = thumbnail_path
                print(f"[OK] ThumbnailAgent: Thumbnail (with perfect Burmese font rendering) successfully saved to {thumbnail_path}")
            else:
                print(f"[WARN] ThumbnailAgent: FFmpeg failed to render thumbnail text: {proc.stderr}")
                # Fallback to the temp base image if ffmpeg fails
                shutil.copy(temp_base, thumbnail_path)
                state.thumbnail_path = thumbnail_path
        except Exception as e:
            print(f"[WARN] ThumbnailAgent: Exception during ffmpeg text burn: {e}")
            shutil.copy(temp_base, thumbnail_path)
            state.thumbnail_path = thumbnail_path
        finally:
            if os.path.exists(temp_base):
                try: os.remove(temp_base)
                except Exception: pass
            if os.path.exists(ass_path):
                try: os.remove(ass_path)
                except Exception: pass

        return state
