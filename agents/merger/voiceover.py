"""
Voiceover Clip Discovery, Timeline Layout & Track Assembly
(Voiceover clip ရှာဖွေခြင်း၊ အချိန်ဇယားချခြင်းနှင့် အသံ track ပေါင်းစပ်ခြင်း)
=====================================================================================
Steps 2 - 4 of the merge: find the synthesized scene clips, anchor every clip to its scene
timestamp (zero cumulative drift), decide how long the output video is (smart outro cut),
and assemble one linear PCM voiceover track.

Merge ၏ အဆင့် ၂ မှ ၄ - scene clip များကို ရှာဖွေ၊ မူရင်း timestamp သို့ ချိတ်ဆက်၊ output ကြာချိန်ကို
ဆုံးဖြတ်ပြီး voiceover track တစ်ခုအဖြစ် ပေါင်းစပ်သည်။
"""

import os

from agents.merger.media_utils import _get_audio_duration, _assemble_voiceover_track
from agents.merger.plan import MergePlan


class VoiceoverLayoutMixin:
    """Mixin for VideoMergerAgent – fills the voiceover/timing part of a MergePlan."""

    # ── 2. Discover Speech Clips & Fast Duration Extraction ───────────────
    def _collect_voiceover_clips(self, plan: MergePlan, state) -> None:
        audio_items = []
        script_blocks = getattr(state, "generated_script", []) or []
        voiceover_dir = os.path.join(plan.output_dir, "voiceover")
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
                    dur = _get_audio_duration(fpath)
                    if dur > 0:
                        audio_items.append((sorted_idx, fpath, dur, b))
                    else:
                        print(f"[WARN] VideoMerger: Audio file {fname} is empty or unreadable.")
                else:
                    print(f"[WARN] VideoMerger: Missing audio file {fname} for script block.")

        plan.script_blocks = script_blocks
        plan.audio_items = audio_items

    # ── 3. Absolute Scene-Anchor Sync Engine ──────────────────────────────
    def _layout_voiceover_clips(self, plan: MergePlan, state) -> None:
        audio_items = plan.audio_items
        script_blocks = plan.script_blocks
        video_dur = plan.video_dur

        subtitle_timings = []
        clips_with_timing = []
        n_blocks = len(audio_items)
        curr_t = 0.0
        if n_blocks > 0:
            print(f"[*] VideoMerger: Laying out {n_blocks} audio blocks across synced {video_dur:.1f}s video...")
            has_exact_timestamps = (
                len(script_blocks) > 0
                and isinstance(script_blocks[0], dict)
                and "start_sec" in script_blocks[0]
            )
            starts = []
            if has_exact_timestamps:
                print("[*] VideoMerger: Using EXACT Gemini timestamps for perfect audio sync.")
                for _, _, _, b in audio_items:
                    s = float(b.get("start_sec", 0.0))
                    if video_dur > 0 and s > video_dur - 1.0:
                        s = max(0.0, video_dur - 2.0)
                    starts.append(s)
            else:
                print("[WARN] No exact timestamps. Falling back to proportional dubbing mode.")
                starts = [0.2 + (idx / max(n_blocks - 1, 1)) * max(1.0, video_dur - 0.4) for idx in range(n_blocks)]

            for idx, (s_idx, fpath, dur, b) in enumerate(audio_items):
                orig_start = starts[idx]
                # True Scene-Anchor Sync:
                # If the previous clip overran slightly (<= 0.5s), anchor directly to orig_start.
                # Overlap-add audio mixing cleanly blends speech tails with zero cumulative drift.
                # If overrun is larger, place at curr_t, but whenever a scene gap occurs,
                # timing instantly snaps back to orig_start (0.000s drift).
                if orig_start >= curr_t - 0.5:
                    place_time = orig_start
                else:
                    place_time = curr_t

                if video_dur > 0 and place_time >= video_dur:
                    print(f"[*] VideoMerger: Audio clip {idx+1} falls past video duration ({video_dur:.1f}s), trimming remaining clips.")
                    break

                clips_with_timing.append((fpath, place_time, dur))
                curr_t = place_time + dur

                narration_text = b.get("narration", "").strip() if isinstance(b, dict) else ""
                subtitle_timings.append((place_time, dur, narration_text))

        state.subtitle_timings = subtitle_timings
        plan.clips_with_timing = clips_with_timing
        plan.subtitle_timings = subtitle_timings
        plan.curr_t = curr_t

    # ── 3b. Smart Outro & Subscribe Protection ───────────────────────────
    def _resolve_render_duration(self, plan: MergePlan, state) -> None:
        video_dur = plan.video_dur
        clips_with_timing = plan.clips_with_timing

        outro_cfg = plan.config_data.get("outro_protection", {})
        auto_trim_enabled = bool(outro_cfg.get("auto_trim", True)) and not getattr(state, "no_smart_trim", False)
        manual_trim_sec = float(getattr(state, "trim_end", None) or outro_cfg.get("trim_end_seconds", 0.0) or 0.0)
        plan.anti_sub_zoom_enabled = bool(outro_cfg.get("anti_subscribe_zoom", True))
        plan.has_outro_card = bool(getattr(state, "outro_card", False) or outro_cfg.get("outro_card", False))

        effective_video_dur = video_dur
        if manual_trim_sec > 0.0 and video_dur > manual_trim_sec + 5.0:
            effective_video_dur = max(5.0, video_dur - manual_trim_sec)
            print(f"[OK] VideoMerger: Manual Outro Trim applied (-{manual_trim_sec:.1f}s) -> Render length: {effective_video_dur:.1f}s")
        elif auto_trim_enabled and clips_with_timing and video_dur > 15.0:
            last_narration_end = max([place_time + dur for _, place_time, dur in clips_with_timing])
            trailing_gap = video_dur - last_narration_end
            if trailing_gap > 3.0:
                # Add 2.0s graceful buffer after final spoken word, discarding trailing channel outro/subscribe cards
                effective_video_dur = min(video_dur, last_narration_end + 2.0)
                print(f"[OK] VideoMerger (Smart Outro Cut): Trimmed trailing outro from {video_dur:.1f}s -> {effective_video_dur:.1f}s (Cleaned {trailing_gap:.1f}s of source outro).")
        plan.effective_video_dur = effective_video_dur

    # ── 4. Linear PCM Voiceover Track Assembly in C-speed (~2s) ───────────
    def _assemble_voiceover(self, plan: MergePlan) -> None:
        plan.has_voiceover = False
        if plan.clips_with_timing:
            print(f"[*] VideoMerger: Fast-assembling linear voiceover PCM track ({len(plan.clips_with_timing)} clips) across {plan.effective_video_dur:.1f}s in C-memory...")
            try:
                target_len = max(plan.effective_video_dur, plan.curr_t)
                _assemble_voiceover_track(plan.clips_with_timing, target_len, plan.assembled_vo_path)
                if os.path.exists(plan.assembled_vo_path) and os.path.getsize(plan.assembled_vo_path) > 1000:
                    plan.has_voiceover = True
                    print("[OK] VideoMerger: Voiceover linear PCM track assembled in ~2 seconds.")
            except Exception as vo_err:
                print(f"[WARN] VideoMerger: Voiceover assembly notice: {vo_err}")
