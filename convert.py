"""
HomeStream converter.

Turns every movie in media/movies into an adaptive HLS stream in media/hls,
with several quality levels the player can switch between.

Usage examples:
    python convert.py                      convert new movies (up to 720p)
    python convert.py --max-height 1080    also make a 1080p version
    python convert.py --encoder qsv        use Intel Quick Sync (faster, uses the GPU)
    python convert.py --force              convert everything again
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from main import HLS_DIR, MOVIES_DIR, build_movie_info, iter_video_files

# The quality ladder. Bitrates are in kilobits per second.
RENDITIONS = [
    {"name": "1080p", "height": 1080, "bitrate": 5000},
    {"name": "720p", "height": 720, "bitrate": 2800},
    {"name": "360p", "height": 360, "bitrate": 800},
]

SEGMENT_SECONDS = 6


def probe(path: Path) -> dict:
    """Ask ffprobe for the video height and whether the file has audio."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(result.stdout)["streams"]
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    if video is None:
        raise ValueError("No video stream found")
    return {"height": int(video["height"]), "has_audio": has_audio}


def choose_renditions(source_height: int, max_height: int) -> list[dict]:
    """Pick quality levels that are not bigger than the source (no point upscaling)."""
    chosen = [r for r in RENDITIONS if r["height"] <= max_height and r["height"] <= source_height]
    if not chosen:
        # Very small source video: keep its own size, rounded to an even number
        height = source_height - (source_height % 2)
        chosen = [{"name": f"{height}p", "height": height, "bitrate": 800}]
    return chosen


def build_command(source: Path, out_dir: Path, renditions: list[dict], has_audio: bool, encoder: str) -> list[str]:
    """Build one FFmpeg command that creates every quality level in a single pass."""
    count = len(renditions)

    # Split the video into copies, then resize each copy
    split_outputs = "".join(f"[v{i}]" for i in range(count))
    filters = [f"[0:v]split={count}{split_outputs}"]
    for i, r in enumerate(renditions):
        filters.append(f"[v{i}]scale=-2:{r['height']}[v{i}out]")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y", "-i", str(source),
           "-filter_complex", ";".join(filters)]

    video_codec = "h264_qsv" if encoder == "qsv" else "libx264"
    pixel_format = "nv12" if encoder == "qsv" else "yuv420p"

    for i, r in enumerate(renditions):
        cmd += [
            "-map", f"[v{i}out]",
            f"-c:v:{i}", video_codec,
            f"-b:v:{i}", f"{r['bitrate']}k",
            f"-maxrate:v:{i}", f"{int(r['bitrate'] * 1.1)}k",
            f"-bufsize:v:{i}", f"{r['bitrate'] * 2}k",
        ]

    if has_audio:
        for _ in renditions:
            cmd += ["-map", "0:a:0"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]

    cmd += [
        "-pix_fmt", pixel_format,
        "-preset", "veryfast",
        # A keyframe every segment, so every quality level can switch at the same moments
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
    ]

    if has_audio:
        stream_map = " ".join(f"v:{i},a:{i},name:{r['name']}" for i, r in enumerate(renditions))
    else:
        stream_map = " ".join(f"v:{i},name:{r['name']}" for i, r in enumerate(renditions))

    cmd += [
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(out_dir / "%v" / "seg_%04d.ts"),
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", stream_map,
        str(out_dir / "%v" / "index.m3u8"),
    ]
    return cmd


def convert_movie(source: Path, movie: dict, max_height: int, encoder: str) -> bool:
    final_dir = HLS_DIR / movie["id"]
    temp_dir = HLS_DIR / f"{movie['id']}.tmp"

    info = probe(source)
    renditions = choose_renditions(info["height"], max_height)
    names = ", ".join(r["name"] for r in renditions)
    print(f"\nConverting {movie['title']} ({names})")

    # Work in a temporary folder, so a half finished conversion never shows up in the app
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True)
    for r in renditions:
        (temp_dir / r["name"]).mkdir()

    command = build_command(source, temp_dir, renditions, info["has_audio"], encoder)
    result = subprocess.run(command)

    if result.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"Failed: {movie['title']}. See the FFmpeg message above.")
        return False

    shutil.rmtree(final_dir, ignore_errors=True)
    temp_dir.rename(final_dir)
    print(f"Done: {movie['title']}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Convert movies to adaptive HLS streams.")
    parser.add_argument("--max-height", type=int, default=720, choices=[360, 720, 1080],
                        help="Highest quality to create (default 720)")
    parser.add_argument("--encoder", default="cpu", choices=["cpu", "qsv"],
                        help="cpu works everywhere; qsv uses Intel Quick Sync on your GPU")
    parser.add_argument("--force", action="store_true", help="Convert movies again even if already done")
    args = parser.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("FFmpeg isn't installed or isn't on PATH. Install it, reopen the terminal and try again.")

    files = list(iter_video_files())
    if not files:
        sys.exit(f"No movies found in {MOVIES_DIR}")

    converted, skipped, failed = 0, 0, 0
    for source in files:
        movie = build_movie_info(source)
        if movie["hls_url"] and not args.force:
            skipped += 1
            continue
        try:
            if convert_movie(source, movie, args.max_height, args.encoder):
                converted += 1
            else:
                failed += 1
        except (subprocess.CalledProcessError, ValueError) as error:
            print(f"Skipped {movie['title']}: {error}")
            failed += 1

    print(f"\nFinished. Converted {converted}, already done {skipped}, failed {failed}.")


if __name__ == "__main__":
    main()