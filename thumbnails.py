"""
HomeStream thumbnail maker.

Grabs one good-looking frame from every movie in media/movies and saves it
as a small JPEG in media/thumbs. The web page shows these as movie covers.

Usage examples:
    python thumbnails.py              make thumbnails for movies that don't have one
    python thumbnails.py --at 25      take the frame from 25% into each movie
    python thumbnails.py --force      make every thumbnail again
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from main import MOVIES_DIR, THUMBS_DIR, build_movie_info, iter_video_files

THUMB_WIDTH = 640


def get_duration(path: Path) -> float:
    """Ask ffprobe how long the video is, in seconds. Returns 0 if it can't tell."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def make_thumbnail(source: Path, movie: dict, percent: int) -> bool:
    duration = get_duration(source)

    # Jump to a spot inside the movie. The very start is often a black screen or a logo.
    seek_to = duration * percent / 100 if duration > 0 else 1.0

    final_path = THUMBS_DIR / f"{movie['id']}.jpg"
    temp_path = THUMBS_DIR / f"{movie['id']}.tmp.jpg"

    # "thumbnail=60" looks at the next 60 frames and picks the most typical one,
    # which avoids fades, blurry transitions and half black frames.
    # The scale part resizes to 640 pixels wide, but never makes a small video bigger.
    video_filter = f"thumbnail=60,scale='min({THUMB_WIDTH},iw)':-2"

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{seek_to:.2f}",      # placed before -i so FFmpeg jumps instantly instead of reading everything
        "-i", str(source),
        "-vf", video_filter,
        "-frames:v", "1",
        "-q:v", "3",                  # JPEG quality: 2 is best, 31 is worst
        str(temp_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size == 0:
        temp_path.unlink(missing_ok=True)
        message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        print(f"Failed: {movie['title']} ({message})")
        return False

    temp_path.replace(final_path)
    print(f"Done: {movie['title']}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Make thumbnails for your movies.")
    parser.add_argument("--at", type=int, default=10, choices=range(0, 91), metavar="PERCENT",
                        help="How far into the movie to take the picture, 0 to 90 (default 10)")
    parser.add_argument("--force", action="store_true", help="Replace thumbnails that already exist")
    args = parser.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("FFmpeg isn't installed or isn't on PATH. Install it, reopen the terminal and try again.")

    files = list(iter_video_files())
    if not files:
        sys.exit(f"No movies found in {MOVIES_DIR}")

    made, skipped, failed = 0, 0, 0
    for source in files:
        movie = build_movie_info(source)
        if movie["thumbnail_url"] and not args.force:
            skipped += 1
            continue
        if make_thumbnail(source, movie, args.at):
            made += 1
        else:
            failed += 1

    print(f"\nFinished. Made {made}, already done {skipped}, failed {failed}.")


if __name__ == "__main__":
    main()