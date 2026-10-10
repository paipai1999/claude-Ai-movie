"""
Myanmar Subtitle Overlay (မြန်မာ စာတန်းထိုး ASS ဖန်တီးခြင်း)
============================================================
ASS generation, Burmese syllable-aware wrapping, font discovery and standalone SRT/ASS export.

ASS ဖိုင်ဖန်တီးခြင်း၊ မြန်မာစာ ဝါကျပိုင်းဖြတ်ခြင်း၊ font ရှာဖွေခြင်းနှင့် SRT/ASS ထုတ်ပေးခြင်း။
"""

from brain.diagnostics import log_suppressed
import os
import sys


class SubtitleOverlayMixin:
    """Mixin for VideoMergerAgent – methods are used through `self`."""

    # ─────────────────────────────────────────────────────
    # MYANMAR SUBTITLE OVERLAY — ASS Generation + FFmpeg Burn
    # ─────────────────────────────────────────────────────

    def _sec_to_ass_ts(self, sec: float) -> str:
        """Convert seconds to ASS timestamp format H:MM:SS.cs (centiseconds)."""
        total_cs = int(round(max(0.0, float(sec)) * 100))
        cs = total_cs % 100
        total_s = total_cs // 100
        s = total_s % 60
        total_m = total_s // 60
        m = total_m % 60
        h = total_m // 60
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def _wrap_burmese_text(self, text: str, max_chars: int) -> str:
        """Wrap long Myanmar text into multiple lines at natural syllable/word boundaries (ASS uses {\\N})."""
        text = str(text or "").strip()
        if len(text) <= max_chars:
            return text

        import re
        # Break text into tokens: split by space, but for segments longer than max_chars,
        # perform Myanmar syllable-level segmentation to prevent horizontal screen overflow.
        pattern = re.compile(
            r'[\u1000-\u102a\u104e]'
            r'[\u102b-\u103e\u1036-\u1038]*'
            r'(?:[\u1039][\u1000-\u1021][\u102b-\u103e\u1036-\u1038]*)*'
            r'(?:[\u1000-\u1021][\u103a][\u1037\u1038]*)*'
            r'|[\u1040-\u1049]+'
            r'|[a-zA-Z0-9]+'
            r'|[^\u1000-\u104f\s]'
        )

        words = text.split(" ")
        refined_tokens = []
        for word in words:
            if len(word) <= max_chars:
                refined_tokens.append(word)
            else:
                last_idx = 0
                for m in pattern.finditer(word):
                    if m.start() > last_idx:
                        refined_tokens.append(word[last_idx:m.start()])
                    refined_tokens.append(m.group(0))
                    last_idx = m.end()
                if last_idx < len(word):
                    refined_tokens.append(word[last_idx:])

        lines, current = [], ""
        for tok in refined_tokens:
            if not tok:
                continue
            # Keep punctuation with current line
            if tok in ["၊", "။", ",", ".", "!", "?"] and current:
                current += tok
                continue
            sep = " " if current and not ('\u1000' <= tok[0] <= '\u109F' and '\u1000' <= current[-1] <= '\u109F') else ""
            candidate = current + sep + tok if current else tok
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = tok
        if current:
            lines.append(current)

        return "\\N".join(lines)

    def _chunk_burmese_narration(self, text: str, max_chars_per_chunk: int = 40) -> list:
        """
        Splits narration text into proportional timing chunks.
        Space-agnostic: if the text lacks spaces (standard Myanmar writing),
        segments syllables using Myanmar phonetic boundaries so subtitles are
        never displayed as one giant unchunked sentence.
        """
        text = str(text or "").strip()
        if not text:
            return []

        import re
        syllable_pattern = re.compile(
            r'[\u1000-\u102a\u104e]'
            r'[\u102b-\u103e\u1036-\u1038]*'
            r'(?:[\u1039][\u1000-\u1021][\u102b-\u103e\u1036-\u1038]*)*'
            r'(?:[\u1000-\u1021][\u103a][\u1037\u1038]*)*'
            r'|[\u1040-\u1049]+'
            r'|[a-zA-Z0-9]+'
            r'|[^\u1000-\u104f\s]'
        )

        words = [w for w in text.split(" ") if w]
        refined_tokens = []
        for word in words:
            if len(word) <= max_chars_per_chunk:
                refined_tokens.append(word)
            else:
                last_idx = 0
                for m in syllable_pattern.finditer(word):
                    if m.start() > last_idx:
                        refined_tokens.append(word[last_idx:m.start()])
                    refined_tokens.append(m.group(0))
                    last_idx = m.end()
                if last_idx < len(word):
                    refined_tokens.append(word[last_idx:])

        if not refined_tokens:
            refined_tokens = [text]

        chunks = []
        current = ""
        for tok in refined_tokens:
            if not tok:
                continue
            if tok in ["၊", "။", ",", ".", "!", "?"] and current:
                current += tok
                continue
            sep = " " if current and not ('\u1000' <= tok[0] <= '\u109F' and '\u1000' <= current[-1] <= '\u109F') else ""
            candidate = current + sep + tok if current else tok
            if len(candidate) <= max_chars_per_chunk:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = tok
        if current:
            chunks.append(current)

        return chunks if chunks else [text]

    def _write_ass(
        self,
        timings: list,
        ass_path: str,
        font_name: str    = "Myanmar Text",
        font_size: int    = 40,
        bold: bool        = True,
        border_style: int = 3,
        outline_width: int = 3,
        margin_bottom: int = 50,
        max_chars: int    = 28,
        preset: str       = None,
    ) -> str:
        """
        Write an ASS (Advanced SubStation Alpha) subtitle file.
        All styling is embedded — no FFmpeg force_style needed.
        ASS colour: &HAABBGGRR (alpha 00=opaque, 80=50% transparent)
        BorderStyle=1: outline+shadow  |  BorderStyle=3: opaque box background
        """
        bold_flag = "-1" if bold else "0"

        # Resolve style preset from SUBTITLE_PRESETS if provided
        shadow_val = 2
        primary_colour = "&H00FFFFFF"
        outline_colour = "&H00000000"

        if preset:
            from brain.config import SUBTITLE_PRESETS
            p_data = SUBTITLE_PRESETS.get(str(preset).lower())
            if p_data:
                font_size = int(p_data.get("font_size", font_size))
                bold_flag = "-1" if p_data.get("bold", bold) else "0"
                border_style = int(p_data.get("border_style", border_style))
                outline_val = int(p_data.get("outline_width", outline_width))
                shadow_val = int(p_data.get("shadow", 2))
                primary_colour = p_data.get("primary_color", "&H00FFFFFF")
                outline_colour = p_data.get("outline_color", "&H00000000")
                back_colour = p_data.get("back_color", "&HB0000000")
                margin_bottom = int(p_data.get("margin_bottom", margin_bottom))
            elif border_style == 3:
                outline_val = 10
                back_colour = "&HB0000000"
            else:
                outline_val = outline_width
                back_colour = "&H90000000"
        elif border_style == 3:
            # Box background mode (YouTube-style): Outline=box_padding, Shadow for depth
            outline_val = 10      # box padding in px
            shadow_val  = 2       # subtle depth shadow
            back_colour = "&HB0000000"   # 70% opacity deep dark box for maximum contrast
        else:
            # Classic outline mode
            outline_val = outline_width
            shadow_val  = 2
            back_colour = "&H90000000"

        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "WrapStyle: 0\n"
            "PlayResX: 1920\n"
            "PlayResY: 1080\n"
            "Collisions: Normal\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{font_name},{font_size},"
            f"{primary_colour},&H000000FF,{outline_colour},{back_colour},"
            f"{bold_flag},0,0,0,100,100,0,0,{border_style},{outline_val},{shadow_val},"
            f"2,10,10,{margin_bottom},1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        dialogue_lines = []
        for (start_sec, duration, text) in timings:
            if not text:
                continue
            text = text.strip()

            # Split long narration into proportional chunks using syllable awareness
            chunks = self._chunk_burmese_narration(text, max_chars_per_chunk=max_chars * 2)
            if not chunks:
                continue

            seg_dur = duration / len(chunks)
            for i, chunk in enumerate(chunks):
                seg_start = start_sec + i * seg_dur
                seg_end   = seg_start + seg_dur - 0.05
                # BUG-M7 Fix: Escape ASS special chars { } to prevent format code injection
                from brain.burmese_utils import strip_trailing_subtitle_punctuation, normalize_standard_burmese_spelling
                norm_chunk = normalize_standard_burmese_spelling(chunk.replace('\\', '').replace('{', '').replace('}', ''))
                safe_chunk = strip_trailing_subtitle_punctuation(norm_chunk)
                ass_text  = self._wrap_burmese_text(safe_chunk, max_chars)
                ts_start  = self._sec_to_ass_ts(seg_start)
                ts_end    = self._sec_to_ass_ts(seg_end)
                dialogue_lines.append(
                    f"Dialogue: 0,{ts_start},{ts_end},Default,,0,0,0,,{ass_text}"
                )

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(header + "\n".join(dialogue_lines) + "\n")

        print(f"[*] MyanmarSubs: ASS written → {ass_path} ({len(dialogue_lines)} subtitle entries)")
        return ass_path

    def _find_myanmar_font(self) -> str:
        """Find a suitable Myanmar Unicode font path for reference/logging."""
        candidates = [
            r"assets/fonts/Padauk.ttf",
            r"assets\fonts\Padauk.ttf",
            r"assets/fonts/Padauk-Regular.ttf",
            r"assets\fonts\Padauk-Regular.ttf",
            r"assets\fonts\NotoSansMyanmar-Regular.ttf",
            r"assets/fonts/NotoSansMyanmar-Regular.ttf",
            # Windows fonts
            r"C:\Windows\Fonts\mmrtext.ttf",     # Myanmar Text (Win10+)
            r"C:\Windows\Fonts\mmrtextb.ttf",
            r"C:\Windows\Fonts\NotoSansMyanmar-Regular.ttf",
            r"C:\Windows\Fonts\Padauk-Regular.ttf",
            # Linux (Colab, Kaggle, Docker) fonts
            "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf",
            "/usr/share/fonts/truetype/padauk/Padauk.ttf",
            "/usr/share/fonts/truetype/padauk/Padauk-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansMyanmar-Regular.otf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return os.path.abspath(path)
        return None

    def _build_ass_filter_str(self, ass_file_path: str) -> str:
        """
        Builds a cross-platform FFmpeg libass filter expression with HarfBuzz complex text shaping
        and explicit fontsdir linking to assets/fonts for reliable Myanmar font rendering.
        """
        ass_fname = os.path.basename(ass_file_path).replace("\\", "/").replace("'", r"\'").replace(":", r"\:")
        fonts_dir = os.path.abspath("assets/fonts").replace("\\", "/").replace("'", r"\'").replace(":", r"\:")
        return f"ass=filename='{ass_fname}':shaping=1:fontsdir='{fonts_dir}'"

    def _export_standalone_srt(self, timings: list, output_dir: str):
        """Exports standalone .srt and .ass subtitle files for YouTube caption upload / VLC player."""
        if not timings:
            return
        srt_path = os.path.join(output_dir, "myanmar_subs.srt")
        ass_path = os.path.join(output_dir, "myanmar_subs.ass")

        def _sec_to_srt_ts(sec: float) -> str:
            total_ms = int(round(max(0.0, float(sec)) * 1000))
            ms = total_ms % 1000
            total_s = total_ms // 1000
            s = total_s % 60
            total_m = total_s // 60
            m = total_m % 60
            h = total_m // 60
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        lines = []
        idx = 1
        for item in timings:
            try:
                start_s = float(item[0])
                dur_s = float(item[1])
                from brain.burmese_utils import strip_trailing_subtitle_punctuation, normalize_standard_burmese_spelling
                norm_txt = normalize_standard_burmese_spelling(str(item[2]).strip())
                txt = strip_trailing_subtitle_punctuation(norm_txt)
                if not txt:
                    continue
                end_s = start_s + dur_s
                lines.append(f"{idx}\n{_sec_to_srt_ts(start_s)} --> {_sec_to_srt_ts(end_s)}\n{txt}\n")
                idx += 1
            except Exception as _exc:
                log_suppressed("agents.merger.subtitles._export_standalone_srt", _exc)

        if lines:
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            print(f"[OK] VideoMerger: Standalone SRT subtitles exported -> {srt_path}")

        # Also write clean ASS file
        try:
            import sys
            font_name = "Myanmar Text" if sys.platform == "win32" else "Padauk"
            self._write_ass(timings, ass_path, font_name=font_name, font_size=40, bold=True, border_style=3, outline_width=3, margin_bottom=50, max_chars=28)
        except Exception as _exc:
            log_suppressed("agents.merger.subtitles._export_standalone_srt", _exc)

    # ─────────────────────────────────────────────────────
    # MERGE STEP 6 — decide whether to burn subtitles and write the ASS file
    # ─────────────────────────────────────────────────────

    def _prepare_subtitles(self, plan, state) -> None:
        """Exports the standalone SRT and (when hardsub is on) writes the ASS used by the burn pass."""
        sub_cfg = plan.config_data.get("subtitle_overlay", {})
        burn_subs = True
        if self.subtitle_mode in ["none", "off", "no"]:
            burn_subs = False
        elif self.subtitle_mode in ["burn", "hardsub", "both", "auto"]:
            burn_subs = True

        target_ass_path = None
        if plan.subtitle_timings:
            self._export_standalone_srt(plan.subtitle_timings, plan.output_dir)
            if burn_subs:
                target_ass_path = os.path.join(plan.temp_dir, f"myanmar_subs_{plan.safe_id}.ass")
                sub_preset = getattr(state, "subtitle_style_preset", None) or sub_cfg.get("style_preset", "box_black")
                font_name = (sub_cfg.get("font_name") or "Myanmar Text") if sys.platform == "win32" else "Padauk"
                print(f"[*] VideoMerger: Preparing Myanmar ASS Subtitles (Style Preset: {sub_preset})...")
                self._write_ass(
                    timings       = plan.subtitle_timings,
                    ass_path      = target_ass_path,
                    font_name     = font_name,
                    font_size     = int(sub_cfg.get("font_size", 40)),
                    bold          = bool(sub_cfg.get("bold", True)),
                    border_style  = int(sub_cfg.get("border_style", 3)),
                    outline_width = int(sub_cfg.get("outline_width", 3)),
                    margin_bottom = int(sub_cfg.get("margin_bottom", 50)),
                    max_chars     = int(sub_cfg.get("max_chars_per_line", 28)),
                    preset        = sub_preset,
                )
            else:
                print("[*] VideoMerger: Subtitle Mode is 'Voiceover Only' (Hardsub disabled). Exported standalone .srt subtitles.")

        plan.burn_subs = burn_subs
        plan.target_ass_path = target_ass_path
