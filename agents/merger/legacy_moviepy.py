"""
Legacy MoviePy Merge Fallback (MoviePy ဖြင့် ပေါင်းစပ်သော fallback နည်းလမ်း)
============================================================================
Slower MoviePy-based compositor used only when the single-pass FFmpeg filtergraph fails.

FFmpeg single-pass မအောင်မြင်ပါက သုံးသော MoviePy fallback ပေါင်းစပ်စနစ်။
"""

from brain.diagnostics import log_suppressed
import os
import shutil

from agents.merger.encoder import detect_hardware_encoder


class LegacyMoviepyMixin:
    """Mixin for VideoMergerAgent – methods are used through `self`."""

    def _legacy_moviepy_merge(
        self,
        state,
        movie_path: str,
        output_dir: str,
        final_output: str,
        persistent_clean_path: str,
        clean_video_path: str,
        config_data: dict,
        burn_subs: bool = True,
        target_ass_path: str = None,
        subtitle_timings: list = None,
    ):
        """Legacy MoviePy Multi-Pass merge safety net (used only if FFmpeg Single-Pass fails)."""
        print("[*] VideoMerger: Falling back to Legacy Multi-Pass Export...")
        try:
            from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip
        except ImportError:
            try:
                from moviepy import VideoFileClip, AudioFileClip, CompositeAudioClip
            except ImportError:
                print("[ERROR] VideoMerger: moviepy is not installed for legacy fallback. Run: pip install moviepy")
                return state

        main_video = None
        sfx_only_audio = None
        intro_clip = None
        final_clip = None
        audio_clips = []
        positioned_clips = []

        try:
            copyright_cfg   = config_data.get("copyright_protection", {})
            copyright_enabled = copyright_cfg.get("enabled", True)
            blur_cfg        = config_data.get("subtitle_blur", {})
            blur_enabled    = blur_cfg.get("enabled", True)
            if self.subtitle_blur_override:
                blur_enabled = True
            blur_region_pct = float(blur_cfg.get("region_height_pct", 0.18))
            blur_strength   = int(blur_cfg.get("blur_strength", 18))
            color_cfg       = config_data.get("color_grading", {})
            color_enabled   = color_cfg.get("enabled", True)
            cg_brightness   = float(color_cfg.get("brightness", 0.03))
            cg_contrast     = float(color_cfg.get("contrast", 1.02))
            cg_saturation   = float(color_cfg.get("saturation", 1.08))

            voiceover_dir = os.path.join(output_dir, "voiceover")
            script_blocks = getattr(state, "generated_script", []) or []
            script_blocks_with_audio = []
            if os.path.exists(voiceover_dir):
                try:
                    script_blocks.sort(key=lambda x: float(x.get("start_sec") or 0.0) if isinstance(x, dict) else 0.0)
                except Exception as e:
                    print(f"[WARN] Failed to sort script blocks: {e}")

                for sorted_idx, b in enumerate(script_blocks):
                    if not isinstance(b, dict):
                        continue
                    fname = f"scene_{(sorted_idx+1):04d}.mp3"
                    fpath = os.path.join(voiceover_dir, fname)
                    if not os.path.exists(fpath):
                        for root, _, files in os.walk(voiceover_dir):
                            if fname in files:
                                fpath = os.path.join(root, fname)
                                break
                    if os.path.exists(fpath):
                        try:
                            audio_clips.append(AudioFileClip(fpath))
                            script_blocks_with_audio.append(b)
                        except Exception as e:
                            print(f"[WARN] VideoMerger: Failed to load voiceover {fname}: {e}")

            script_blocks = script_blocks_with_audio
            main_video = VideoFileClip(movie_path)

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
                    break

            if no_vocals_path and os.path.exists(no_vocals_path):
                has_no_vocals = True
                try:
                    sfx_only_audio = AudioFileClip(no_vocals_path)
                    if hasattr(main_video, "with_audio"):
                        main_video = main_video.with_audio(sfx_only_audio)
                    else:
                        main_video = main_video.set_audio(sfx_only_audio)
                except Exception as e:
                    print(f"[WARN] VideoMerger: Failed to load no_vocals.wav: {e}")

            w, h = main_video.size
            new_w = w if w % 2 == 0 else w - 1
            new_h = h if h % 2 == 0 else h - 1
            if (new_w, new_h) != (w, h):
                try:
                    try:
                        from moviepy.video.fx.Crop import Crop
                        main_video = main_video.with_effects([Crop(x1=0, y1=0, x2=new_w, y2=new_h)])
                    except ImportError:
                        import moviepy.video.fx.all as vfx
                        main_video = main_video.fx(vfx.crop, x1=0, y1=0, x2=new_w, y2=new_h)
                except Exception as e:
                    print(f"[WARN] Failed to crop odd dimensions: {e}")

            if copyright_enabled:
                effects = []
                mirror_enabled = copyright_cfg.get("mirror_video", False)
                if mirror_enabled:
                    try:
                        try:
                            from moviepy.video.fx.MirrorX import MirrorX
                            effects.append(MirrorX())
                        except Exception:
                            import moviepy.video.fx.all as vfx
                            main_video = main_video.fx(vfx.mirror_x)
                    except Exception as e:
                        print(f"[WARN] Failed to apply mirror effect: {e}")

                resize_factor = float(copyright_cfg.get("resize_factor", 1.02))
                if resize_factor != 1.0:
                    try:
                        try:
                            from moviepy.video.fx.Resize import Resize
                            effects.append(Resize(resize_factor))
                        except Exception:
                            import moviepy.video.fx.all as vfx
                            main_video = main_video.fx(vfx.resize, resize_factor)
                    except Exception as e:
                        print(f"[WARN] Failed to apply resize: {e}")

                try:
                    try:
                        from moviepy.video.fx.FadeIn import FadeIn
                        from moviepy.video.fx.FadeOut import FadeOut
                        effects.append(FadeIn(1.0))
                        effects.append(FadeOut(1.0))
                    except Exception:
                        import moviepy.video.fx.all as vfx
                        main_video = main_video.fx(vfx.fadein, 1.0).fx(vfx.fadeout, 1.0)
                except Exception as e:
                    print(f"[WARN] Failed to apply fade transitions: {e}")

                if effects and hasattr(main_video, "with_effects"):
                    try:
                        main_video = main_video.with_effects(effects)
                    except Exception as _exc:
                        log_suppressed("agents.merger.legacy_moviepy._legacy_moviepy_merge", _exc)

            if audio_clips:
                video_dur = main_video.duration
                n_blocks = len(audio_clips)
                has_exact_timestamps = (
                    len(script_blocks) > 0
                    and isinstance(script_blocks[0], dict)
                    and "start_sec" in script_blocks[0]
                )
                starts = []
                if has_exact_timestamps:
                    for b in script_blocks:
                        s = float(b.get("start_sec", 0.0))
                        if s > video_dur - 1.0:
                            s = max(0.0, video_dur - 2.0)
                        starts.append(s)
                else:
                    starts = [0.2 + (idx / max(n_blocks - 1, 1)) * (video_dur - 0.4) for idx in range(n_blocks)]

                curr_t = 0.0
                legacy_subtitle_timings = []
                for idx, c in enumerate(audio_clips):
                    orig_start = starts[idx]
                    if orig_start >= curr_t - 0.5:
                        place_time = orig_start
                    else:
                        place_time = curr_t
                    if place_time >= video_dur:
                        break
                    if hasattr(c, "with_start"):
                        positioned_clips.append(c.with_start(place_time))
                    else:
                        positioned_clips.append(c.set_start(place_time))
                    curr_t = place_time + c.duration
                    if idx < len(script_blocks):
                        b = script_blocks[idx]
                        narration_text = b.get("narration", "").strip() if isinstance(b, dict) else ""
                        legacy_subtitle_timings.append((place_time, c.duration, narration_text))

                if not subtitle_timings:
                    subtitle_timings = legacy_subtitle_timings
                state.subtitle_timings = subtitle_timings

                duck_cfg = config_data.get("audio_ducking", {})
                duck_enabled = duck_cfg.get("enabled", True)
                duck_vol = float(duck_cfg.get("duck_volume", 0.12))
                ambient_vol = float(duck_cfg.get("ambient_volume", 0.35))

                orig_audio = main_video.audio
                if orig_audio is not None and duck_enabled and positioned_clips:
                    try:
                        import numpy as np
                        raw_intervals = [
                            (max(0.0, float(getattr(c, "start", 0.0) or 0.0) - 0.25),
                             float(getattr(c, "start", 0.0) or 0.0) + float(getattr(c, "duration", 0.0) or 0.0) + 0.25)
                            for c in positioned_clips
                        ]
                        raw_intervals.sort(key=lambda x: x[0])
                        merged_intervals = []
                        for s, e in raw_intervals:
                            if not merged_intervals or merged_intervals[-1][1] < s:
                                merged_intervals.append([s, e])
                            else:
                                merged_intervals[-1][1] = max(merged_intervals[-1][1], e)

                        def duck_transform(get_frame, t):
                            frame = get_frame(t)
                            if np.isscalar(t):
                                is_speech = any(s <= t <= e for s, e in merged_intervals)
                                return (duck_vol if is_speech else ambient_vol) * frame
                            else:
                                t_min, t_max = t[0], t[-1]
                                vol = np.full(t.shape[0], ambient_vol, dtype=np.float32)
                                for s, e in merged_intervals:
                                    if e < t_min: continue
                                    if s > t_max: break
                                    vol[(t >= s) & (t <= e)] = duck_vol
                                if hasattr(frame, 'ndim') and frame.ndim == 2:
                                    return vol[:, np.newaxis] * frame
                                return vol * frame

                        orig_audio = orig_audio.transform(duck_transform)
                    except Exception as _exc:
                        log_suppressed("agents.merger.legacy_moviepy._legacy_moviepy_merge", _exc)

                bgm_clip = None
                if not has_no_vocals or getattr(state, "skip_demucs", False):
                    orig_audio = None
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

                    if bgm_track and os.path.exists(bgm_track):
                        try:
                            raw_bgm = AudioFileClip(bgm_track)
                            try:
                                from moviepy.audio.fx.AudioLoop import AudioLoop
                                from moviepy.audio.fx.MultiplyVolume import MultiplyVolume
                                bgm_clip = raw_bgm.with_effects([AudioLoop(duration=main_video.duration), MultiplyVolume(0.18)])
                            except Exception:
                                import moviepy.audio.fx.all as afx
                                bgm_clip = afx.audio_loop(raw_bgm, duration=main_video.duration)
                                bgm_clip = afx.volumex(bgm_clip, 0.18)
                        except Exception as _exc:
                            log_suppressed("agents.merger.legacy_moviepy._legacy_moviepy_merge", _exc)

                audio_components = []
                if orig_audio is not None:
                    audio_components.append(orig_audio)
                if bgm_clip is not None:
                    audio_components.append(bgm_clip)
                audio_components.extend(positioned_clips)

                final_audio = CompositeAudioClip(audio_components)
                if hasattr(main_video, "with_audio"):
                    main_video = main_video.with_audio(final_audio)
                else:
                    main_video = main_video.set_audio(final_audio)

                final_clip = main_video
            else:
                final_clip = main_video

            thumb_intro_cfg = config_data.get("thumbnail_intro", {})
            thumb_intro_enabled = getattr(state, "thumbnail_intro_enabled", None)
            if thumb_intro_enabled is None:
                thumb_intro_enabled = thumb_intro_cfg.get("enabled", False)
            thumb_duration = float(thumb_intro_cfg.get("duration_sec", 3.0))
            thumbnail_path = os.path.join(output_dir, "thumbnail.jpg")
            has_thumb_intro = thumb_intro_enabled and os.path.exists(thumbnail_path)

            if has_thumb_intro:
                try:
                    try:
                        from moviepy import ImageClip, concatenate_videoclips
                    except ImportError:
                        from moviepy.editor import ImageClip, concatenate_videoclips
                    intro_clip = ImageClip(thumbnail_path)
                    if hasattr(intro_clip, "with_duration"):
                        intro_clip = intro_clip.with_duration(thumb_duration)
                    else:
                        intro_clip = intro_clip.set_duration(thumb_duration)
                    fps_val = final_clip.fps if final_clip.fps else 24
                    if hasattr(intro_clip, "with_fps"):
                        intro_clip = intro_clip.with_fps(fps_val)
                    else:
                        intro_clip.fps = fps_val
                    w, h = intro_clip.size
                    new_w, new_h = final_clip.size
                    if (w, h) != (new_w, new_h):
                        try:
                            from moviepy.video.fx.Resize import Resize
                            intro_clip = intro_clip.with_effects([Resize((new_w, new_h))])
                        except Exception as _exc:
                            log_suppressed("agents.merger.legacy_moviepy._legacy_moviepy_merge", _exc)
                    final_clip = concatenate_videoclips([intro_clip, final_clip], method="compose")
                    if subtitle_timings:
                        subtitle_timings = [(start + thumb_duration, dur, txt) for (start, dur, txt) in subtitle_timings]
                        state.subtitle_timings = subtitle_timings
                except Exception as e:
                    print(f"[WARN] VideoMerger: Failed to stitch thumbnail intro: {e}")

            enc_info = detect_hardware_encoder()
            print(f"[*] VideoMerger (Hardware Acceleration): Exporting legacy video using {enc_info['label']} [{enc_info['codec']}]...")
            try:
                final_clip.write_videofile(
                    final_output,
                    codec=enc_info["codec"],
                    audio_codec='aac',
                    bitrate='4500k',
                    preset=enc_info.get("preset", "faster"),
                    threads=4,
                    ffmpeg_params=["-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
                    logger='bar'
                )
            except Exception as enc_err:
                print(f"[WARN] Hardware encoder '{enc_info['codec']}' failed: {enc_err}. Falling back to CPU libx264...")
                final_clip.write_videofile(
                    final_output,
                    codec='libx264',
                    audio_codec='aac',
                    bitrate='4500k',
                    preset='superfast',
                    threads=4,
                    ffmpeg_params=["-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
                    logger='bar'
                )

            try:
                shutil.copy2(final_output, clean_video_path)
                shutil.copy2(final_output, persistent_clean_path)
                state.clean_video_path = persistent_clean_path
            except Exception as ce:
                print(f"[WARN] VideoMerger: Could not cache clean video copy: {ce}")
                state.clean_video_path = clean_video_path

            need_post_pass = blur_enabled or color_enabled or (burn_subs and target_ass_path and os.path.exists(target_ass_path))
            if need_post_pass:
                print("[*] VideoMerger: Running Single-Pass Hardware-Accelerated Post-Processing...")
                self._blur_subtitle_region(
                    state,
                    final_output,
                    source_video_for_detection = movie_path,
                    region_pct    = blur_region_pct    if blur_enabled  else 0.0,
                    blur_strength = blur_strength      if blur_enabled  else 0,
                    color_enabled = color_enabled,
                    brightness    = cg_brightness,
                    contrast      = cg_contrast,
                    saturation    = cg_saturation,
                    ass_path      = target_ass_path    if (burn_subs and target_ass_path and os.path.exists(target_ass_path)) else None,
                )
        except Exception as e:
            print(f"[ERROR] VideoMerger: Failed during legacy video merge/writing: {e}")
        finally:
            for clip_obj in [main_video, sfx_only_audio, intro_clip, final_clip]:
                if clip_obj is not None:
                    try: clip_obj.close()
                    except Exception: pass
            for ac in audio_clips:
                try: ac.close()
                except Exception: pass
            for pc in positioned_clips:
                try: pc.close()
                except Exception: pass

        return state
