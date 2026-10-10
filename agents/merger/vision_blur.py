"""
Subtitle Region Blur (စာတန်းဟောင်း Blur လုပ်ခြင်း)
==================================================
Gemini-Vision detection of burnt-in subtitles and the FFmpeg boxblur pass that hides them.

မူရင်း စာတန်းဟောင်းများကို Vision AI ဖြင့်ရှာပြီး boxblur ဖြင့် ဖုံးကွယ်ပေးသည်။
"""


from brain.memory import MovieState
from agents.merger.encoder import _get_ffmpeg_bin, detect_hardware_encoder
import os


class SubtitleBlurMixin:
    """Mixin for VideoMergerAgent – methods are used through `self`."""

    # ─────────────────────────────────────────────────────
    # SUBTITLE BLUR — Vision AI Auto-Detection + FFmpeg Pass
    # ─────────────────────────────────────────────────────

    def _detect_subtitle_region_with_vision(self, video_path: str, state: MovieState = None):
        """
        Uses Gemini Vision AI to analyze sample frames from the ORIGINAL SOURCE video
        and detect the EXACT vertical region (start_y_pct, height_pct) where
        hardcoded subtitles appear.
        Samples frames at Whisper dialogue timestamps to guarantee subtitle visibility.

        Returns:
            (start_y_pct, height_pct, subtitle_found)
        """
        import os, cv2, json
        from brain.gemini_client import call_gemini_vision
        import brain.config as cfg

        print(f"[*] VisionAI Subtitle Detector: Analyzing source video for subtitles: {os.path.basename(video_path)}")
        temp_dir = os.path.join("temp", "sub_detect")
        os.makedirs(temp_dir, exist_ok=True)

        default_start_y = 0.82
        default_height  = 0.18

        try:
            config_data = cfg.load_config()
            gemini_cfg = config_data.get("gemini", {})
            api_keys = gemini_cfg.get("api_keys", [])
            model = gemini_cfg.get("model", "gemini-3.5-flash-lite")

            prompt = (
                "Analyze this video frame carefully. Look at the lower half of the frame (bottom 50%). "
                "Are there any hardcoded DIALOGUE subtitles or captions? "
                "IGNORE watermarks, channel logos, or title cards at the top or middle of the screen. "
                "Respond ONLY in valid JSON with NO markdown formatting: "
                '{"has_subtitles": true, "start_y_pct": 0.75, "height_pct": 0.12} '
                "where start_y_pct (0.0=top, 1.0=bottom) is where the dialogue text STARTS, "
                "and height_pct is the vertical height of the text region. "
                "If no dialogue subtitles exist in the lower half, respond: "
                '{"has_subtitles": false, "start_y_pct": 0.82, "height_pct": 0.18}'
            )

            def get_sample_frames(pcts, stage_prefix):
                frames = []
                cap = cv2.VideoCapture(video_path)
                try:
                    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 300)
                    
                    dialogue_times = []
                    if state and getattr(state, "transcript", None):
                        for s in state.transcript[:15]:
                            t_val = getattr(s, "start", None) if hasattr(s, "start") else s.get("start") if isinstance(s, dict) else None
                            if t_val is not None and float(t_val) > 1.0:
                                dialogue_times.append(float(t_val) + 0.5)

                    if dialogue_times and stage_prefix == "s1":
                        step = max(1, len(dialogue_times) // 5)
                        for idx, sec in enumerate(dialogue_times[::step][:5]):
                            frame_idx = min(int(sec * fps), total_frames - 1)
                            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                            ret, frame = cap.read()
                            if ret and frame is not None:
                                frame_path = os.path.join(temp_dir, f"frame_dialogue_{idx}_{int(sec)}s.jpg")
                                cv2.imwrite(frame_path, frame)
                                frames.append(frame_path)

                    if not frames:
                        for sample_pct in pcts:
                            sample_frame_idx = int(total_frames * sample_pct)
                            cap.set(cv2.CAP_PROP_POS_FRAMES, sample_frame_idx)
                            ret, frame = cap.read()
                            if ret and frame is not None:
                                frame_path = os.path.join(temp_dir, f"frame_{stage_prefix}_{int(sample_pct*100):03d}.jpg")
                                cv2.imwrite(frame_path, frame)
                                frames.append(frame_path)
                finally:
                    cap.release()
                return frames

            def scan_frames(sample_frames, stage_name):
                print(f"[*] VisionAI Subtitle Detector ({stage_name}): Analyzing {len(sample_frames)} frames...")
                results = []
                for i, frame_path in enumerate(sample_frames):
                    try:
                        res_text, used_model = call_gemini_vision(
                            system_prompt=(
                                "You are a precise computer vision AI. Your job is to detect ANY text, "
                                "subtitles, captions, or overlaid text in video frames. "
                                "Look at the ENTIRE frame carefully. Respond ONLY in valid JSON."
                            ),
                            user_text=prompt,
                            image_path=frame_path,
                            api_key=api_keys,
                            model=model,
                            temperature=0.05
                        )
                        clean_json = res_text.strip().strip("`").replace("json", "").strip()
                        start_idx = clean_json.find('{')
                        end_idx = clean_json.rfind('}')
                        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                            clean_json = clean_json[start_idx:end_idx+1]
                        parsed = json.loads(clean_json)
                        if isinstance(parsed, list) and len(parsed) > 0:
                            parsed = parsed[0]
                        elif not isinstance(parsed, dict):
                            parsed = {}
                            
                        results.append(parsed)
                        found = parsed.get("has_subtitles", False)
                        print(f"[*] VisionAI [{stage_name}] Frame {i+1}/{len(sample_frames)}: has_subtitles={found} "
                              f"y={parsed.get('start_y_pct', '?')} h={parsed.get('height_pct', '?')} (model: {used_model})")
                    except Exception as e:
                        print(f"[WARN] VisionAI [{stage_name}] Frame {i+1} failed: {e}")
                        continue
                return results

            # Stage 1: Scan first 50% of the video (5%, 15%, 25%, 35%, 50%)
            stage1_frames = get_sample_frames([0.05, 0.15, 0.25, 0.35, 0.50], "s1")
            if not stage1_frames:
                print("[WARN] VisionAI Subtitle Detector: Could not read any frame. Skipping blur.")
                return default_start_y, default_height, False

            detection_results = scan_frames(stage1_frames, "Stage 1 (0-50%)")
            frames_with_subs = [r for r in detection_results if r.get("has_subtitles", False)]

            # Stage 2 Fallback: If no subtitles found in first 50%, scan second half (60%, 70%, 80%, 90%)
            if not frames_with_subs:
                print("[*] VisionAI Stage 1 (0-50%) found NO subtitles. Triggering Stage 2: Scanning remaining 50-90% of video...")
                stage2_frames = get_sample_frames([0.60, 0.70, 0.80, 0.90], "s2")
                if stage2_frames:
                    stage2_results = scan_frames(stage2_frames, "Stage 2 (50-90%)")
                    detection_results.extend(stage2_results)
                    frames_with_subs = [r for r in detection_results if r.get("has_subtitles", False)]

            if not detection_results:
                print("[WARN] VisionAI: All frame analyses failed. Skipping blur.")
                return default_start_y, default_height, False

            # Filter out false positives (e.g., watermarks at the top of the screen)
            # Dialogue subtitles are almost always in the lower half (y >= 0.5)
            valid_subs = []
            for r in frames_with_subs:
                try:
                    raw_y = r.get("start_y_pct", r.get("y", default_start_y))
                    raw_h = r.get("height_pct", r.get("h", default_height))
                    y_val = float(raw_y)
                    h_val = float(raw_h)
                    # Normalize if returned as raw pixel value (e.g. 743px for 1080p frame)
                    if y_val > 1.0:
                        y_val = y_val / 1080.0
                    if h_val > 1.0:
                        h_val = h_val / 1080.0
                    
                    y_val = max(0.0, min(0.98, y_val))
                    h_val = max(0.02, min(0.40, h_val))
                    if y_val >= 0.5:
                        valid_subs.append({"start_y_pct": y_val, "height_pct": h_val})
                except Exception:
                    continue
            
            subtitle_found = len(valid_subs) > 0

            if subtitle_found:
                import statistics
                y_values = [float(r["start_y_pct"]) for r in valid_subs]
                h_values = [float(r["height_pct"]) for r in valid_subs]
                
                # Use median to ignore extreme outliers instead of average
                median_y = statistics.median(y_values)
                max_h = max(h_values)  # Use max height to ensure we cover 2-line subtitles if detected
                
                y_start  = max(0.50, min(0.92, median_y - 0.02))
                y_height = max(0.05, min(1.0 - y_start, max_h + 0.04))

                print(
                    f"[OK] VisionAI: Subtitles CONFIRMED ({len(valid_subs)}/{len(detection_results)} frames positive)! "
                    f"Region -> Y: {y_start*100:.1f}%, Height: {y_height*100:.1f}% — Will blur."
                )
                return y_start, y_height, True
            else:
                print(f"[*] VisionAI: No subtitles detected across ENTIRE video ({len(detection_results)} frames tested). Skipping blur.")
                return default_start_y, default_height, False

        except Exception as e:
            print(f"[WARN] VisionAI Subtitle Detector: Detection failed ({e}). Skipping blur to be safe.")
            return default_start_y, default_height, False
        finally:
            # Clean up temp frames
            import shutil
            if os.path.exists(temp_dir):
                try: shutil.rmtree(temp_dir)
                except Exception: pass

    def _blur_subtitle_region(
        self,
        state: MovieState,
        video_path: str,
        source_video_for_detection: str = None,
        region_pct: float = 0.18,
        blur_strength: int = 18,
        color_enabled: bool = False,
        brightness: float = 0.03,
        contrast: float = 1.02,
        saturation: float = 1.08,
        ass_path: str = None,
    ):
        """
        Unified single FFmpeg post-pass that combines up to three post-processing operations:

        Layer 1 — Vision AI Powered Subtitle Blur:
          Uses Gemini Vision AI to detect exact subtitle Y-boundaries from the
          ORIGINAL SOURCE movie (not the recap), crops that exact region,
          applies boxblur, and overlays back. Skipped if AI says no subtitles.

        Layer 2 — Color Grading (eq filter):
          Applies slight brightness / contrast / saturation shift via FFmpeg.

        Layer 3 — Myanmar Subtitle Burn (ASS filter):
          Burns styled Myanmar ASS subtitles in the SAME encode pass.
        Hardware-accelerated via Intel QSV / NVIDIA NVENC with automatic libx264 CPU fallback.
        """
        import subprocess, os

        ffmpeg_bin = _get_ffmpeg_bin()
        if not ffmpeg_bin:
            print("[WARN] PostProcess: ffmpeg not found. Skipping post-pass.")
            return

        do_blur  = blur_strength > 0
        do_color = color_enabled
        do_subtitles = bool(ass_path and os.path.exists(ass_path))

        user_sub_mode = getattr(state, "subtitle_mode", "auto") if state is not None else "auto"
        user_sub_mode = user_sub_mode or "auto"

        if user_sub_mode == "no":
            print("[*] Subtitle Blur: Disabled by user setting ('No Subtitles'). Skipping blur pass.")
            do_blur = False

        # Run Vision AI on ORIGINAL MOVIE to get exact subtitle coordinates
        # (The recap output does NOT have subtitles — always analyze the source)
        start_y_pct, height_pct = 0.82, 0.18
        subtitle_found = False
        if do_blur:
            detect_target = source_video_for_detection if source_video_for_detection and os.path.exists(source_video_for_detection) else video_path

            if user_sub_mode == "yes":
                print("[*] Subtitle Blur: User selected 'Has Subtitles' — Forcing subtitle blur mode on.")
                y, h, found = self._detect_subtitle_region_with_vision(detect_target, state=state)
                start_y_pct = y if found else 0.82
                height_pct = h if found else 0.18
                subtitle_found = True
            else:
                cache = getattr(state, "subtitle_detection", None) if state is not None else None
                # Only reuse cache if it previously FOUND subtitles
                # If cache says no subtitles, always re-detect (Gemini may have been wrong)
                cache_valid = (
                    cache
                    and cache.get("video_path") == os.path.abspath(detect_target)
                    and cache.get("has_subtitles", False) is True  # Only trust positive cache
                )
                if cache_valid:
                    start_y_pct = float(cache.get("start_y_pct", start_y_pct))
                    height_pct = float(cache.get("height_pct", height_pct))
                    subtitle_found = True
                    print("[*] VideoMerger: Reusing cached subtitle detection (subtitles confirmed previously).")
                else:
                    if cache and not cache.get("has_subtitles", False):
                        print("[*] VideoMerger: Previous detection found no subtitles — re-running with improved detector...")
                    start_y_pct, height_pct, subtitle_found = self._detect_subtitle_region_with_vision(detect_target, state=state)
                if state is not None:
                    state.subtitle_detection = {
                        "video_path": os.path.abspath(detect_target),
                        "has_subtitles": subtitle_found,
                        "start_y_pct": start_y_pct,
                        "height_pct": height_pct,
                    }
                if not subtitle_found:
                    print("[*] PostProcess: No subtitles detected in source — skipping blur pass.")
                    do_blur = False

        if not do_blur and not do_color and not do_subtitles:
            print("[*] PostProcess: No blur, color grade, or subtitles requested. Skipping post-processing pass.")
            return

        if do_blur:
            start_y_pct = max(0.0, min(float(start_y_pct), 0.95))
            height_pct = max(0.02, min(float(height_pct), 1.0 - start_y_pct))

        steps = []
        if do_blur:      steps.append(f"VisionAI subtitle blur (Y-start={start_y_pct*100:.1f}%, h={height_pct*100:.1f}%, r={blur_strength})")
        if do_color:     steps.append(f"color grade (b={brightness:+.2f}, c={contrast:.2f}, s={saturation:.2f}), dynamic noise, vignette")
        if do_subtitles: steps.append(f"Myanmar ASS subtitles burn ({os.path.basename(ass_path)})")
        print(f"[*] PostProcess: Applying unified single-pass — {', '.join(steps)}")

        base, ext = os.path.splitext(video_path)
        tmp_path  = base + "_posttmp" + ext

        # ── Build filter_complex ─────────────────────────────────────────
        # Step 1: Fix even dimensions
        base_filter = "crop=w='trunc(iw/2)*2':h='trunc(ih/2)*2'"

        if do_blur:
            r = blur_strength
            # Guarantee even integer pixel boundaries for libx264 / yuv420p encoder
            flt = (
                f"[0:v]{base_filter},split=2[orig][sub];"
                f"[sub]crop=iw:'trunc(ih*{height_pct:.3f}/2)*2':0:'trunc(ih*{start_y_pct:.3f}/2)*2',"
                f"boxblur=luma_radius={r}:luma_power=2"
                f":chroma_radius={max(1,r//2)}:chroma_power=2[blurred];"
                f"[orig][blurred]overlay=0:'trunc(H*{start_y_pct:.3f}/2)*2'[blended]"
            )
            last_out = "[blended]"
        else:
            flt = f"[0:v]{base_filter}[blended]"
            last_out = "[blended]"

        if do_color:
            eq_str = (
                f"eq=brightness={brightness:.3f}"
                f":contrast={contrast:.3f}"
                f":saturation={saturation:.3f}"
                f",noise=alls=2:allf=t"       # Dynamic Film Grain
                f",vignette=PI/4"             # Subtle Edge Darkening
            )
            flt += f";{last_out}{eq_str}[graded]"
            last_out = "[graded]"

        working_dir = None
        if do_subtitles:
            working_dir = os.path.dirname(os.path.abspath(ass_path))
            flt += f";{last_out}{self._build_ass_filter_str(ass_path)}[subbed]"
            last_out = "[subbed]"

        # Map last output label
        filter_args = ["-filter_complex", flt, "-map", last_out]

        enc_info = detect_hardware_encoder()
        codec = enc_info.get("codec", "libx264")
        preset = enc_info.get("preset", "faster")

        quality_args = ["-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M"] if enc_info.get("type") == "gpu" else ["-crf", "20"]
        cmd = [
            ffmpeg_bin, "-y",
            "-i", os.path.abspath(video_path),
            *filter_args,
            "-map", "0:a?",
            "-c:v", codec,
            "-preset", preset,
            *quality_args,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "copy",
            os.path.abspath(tmp_path),
        ]

        # Dynamically scale timeout to allow ample time for full movies
        dur_sec = getattr(state, "duration_sec", 0.0) if state else 0.0
        if not dur_sec and state and getattr(state, "duration", None):
            try:
                parts = str(state.duration).split(":")
                if len(parts) == 3:
                    dur_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except Exception:
                dur_sec = 600.0
        dyn_timeout = max(1200, int((dur_sec or 600.0) * 2.5))

        try:
            print(f"[*] PostProcess: Encoding with {enc_info.get('label', codec)} [{codec}]...")
            result = subprocess.run(
                cmd, cwd=working_dir, capture_output=True, text=True, timeout=dyn_timeout,
                encoding="utf-8", errors="replace"
            )
            if result.returncode != 0 and codec != "libx264":
                print(f"[WARN] PostProcess: Hardware encoder '{codec}' failed. Retrying with CPU libx264...")
                fallback_cmd = list(cmd)
                if codec in fallback_cmd:
                    fallback_cmd[fallback_cmd.index(codec)] = "libx264"
                if preset in fallback_cmd:
                    fallback_cmd[fallback_cmd.index(preset)] = "superfast"
                if "-b:v" in fallback_cmd:
                    b_idx = fallback_cmd.index("-b:v")
                    fallback_cmd = fallback_cmd[:b_idx] + ["-crf", "20"] + fallback_cmd[b_idx+6:]
                result = subprocess.run(
                    fallback_cmd, cwd=working_dir, capture_output=True, text=True, timeout=dyn_timeout,
                    encoding="utf-8", errors="replace"
                )

            if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 100_000:
                os.replace(tmp_path, video_path)
                if do_subtitles and state is not None:
                    state.subtitles_burned = True
                print("[OK] PostProcess: Copyright-safe single-pass post-processing complete!")
            else:
                print(f"[WARN] PostProcess: FFmpeg returned exit code {result.returncode}.")
                if result.stderr:
                    print(f"[WARN] FFmpeg stderr: {result.stderr[-400:]}")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except subprocess.TimeoutExpired:
            print("[WARN] PostProcess: FFmpeg timed out. Original video kept.")
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except Exception: pass
        except Exception as e:
            print(f"[WARN] PostProcess: {e}. Original video kept.")
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except Exception: pass

    # ─────────────────────────────────────────────────────
    # MERGE STEP 8b — decide whether (and where) the old subtitles are blurred
    # ─────────────────────────────────────────────────────

    def _plan_subtitle_blur(self, plan, state, movie_path: str) -> None:
        """Fills plan.do_blur / start_y_pct / height_pct / subtitle_found (uses a cached detection when valid)."""
        user_sub_mode = getattr(state, "subtitle_mode", "auto") if state is not None else "auto"
        user_sub_mode = user_sub_mode or "auto"
        do_blur = plan.blur_enabled and (plan.blur_strength > 0) and (user_sub_mode != "no")
        start_y_pct, height_pct = 0.82, 0.18
        subtitle_found = False
        if do_blur:
            if user_sub_mode == "yes":
                y, h, found = self._detect_subtitle_region_with_vision(movie_path, state=state)
                start_y_pct = y if found else 0.82
                height_pct = h if found else 0.18
                subtitle_found = True
            else:
                cache = getattr(state, "subtitle_detection", None) if state is not None else None
                if cache and cache.get("video_path") == os.path.abspath(movie_path):
                    start_y_pct = float(cache.get("start_y_pct", start_y_pct))
                    height_pct = float(cache.get("height_pct", height_pct))
                    subtitle_found = bool(cache.get("has_subtitles", False))
                else:
                    start_y_pct, height_pct, subtitle_found = self._detect_subtitle_region_with_vision(movie_path, state=state)
                if state is not None:
                    state.subtitle_detection = {
                        "video_path": os.path.abspath(movie_path),
                        "has_subtitles": subtitle_found,
                        "start_y_pct": start_y_pct,
                        "height_pct": height_pct,
                    }
            if not subtitle_found:
                do_blur = False

        plan.do_blur = do_blur
        plan.start_y_pct = start_y_pct
        plan.height_pct = height_pct
        plan.subtitle_found = subtitle_found
