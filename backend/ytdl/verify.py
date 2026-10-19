"""Checking downloaded files: stream sizes, length, decoding the end, embedded cover."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .util import _fmt_secs

DAMAGED = "The downloaded file is damaged"


def _decodes_fully(path: str, duration: float | None) -> bool:
    """Second opinion before calling a file damaged: decode all of it. True only if ffmpeg reads it to the
    end without a single error (and, when the length is known, reaches at least 97% of it)."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        res = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:V?", "-map", "0:a?",
                              "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
                             capture_output=True, text=True, timeout=min(3600, max(300, 2 * (duration or 600))))
    except (OSError, subprocess.TimeoutExpired):
        return False
    if res.returncode != 0 or res.stderr.strip():
        return False
    times = re.findall(r"out_time_us=(\d+)", res.stdout)
    reached = int(times[-1]) / 1e6 if times else 0.0
    return reached >= 0.97 * duration if duration else reached > 0


def _verify_file(path: str | None, duration: float | None) -> str | None:
    """Check the finished file is really there and complete. Returns a problem, or None."""
    if not path or not os.path.isfile(path):
        return "The downloaded file is missing"
    if os.path.getsize(path) == 0:
        return "The downloaded file is empty"
    if not shutil.which("ffprobe"):
        return None
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return DAMAGED
    try:
        real = float(res.stdout.split()[0])
    except (ValueError, IndexError):
        return None                                  # no duration in the container: nothing to compare
    if duration and real < duration - max(10, 0.03 * duration):
        return f"The file is incomplete ({_fmt_secs(real)} of {_fmt_secs(duration)})"
    # The header can claim the full length of a cut-off file: decode the last seconds to be sure they exist.
    if real > 15:
        try:
            tail = subprocess.run(["ffmpeg", "-v", "error", "-sseof", "-5", "-i", path, "-map", "0:a:0?",
                                   "-map", "0:V:0?", "-f", "framemd5", "-"], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if tail.returncode == 0 and not any(line and not line.startswith("#") for line in tail.stdout.splitlines()):
            return "The end of the file is missing"
    return None


def _has_cover(path: str) -> bool:
    """True if the file carries an embedded cover picture (so the separate thumbnail file isn't needed)."""
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                              "stream=codec_name:stream_disposition=attached_pic", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.rstrip().endswith(",1") for line in res.stdout.splitlines())


def _check_part_sizes(info: dict, parts: dict[str, Path]) -> str | None:
    """Each downloaded stream must be exactly the size the site reported (catches cut-off or doubled data).
    Only plain http(s) streams are compared; fragmented ones (HLS/DASH) have no exact size in advance."""
    expected = {f.get("format_id"): f.get("filesize") for f in info.get("requested_formats") or []
                if str(f.get("protocol") or "").startswith("http")}
    for fid, path in parts.items():
        size, want = path.stat().st_size, expected.get(fid)
        if want and size != want:
            return f"Downloaded stream {fid} has the wrong size ({size:,} instead of {want:,} bytes)"
    return None
