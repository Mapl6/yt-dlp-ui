"""Read-only health report for downloaded videos. Never renames, moves or deletes anything.

Usage: python verify_folder.py <folder> [--full] [--id VIDEO_ID ...]

For each video file (named "... [<id>].<ext>") it runs the app's own check (ytdl.verify._verify_file:
ffprobe + decode the last seconds). --full also decodes the whole file (slow: ~2-3 min per hour of video)
to catch damage in the middle. It also lists leftovers (.part, unmerged .fNNN streams, .damaged).
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "backend"))
from ytdl.files import FINAL_FILE, LEFTOVER  # noqa: E402
from ytdl.verify import _verify_file  # noqa: E402  (import has no side effects)


def full_decode(path: str) -> str | None:
    res = subprocess.run(["ffmpeg", "-v", "error", "-hwaccel", "auto", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True)
    errors = [line for line in res.stderr.splitlines() if line.strip()]
    if res.returncode != 0 or errors:
        return f"decode errors: {errors[0][:120] if errors else 'exit ' + str(res.returncode)}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--full", action="store_true", help="decode every file completely (slow)")
    ap.add_argument("--id", action="append", default=[], help="only check these video ids")
    args = ap.parse_args()

    names = sorted(os.listdir(args.folder))
    videos = [n for n in names if FINAL_FILE.search(n) and (not args.id or FINAL_FILE.search(n).group(1) in args.id)]
    stream = re.compile(r"\]\.f[\w-]+\.\w+$")            # unmerged "Title [id].f137.mp4"
    leftovers = [n for n in names if LEFTOVER.search(n) or stream.search(n)]
    bad = 0
    for name in videos:
        path = os.path.join(args.folder, name)
        start = time.time()
        problem = _verify_file(path, None)
        if not problem and args.full:
            problem = full_decode(path)
        bad += bool(problem)
        size = os.path.getsize(path) / 1e6
        print(f"{'BAD ' if problem else 'OK  '} {size:9.1f} MB  {time.time() - start:5.1f}s  {name}"
              + (f"\n      -> {problem}" if problem else ""))
    print(f"\n{len(videos)} files checked{' (full decode)' if args.full else ''}: {len(videos) - bad} OK, {bad} bad")
    if leftovers:
        print(f"{len(leftovers)} leftover files (partial downloads / unmerged streams / .damaged):")
        for n in leftovers[:20]:
            print("   ", n)


if __name__ == "__main__":
    main()
