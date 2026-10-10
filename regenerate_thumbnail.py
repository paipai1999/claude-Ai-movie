from brain.diagnostics import log_suppressed
import os
import sys
import argparse
from brain.memory import MovieState
from agents.thumbnail_agent import ThumbnailAgent


def regenerate_thumbnail(target_input: str, custom_title: str = None, output_dir: str = "outputs"):
    """
    Regenerates a high-quality stylized thumbnail for any movie project.
    Can be invoked via CLI:
        python regenerate_thumbnail.py <movie_name_or_file> [--title "Custom Title"]
    """
    # 1. Resolve movie name and paths
    clean_target = target_input.strip().strip('"').strip("'")
    if clean_target.endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".m4v")):
        movie_name = os.path.splitext(os.path.basename(clean_target))[0]
        video_path = clean_target if os.path.exists(clean_target) else os.path.join("movies", os.path.basename(clean_target))
    else:
        movie_name = os.path.basename(clean_target.rstrip("/\\"))
        video_path = None
        # Look for video in movies/ folder
        for ext in [".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"]:
            cand = os.path.join("movies", movie_name + ext)
            if os.path.exists(cand):
                video_path = cand
                break

    project_dir = os.path.join(output_dir, movie_name)
    state_path = os.path.join(project_dir, "state.json")

    print(f"[*] Regenerating thumbnail for project: {movie_name}")

    # 2. Load or initialize state
    state = None
    if os.path.exists(state_path):
        try:
            state = MovieState.load_from_json(state_path)
            print(f"    - Loaded existing state from -> {state_path}")
        except Exception as e:
            print(f"    - Notice: Could not parse {state_path}: {e}")

    if state is None:
        state = MovieState(movie_name=movie_name)

    if custom_title:
        state.custom_thumb_title = custom_title.strip()
        print(f"    - Using custom title: {state.custom_thumb_title}")

    # 3. Locate source video
    if not video_path or not os.path.exists(video_path):
        video_path = getattr(state, "movie_path", None) or getattr(state, "file_path", None)

    if not video_path or not os.path.exists(video_path):
        # Check inside project output folder
        for f in os.listdir(project_dir) if os.path.exists(project_dir) else []:
            if f.endswith((".mp4", ".mkv", ".webm")) and not f.startswith("final_"):
                cand = os.path.join(project_dir, f)
                if os.path.isfile(cand):
                    video_path = cand
                    break

    if not video_path or not os.path.exists(video_path):
        # Fallback to final output video if source not available
        final_video = os.path.join(project_dir, "final_recap.mp4")
        if os.path.exists(final_video):
            video_path = final_video

    if not video_path or not os.path.exists(video_path):
        print(f"❌ [ERROR] Could not find source video for project '{movie_name}'.")
        print("💡 Please provide the full path to the video file, e.g.:")
        print(f"   python regenerate_thumbnail.py movies/{movie_name}.mp4 --title \"Your Title\"")
        sys.exit(1)

    print(f"    - Source video: {video_path}")

    # 4. Extract frame & render thumbnail
    agent = ThumbnailAgent()
    temp_base = agent.extract_base_frame(state, video_path)
    if not temp_base or not os.path.exists(temp_base):
        print("❌ [ERROR] Could not extract thumbnail base frame from video.")
        sys.exit(1)

    os.makedirs(project_dir, exist_ok=True)
    state = agent.overlay_text(state, temp_base)

    # 5. Persist updated state
    try:
        state.save_to_json(state_path)
    except Exception as _exc:
        log_suppressed("regenerate_thumbnail.regenerate_thumbnail", _exc)

    thumb_path = getattr(state, "thumbnail_path", os.path.join(project_dir, "thumbnail.jpg"))
    print("\n🎉 [SUCCESS] Thumbnail regenerated successfully!")
    print(f"🖼️ Output Thumbnail -> {thumb_path}")
    return thumb_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Movie Recap — Reusable Thumbnail Generator")
    parser.add_argument("movie", help="Movie name, folder, or path to video file")
    parser.add_argument("--title", "-t", default=None, help="Custom title to burn on thumbnail (optional)")
    parser.add_argument("--output-dir", "-o", default="outputs", help="Output directory containing movie project (default: outputs)")
    args = parser.parse_args()

    regenerate_thumbnail(args.movie, custom_title=args.title, output_dir=args.output_dir)
