"""Download folders and their files: per-video file locks, finding a video's files by its [id] tag, disk
space, opening files in Finder/Explorer."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import LOCK_DIR_NAME

try:
    import fcntl  # POSIX file locks (macOS / Linux)
except ImportError:
    fcntl = None
try:
    import msvcrt  # Windows file locks
except ImportError:
    msvcrt = None


# Files in a download folder, recognised by the "[video id]" yt-dlp puts in every name.
MEDIA_EXTS = ("mp4", "mkv", "webm", "mp3", "m4a", "opus", "ogg", "flac", "wav", "mov")
FINAL_FILE = re.compile(r"\[([^\[\]]+)\]\.(?:%s)$" % "|".join(MEDIA_EXTS), re.I)   # "Title [id].mp4"
ID_TAG = re.compile(r"\[([^\[\]]+)\]")
LEFTOVER = re.compile(r"\.(?:part|ytdl|damaged)$|\.part-Frag\d+|\.temp\.\w+$", re.I)
SIDECAR = re.compile(r"\.(?:jpe?g|webp|png|vtt|srt|ass|lrc)$", re.I)
IMAGE_EXT = re.compile(r"\.(?:jpe?g|webp|png)$", re.I)


def _disk_free(path: str) -> dict:
    """Free space where path is (or would be). Never creates folders; an unplugged drive is reported."""
    parts = Path(path).parts
    if sys.platform == "darwin" and len(parts) > 2 and parts[1] == "Volumes" and not Path("/Volumes", parts[2]).is_dir():
        return {"free": None, "problem": f"Drive “{parts[2]}” is not connected"}
    p = Path(os.path.expanduser(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        du = shutil.disk_usage(p)
        return {"free": du.free, "total": du.total, "dev": os.stat(p).st_dev, "path": str(p)}
    except OSError as exc:
        return {"free": None, "problem": str(exc.strerror or exc)}


def _folder_problem(outdir: str) -> str | None:
    """Why we can't write to outdir right now (None when it's fine)."""
    parts = Path(outdir).parts
    # An unplugged external drive: never recreate /Volumes/<name> on the system disk.
    if sys.platform == "darwin" and len(parts) > 2 and parts[1] == "Volumes" and not Path("/Volumes", parts[2]).is_dir():
        return f"Drive “{parts[2]}” is not connected — waiting for it"
    try:
        Path(outdir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Can't write to the folder: {exc.strerror or exc}"
    if not os.access(outdir, os.W_OK):
        return "No permission to write to the folder"
    return None


class _FileLock:
    """An exclusive, non-blocking OS lock. Released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def acquire(self) -> bool:
        if fcntl is None and msvcrt is None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)      # locks the first byte (may lie past the end)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return True

    def release(self):
        if self.fd is None:
            return
        try:
            self.path.unlink(missing_ok=True)       # POSIX: remove while still locked; Windows can't, see below
        except OSError:
            pass
        if fcntl:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        else:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(self.fd)
        self.fd = None
        if not fcntl:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def _files_for(outdir: str, vid: str) -> list[Path]:
    """Every file in outdir that belongs to this video id."""
    tag = f"[{vid}]"
    try:
        return [p for p in Path(outdir).iterdir() if tag in p.name and p.is_file()]
    except OSError:
        return []


def _part_files(outdir: str, vid: str) -> dict[str, Path]:
    """Finished separate streams waiting to be merged ("Title [id].f137.mp4"), by format id."""
    rx = re.compile(re.escape(f"[{vid}]") + r"\.f([\w-]+)\.\w+$")
    out = {}
    for p in _files_for(outdir, vid):
        m = rx.search(p.name)
        if m and not LEFTOVER.search(p.name):
            out[m.group(1)] = p
    return out


def _lock_path(outdir: str, archive_key: str) -> Path:
    return Path(outdir) / LOCK_DIR_NAME / (re.sub(r"[^\w.-]", "_", archive_key) + ".lock")


def _move_aside(path: str) -> str:
    """Rename a damaged file instead of deleting it: "x.mp4" -> "x.mp4.damaged"."""
    target = path + ".damaged"
    if os.path.exists(target):
        target = f"{path}.{int(time.time())}.damaged"
    os.replace(path, target)
    return target


def _remove(paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _open_path(path: str, reveal: bool = False):
    """Open a file or folder in its default app; reveal=True shows the file in Finder/Explorer instead."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    elif sys.platform.startswith("win"):
        if reveal:
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])


def _open_folder(path: str):
    _open_path(path)
