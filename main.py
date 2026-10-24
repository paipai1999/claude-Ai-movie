import argparse
import os
import sys
import shutil
from types import SimpleNamespace
from agents import gemini_tts
from agents.master import MasterAgent
from agents.downloader_agent import DownloaderAgent
from brain.planner import BatchProcessor
from brain import config as cfg
from core.cli import MAIN, add_option_arguments, resolve_args
from core.options import normalize_engine

# Force UTF-8 output on Windows to prevent emoji/Unicode encode errors
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ASCII_ART = r"""
  __  __            _        _____                       
 |  \/  |          (_)      |  __ \                      
 | \  / | _____   ___  ___  | |__) |___  ___ __ _ _ __   
 | |\/| |/ _ \ \ / / |/ _ \ |  _  // _ \/ __/ _` | '_ \  
 | |  | | (_) \ V /| |  __/ | | \ \  __/ (_| (_| | |_) | 
 |_|  |_|\___/ \_/ |_|\___| |_|  \_\___|\___\__,_| .__/  
                                                  |_|     
  100% Free Local AI Pipeline  .  Cost = $0              
"""

def setup_directories():
    for d in ["movies", "outputs", "temp", "voiceover", "assets/voices", "assets/bgm"]:
        os.makedirs(d, exist_ok=True)

def run_interactive_cleanup():
    print("\n=== MOVIE RECAP CLEANUP UTILITY ===")
    print("1. Delete generated outputs (outputs/ folder)")
    print("2. Delete source input videos (movies/ folder)")
    print("3. Clear temporary audio/video cache (temp/ folder)")
    print("0. Exit")
    choice = input("\nSelect an option [0-3]: ").strip()
    
    if choice == "1":
        out_dir = "outputs"
        if not os.path.exists(out_dir) or not os.listdir(out_dir):
            print("[INFO] No outputs found.")
            return
        PROTECTED_SYSTEM_FILES = {"api_usage_db.json", "movie_metadata.db"}
        items = sorted([f for f in os.listdir(out_dir) if f not in PROTECTED_SYSTEM_FILES])
        if not items:
            print("[INFO] No project outputs found to delete.")
            return
        for i, name in enumerate(items, 1):
            print(f"  {i}. {name}")
        sel = input("\nEnter number to delete (or 'all' for everything, 0 to cancel): ").strip().lower()
        if sel == "all":
            confirm = input("Are you sure you want to delete ALL outputs? (y/N): ").lower()
            if confirm == "y":
                def _safe_remove_path(path: str):
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.isfile(path):
                        try: os.remove(path)
                        except Exception: pass

                for name in items:
                    if name in PROTECTED_SYSTEM_FILES:
                        continue
                    _safe_remove_path(os.path.join("outputs", name))
                    _safe_remove_path(os.path.join("temp", name))
                print("[OK] All outputs and associated temp files deleted!")
        elif sel.isdigit() and 1 <= int(sel) <= len(items):
            name = items[int(sel)-1]
            def _safe_remove_path(path: str):
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    try: os.remove(path)
                    except Exception: pass
            _safe_remove_path(os.path.join("outputs", name))
            _safe_remove_path(os.path.join("temp", name))
            print(f"[OK] Deleted output: {name}")
    elif choice == "2":
        mov_dir = "movies"
        if not os.path.exists(mov_dir) or not os.listdir(mov_dir):
            print("[INFO] No input videos found in movies/.")
            return
        items = sorted([f for f in os.listdir(mov_dir) if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv'))])
        if not items:
            print("[INFO] No video files found in movies/.")
            return
        for i, name in enumerate(items, 1):
            print(f"  {i}. {name}")
        sel = input("\nEnter number to delete (or 'all' for everything, 0 to cancel): ").strip().lower()
        if sel == "all":
            confirm = input("Are you sure you want to delete ALL input movies? (y/N): ").lower()
            if confirm == "y":
                for name in items: os.remove(os.path.join("movies", name))
                print("[OK] All input movies deleted!")
        elif sel.isdigit() and 1 <= int(sel) <= len(items):
            name = items[int(sel)-1]
            os.remove(os.path.join("movies", name))
            print(f"[OK] Deleted input video: {name}")
    elif choice == "3":
        if os.path.exists("temp"):
            shutil.rmtree("temp", ignore_errors=True)
            os.makedirs("temp", exist_ok=True)
            print("[OK] Temporary cache cleared!")

def check_dependencies():
    ffmpeg = "/usr/local/bin/ffmpeg" if (os.path.exists("/usr/local/bin/ffmpeg") and os.path.getsize("/usr/local/bin/ffmpeg") > 10000000) else shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe()
        except ImportError:
            ffmpeg = None
    if not ffmpeg or not os.path.exists(ffmpeg):
        print("[CRITICAL ERROR] FFmpeg was not found. Install it or run: pip install imageio-ffmpeg")
        sys.exit(1)
    # MoviePy and the post-processing agent both honor this executable.
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg
    ffmpeg_dir = os.path.dirname(ffmpeg)
    if ffmpeg_dir and ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    print(f"[*] FFmpeg ready -> {ffmpeg}")

def build_parser() -> argparse.ArgumentParser:
    """
    Command line of the studio (Engine 1 / 2 / 3, batch, download, cleanup).

    The studio options (language, style, format, blur ...) are generated from ``core.options`` via
    ``core.cli`` so they are identical in every program; only run-mode switches live here.
    """
    parser = argparse.ArgumentParser(
        description="Movie Recap AI — Free Local Pipeline",
        formatter_class=argparse.RawTextHelpFormatter
    )

    # ── What to run ─────────────────────────────────────────────────────────
    parser.add_argument("input_source", nargs="?", default=None,
                        help="Path to video file or URL to download")
    parser.add_argument("-i", "--input", dest="input_flag", default=None,
                        help="Path to video file or URL to download")
    parser.add_argument("-b", "--batch", action="store_true",
                        help="Process all videos in movies/ folder sequentially")
    parser.add_argument("-u", "--urls", nargs="+", default=None,
                        help="List of URLs to download and process in batch")
    parser.add_argument("--download", "--download-video", dest="download_url", default=None,
                        help="Download video from YouTube or supported URL into movies/ directory and exit")
    parser.add_argument("--download-urls", nargs="+", dest="download_urls", default=None,
                        help="Download multiple video URLs into movies/ directory and exit")
    parser.add_argument("--clean", action="store_true",
                        help="Interactive cleanup menu to delete old source videos or generated outputs")

    # ── Run switches that are not studio options ────────────────────────────
    parser.add_argument("--blocks", type=int, default=None,
                        help="Maximum number of narrative blocks (override config)")
    parser.add_argument("--subtitle", action="store_true",
                        help="Enable subtitle blur pass in the final recap video")
    parser.add_argument("--no-voice", action="store_true",
                        help="Skip Text-to-Speech voice generation step")
    parser.add_argument("--force", action="store_true",
                        help="Re-process movies even if output already exists (ignore skip_completed)")
    parser.add_argument("--no-thumb-title", dest="no_thumb_title", action="store_true", default=False,
                        help="Disable burning title text onto thumbnail (export clean base thumbnail image)")

    # ── Studio options (single definition: core/options.py) ─────────────────
    add_option_arguments(parser, MAIN)
    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Option resolution (CLI flag များကို အသုံးပြုရန် တန်ဖိုးများအဖြစ် ပြောင်းလဲခြင်း)
# ─────────────────────────────────────────────────────────────────────────────
def handle_download_mode(args) -> bool:
    """Dedicated download-only modes (--download / --download-urls). Returns True when handled."""
    resolution = args.resolution or "1080p"
    if args.download_url:
        print(f"\n[DOWNLOADER] Dedicated video download requested for: {args.download_url}")
        try:
            downloader = DownloaderAgent(output_dir="movies")
            downloaded = downloader.download_video(args.download_url, resolution=resolution)
            print(f"\n🎉 [OK] Video downloaded successfully: {downloaded}\n")
        except Exception as e:
            print(f"\n❌ [ERROR] Video download failed: {e}\n")
            sys.exit(1)
        return True

    if args.download_urls:
        print(f"\n[DOWNLOADER] Dedicated batch download requested for {len(args.download_urls)} video(s)...")
        downloader = DownloaderAgent(output_dir="movies")
        success_count = 0
        for idx, url in enumerate(args.download_urls, 1):
            print(f"\n[{idx}/{len(args.download_urls)}] Downloading: {url}")
            try:
                downloaded = downloader.download_video(url, resolution=resolution)
                print(f"  -> Saved: {downloaded}")
                success_count += 1
            except Exception as e:
                print(f"  -> [ERROR] Failed to download {url}: {e}")
        print(f"\n🎉 [OK] Downloaded {success_count}/{len(args.download_urls)} video(s) into movies/ directory.\n")
        return True
    return False


def apply_environment_flags(args) -> str:
    """Exports flags that the pipeline agents read from the environment. Returns the chosen video format."""
    chosen_format = args.video_format
    if args.reels_enabled is False:
        chosen_format = "16:9"
        os.environ["DISABLE_REELS"] = "true"
    elif args.reels_enabled is True:
        if not chosen_format:
            chosen_format = "both"
        os.environ["ENABLE_REELS"] = "true"

    if args.no_voice:
        os.environ["VOICE_ENABLED"] = "false"

    if args.blocks is not None:
        os.environ["MAX_BLOCKS"] = str(args.blocks)

    if args.subtitle:
        os.environ["ENABLE_SUBTITLES"] = "true"

    if args.skip_demucs:
        os.environ["SKIP_DEMUCS"] = "true"

    if args.detect_scenes is True:
        os.environ["SKIP_SCENES"] = "0"
    elif args.detect_scenes is False:
        os.environ["SKIP_SCENES"] = "1"
    return chosen_format


def resolve_language_and_voice(args):
    """Maps the --lang / --voice shortcuts to (clean_language, edge_tts_voice)."""
    clean_lang = args.language
    chosen_voice = args.tts_voice
    if clean_lang in ["burmese_nilar", "nilar"]:
        clean_lang = "burmese"
        if not chosen_voice:
            chosen_voice = "my-MM-NilarNeural"
    elif clean_lang in ["burmese_thiha", "thiha"]:
        clean_lang = "burmese"
        if not chosen_voice:
            chosen_voice = "my-MM-ThihaNeural"
    elif clean_lang in gemini_tts.SHORTCUTS:
        if not chosen_voice:
            chosen_voice = gemini_tts.SHORTCUTS[clean_lang]
        clean_lang = "burmese"
    elif clean_lang in ["mm", "myanmar"]:
        clean_lang = "burmese"
    elif clean_lang in ["en"]:
        clean_lang = "english"
    elif chosen_voice in ["nilar", "female"]:
        chosen_voice = "my-MM-NilarNeural"
    elif chosen_voice in ["thiha", "male"]:
        chosen_voice = "my-MM-ThihaNeural"
    return clean_lang, gemini_tts.canonical_voice(chosen_voice)


def resolve_run_options(args) -> SimpleNamespace:
    """
    Every value the three engines need, resolved ONCE for the engine selected with --engine-mode
    (defaults come from ``core.options``; recap options owned by config.json stay ``None``).
    """
    engine = normalize_engine(args.engine_mode)
    chosen_format = apply_environment_flags(args)
    clean_lang, chosen_voice = resolve_language_and_voice(args)
    values = resolve_args(MAIN, args, engine)

    # --reels / --no-reels / --format decide the format before the engine default does.
    if chosen_format:
        values["video_format"] = chosen_format
    # --subtitle means "there are burnt-in subtitles to remove" unless --blur-mode was typed.
    if args.blur_mode is None and args.subtitle:
        values["blur_mode"] = "yes"
    if args.subtitle:
        values["subtitle_mode"] = "burn"

    return SimpleNamespace(
        engine=engine,
        values=values,
        chosen_format=values["video_format"],
        clean_lang=clean_lang,
        chosen_voice=chosen_voice,
        thumb_title="none" if getattr(args, "no_thumb_title", False) else values["custom_thumb_title"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Keyword-argument builders (Engine တစ်ခုချင်းစီ၏ parameter များ)
# ─────────────────────────────────────────────────────────────────────────────
_HARDSUB_KEYS = (
    "project_name", "source_language", "force_whisper", "video_format", "resolution",
    "subtitle_style", "blur_mode", "mirror", "color_grading", "blur_height",
    "audio_anti_copyright", "translation_style", "audio_mode", "sfx_mode", "sfx_volume",
    "render_video", "context_hint", "delivery_mode",
)
_SUBTITLE_KEYS = (
    "project_name", "source_language", "force_whisper", "translation_style", "audio_mode",
    "sfx_mode", "sfx_volume", "render_video", "video_format", "resolution", "subtitle_style",
    "blur_mode", "mirror", "color_grading", "blur_height", "audio_anti_copyright", "context_hint", "delivery_mode",
)


def hardsub_run_kwargs(args, opts) -> dict:
    """Engine 3 (Hardsub Studio) `run()` options – identical for single and batch mode."""
    return {k: opts.values[k] for k in _HARDSUB_KEYS}


def subtitle_run_kwargs(args, opts) -> dict:
    """Engine 2 (Subtitle Studio) `run()` options – identical for single and batch mode."""
    return {k: opts.values[k] for k in _SUBTITLE_KEYS}


def recap_common_kwargs(args, opts) -> dict:
    """Engine 1 (Recap Studio) options shared by MasterAgent and BatchProcessor."""
    v = opts.values
    return dict(
        language=opts.clean_lang,
        subtitle_mode=v["subtitle_mode"],
        subtitle_style=v["subtitle_style"],
        resolution=v["resolution"],
        tts_engine=v["tts_engine"],
        tts_voice=opts.chosen_voice,
        custom_thumb_title=opts.thumb_title,
        watermark_enabled=v["watermark_enabled"],
        watermark_text=v["watermark_text"],
        watermark_opacity=v["watermark_opacity"],
        video_format=opts.chosen_format,
        thumbnail_intro=v["thumbnail_intro"],
        source_language=v["source_language"],
        script_engine=v["script_engine"],
        resume=v["resume"],
        skip_demucs=v["skip_demucs"],
        detect_scenes=v["detect_scenes"],
        translation_style=v["translation_style"],
        audio_mode=v["audio_mode"],
        sfx_mode=v["sfx_mode"],
        sfx_volume=v["sfx_volume"],
        blur_mode=v["blur_mode"],
        blur_height=v["blur_height"],
        mirror=v["mirror"],
        audio_anti_copyright=v["audio_anti_copyright"],
        render_video=v["render_video"],
        context_hint=v["context_hint"],
        story_length=v["story_length"],
        narrative_tone=v["narrative_tone"],
        tts_rate=v["tts_rate"],
        delivery_mode=v["delivery_mode"],
    )


def _list_batch_items(args, movies_dir: str) -> list:
    """URLs given with --urls, otherwise every video file inside movies/."""
    if args.urls:
        return list(args.urls)
    if os.path.exists(movies_dir):
        valid_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv')
        return [os.path.join(movies_dir, f) for f in sorted(os.listdir(movies_dir)) if f.lower().endswith(valid_exts)]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Run modes (Single video / Batch)
# ─────────────────────────────────────────────────────────────────────────────
def run_single(args, opts, src: str) -> None:
    """One local file or URL through the engine selected with --engine-mode."""
    if DownloaderAgent.is_url(src):
        print("[URL] Detected URL - starting auto-download...")
        try:
            downloader = DownloaderAgent(output_dir="movies")
            movie_path = downloader.download_video(src)
        except Exception as e:
            print(f"[ERROR] Download failed: {e}")
            sys.exit(1)
    else:
        movie_path = src
        if not os.path.exists(movie_path):
            print(f"[ERROR] File not found: '{movie_path}'")
            print("[TIP] If this is a URL, make sure it starts with http:// or https://")
            sys.exit(1)

    if opts.engine == "hardsub":
        from hardsub_engine import HardsubEngine
        HardsubEngine().run(input_source=movie_path, **hardsub_run_kwargs(args, opts))
        return
    if opts.engine == "subtitle":
        from subtitle_engine import SubtitleEngine
        SubtitleEngine().run(input_source=movie_path, **subtitle_run_kwargs(args, opts))
        return

    try:
        master = MasterAgent(
            movie_path,
            trim_end=opts.values["trim_end"],
            no_smart_trim=opts.values["no_smart_trim"],
            outro_card=opts.values["outro_card"],
            **recap_common_kwargs(args, opts),
        )
        master.run_pipeline()
    except Exception as e:
        print(f"\n[ERROR] Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def run_batch(args, opts) -> None:
    """--batch (movies/ folder) or --urls, for any of the three engines."""
    conf = cfg.load_config()
    movies_dir = conf.get("batch", {}).get("movies_folder", "movies")
    skip = conf.get("batch", {}).get("skip_completed", True) and not args.force

    if opts.engine in ("hardsub", "subtitle"):
        if opts.engine == "hardsub":
            from hardsub_engine import HardsubEngine
            engine, label, run_kwargs = HardsubEngine(), "HARDSUB", hardsub_run_kwargs(args, opts)
        else:
            from subtitle_engine import SubtitleEngine
            engine, label, run_kwargs = SubtitleEngine(), "SUBTITLE", subtitle_run_kwargs(args, opts)

        items = _list_batch_items(args, movies_dir)
        print(f"[BATCH {label}] Starting batch of {len(items)} item(s)...")
        for idx, item in enumerate(items, 1):
            print(f"\n{'='*65}\n[BATCH {label}] Item {idx}/{len(items)}: {item}\n{'='*65}")
            try:
                engine.run(input_source=item, **run_kwargs)
            except Exception as err:
                print(f"[ERROR] Batch item {idx} failed: {err}")
        return

    processor = BatchProcessor(movies_folder=movies_dir, skip_completed=skip, **recap_common_kwargs(args, opts))
    if args.batch:
        print("[BATCH] Processing all videos in movies/ folder...")
        processor.process_all()
    elif args.urls:
        print(f"[BATCH] URL Batch Mode: {len(args.urls)} video(s) to download & process...")
        processor.process_all(url_list=args.urls, local_paths=[])


def main():
    check_dependencies()
    print(ASCII_ART)

    parser = build_parser()
    args = parser.parse_args()
    setup_directories()

    if args.clean:
        run_interactive_cleanup()
        return

    if handle_download_mode(args):
        return

    opts = resolve_run_options(args)

    chosen_input = (args.input_flag or args.input_source or "").strip()
    if chosen_input:
        run_single(args, opts, chosen_input)
    elif args.batch or args.urls:
        run_batch(args, opts)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
