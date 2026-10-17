"""Download engine: playlist inspection, a persistent job queue, and a worker pool built on yt-dlp."""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import fcntl                                   # POSIX file locks (macOS / Linux)
except ImportError:
    fcntl = None
try:
    import msvcrt                                  # Windows file locks
except ImportError:
    msvcrt = None

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP
from yt_dlp.utils import ContentTooShortError, DownloadCancelled, ExtractorError, GeoRestrictedError, download_range_func

DATA_DIR = Path(os.environ.get("YTDL_DATA_DIR") or Path(__file__).parent / "data")
DB_FILE = DATA_DIR / "library.db"
DB_BACKUP = DATA_DIR / "library.backup.db"
LEGACY_STATE = DATA_DIR / "state.json"            # pre-SQLite library, migrated on first start
LEGACY_BACKUP = DATA_DIR / "state.backup.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "app.log"
ARCHIVE_NAME = ".downloaded.txt"
LOCK_DIR_NAME = ".ytdl-locks"                     # per-video locks inside the download folder

MAX_WORKERS = 8          # hard upper bound for parallel downloads
MAX_ATTEMPTS = 4         # per item, for unexpected errors
MAX_NET_ATTEMPTS = 40    # per item, for connection problems (weak internet must never lose a video)
MAX_RATE_STRIKES = 8     # per item, for YouTube rate limiting
MIN_FREE_BYTES = 1 << 30 # pause everything below 1 GB free
BACKUP_EVERY = 600       # seconds between copies of the library to library.backup.db
ITEM_LOG_LINES = 40      # warnings/errors kept per video for the "Details" view
ACTIVE = {"downloading", "processing"}

DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "Downloads" / "YT Downloader"),
    "concurrency": 3,
    "cookies_browser": "",       # "", chrome, safari, firefox, brave, edge, ...
    "rate_limit_mb": 0.0,        # per download, 0 = unlimited
    "sleep_between": 0.0,        # seconds between starting downloads (helps avoid 429s)
    "proxy": "",                 # e.g. socks5://127.0.0.1:1080 or http://127.0.0.1:8080
    "wait_for_network": True,    # hold the queue while offline, slow down on a weak connection
    "total_rate_mb": 0.0,        # shared by all downloads, 0 = unlimited
    "schedule_on": False,        # only start downloads between schedule_start and schedule_end (local time)
    "schedule_start": "01:00",
    "schedule_end": "07:00",
    "sub_langs": "en",           # default subtitle languages, e.g. "en, fa"
}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
AUDIO_FORMATS = {"mp3", "m4a", "opus"}
# Typical total bitrate (bits/s) per quality, to estimate sizes before they are known.
BITRATES = {2160: 20e6, 1440: 10e6, 1080: 5e6, 720: 2.5e6, 480: 1.2e6, 360: 0.7e6, 240: 0.4e6, 144: 0.2e6}
BEST_BITRATE, AUDIO_BITRATE = 8e6, 160e3
SPONSOR_CATEGORIES = ("sponsor", "selfpromo", "interaction")
SUB_LANG = re.compile(r"^(?:all|[a-z]{2,3}(?:-[a-z0-9]+)*)$", re.I)
SYNC_TICK = 60           # seconds between checks for playlists due an automatic refresh
SYNC_CHOICES = (0, 6, 12, 24, 168)                # hours between automatic refreshes (0 = off)
YTDLP_CHECK_EVERY = 6 * 3600

# Text fallbacks for error classification (exception types and HTTP status are checked first).
DISK_ERRORS = re.compile(r"no space left|errno 28|disk (?:is )?full|not enough space", re.I)
PERMANENT_ERRORS = re.compile(
    r"private video|video unavailable|has been removed|members.only|not available in your country|"
    r"copyright|account associated with this video|sign in to confirm your age|premieres in|"
    r"this live event will begin|deleted video|is not a valid url|unsupported url|http error 404",
    re.I,
)
RATE_LIMIT_ERRORS = re.compile(r"http error 429|too many requests|confirm you.?re not a bot|rate.?limit", re.I)
NETWORK_ERRORS = re.compile(
    r"timed? ?out|connection (?:reset|refused|aborted|error|broken)|remote end closed|broken pipe|"
    r"network is unreachable|no route to host|temporary failure in name resolution|name or service not known|"
    r"nodename nor servname|getaddrinfo|failed to resolve|unable to connect|proxy|ssl|eof occurred|"
    r"incomplete ?read|content too short|did not get any data|http error 5\d\d|transport|urlopen error|"
    r"unable to download (?:webpage|video data|json|api)|giving up after|errno (?:32|54|60|61|65|104|110|111|113)",
    re.I,
)
# Files in a download folder, recognised by the "[video id]" yt-dlp puts in every name.
MEDIA_EXTS = ("mp4", "mkv", "webm", "mp3", "m4a", "opus", "ogg", "flac", "wav", "mov")
FINAL_FILE = re.compile(r"\[([^\[\]]+)\]\.(?:%s)$" % "|".join(MEDIA_EXTS), re.I)   # "Title [id].mp4"
ID_TAG = re.compile(r"\[([^\[\]]+)\]")
LEFTOVER = re.compile(r"\.(?:part|ytdl|damaged)$|\.part-Frag\d+|\.temp\.\w+$", re.I)
SIDECAR = re.compile(r"\.(?:jpe?g|webp|png|vtt|srt|ass|lrc)$", re.I)
IMAGE_EXT = re.compile(r"\.(?:jpe?g|webp|png)$", re.I)
CHECK_WORKERS = 3        # files verified in parallel by "Check files"

# Placeholder titles YouTube uses for entries that can't be watched any more.
UNAVAILABLE_TITLE = re.compile(r"^\[(?:private|deleted|unavailable)[^\]]*\]$", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")

log = logging.getLogger("ytdl")


def setup_logging():
    if log.handlers:
        return
    DATA_DIR.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _clean_error(exc: Exception) -> str:
    msg = ANSI.sub("", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)  # drop "[youtube] abc123: " prefix
    return msg[:400]


def _exc_chain(exc: BaseException) -> list[BaseException]:
    """The exception plus everything it wraps (yt-dlp keeps the original in exc_info / cause)."""
    seen: dict[int, BaseException] = {}
    stack = [exc]
    while stack:
        e = stack.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen[id(e)] = e
        info = getattr(e, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1:
            stack.append(info[1])
        stack += [getattr(e, "cause", None), e.__cause__, e.__context__]
    return list(seen.values())


def _error_kind(exc: BaseException | str) -> str:
    """disk | permanent | ratelimit | network | other"""
    chain = [] if isinstance(exc, str) else _exc_chain(exc)
    for e in chain:
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            return "disk"
        if isinstance(e, HTTPError):
            if e.status == 429:
                return "ratelimit"
            if e.status in (404, 410):
                return "permanent"
            if e.status >= 500 or e.status in (403, 408):   # 403: expired stream URL, fixed by re-extracting
                return "network"
        if isinstance(e, GeoRestrictedError):
            return "permanent"
    msg = exc if isinstance(exc, str) else " | ".join(_clean_error(e) for e in chain)
    for kind, rx in (("disk", DISK_ERRORS), ("ratelimit", RATE_LIMIT_ERRORS), ("permanent", PERMANENT_ERRORS)):
        if rx.search(msg):
            return kind
    if any(isinstance(e, (TransportError, ContentTooShortError, socket.timeout, TimeoutError, ConnectionError))
           for e in chain) or NETWORK_ERRORS.search(msg):
        return "network"
    if any(isinstance(e, ExtractorError) and e.expected for e in chain):
        return "permanent"                           # yt-dlp marks errors a retry can't fix as "expected"
    return "other"


class _IncompleteFile(Exception):
    """The download finished but the file on disk failed verification."""


def _escape_tmpl(text: str) -> str:
    """Make literal text safe inside a yt-dlp output template."""
    return text.replace("%", "%%")


def _safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" .")
    return text[:120] or "Untitled"


def _fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _atomic_write(path: Path, text: str):
    """Write so that a crash or power loss leaves either the old file or the new one, never half of each."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _coerce_settings(raw: dict) -> dict:
    """Return complete, well-typed settings; bad values fall back to the defaults."""
    out = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = raw.get(key, default)
        try:
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(float(value))
            elif isinstance(default, float):
                value = max(0.0, float(value))
            else:
                value = str(value or "").strip()
        except (TypeError, ValueError):
            value = default
        out[key] = value
    out["concurrency"] = max(1, min(MAX_WORKERS, out["concurrency"]))
    out["output_dir"] = out["output_dir"] or DEFAULT_SETTINGS["output_dir"]
    for key in ("schedule_start", "schedule_end"):
        m = HHMM.match(out[key])
        out[key] = f"{int(m.group(1)):02d}:{m.group(2)}" if m else DEFAULT_SETTINGS[key]
    return out


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _sub_langs(text: str) -> list[str]:
    """ "en, fa" -> yt-dlp patterns that also match regional variants (en-US, en-GB…)."""
    out = []
    for code in re.split(r"[\s,;]+", text or ""):
        if SUB_LANG.match(code):
            out += ["all"] if code.lower() == "all" else [code, f"{code}-.*"]
    return out or ["en", "en-.*"]


def _parse_time(text) -> float | None:
    """ "90", "1:30", "1:02:03" or "1:30.5" -> seconds (None if it can't be read)."""
    text = str(text).strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", text):
        return None
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return secs


def _parse_section(section) -> dict | None:
    """Validate "download only part": {"start": "1:00", "end": "2:30"} -> {"start": 60.0, "end": 150.0}.
    An empty end means "to the end of the video". None when no part is asked for."""
    if not section:
        return None
    start_txt, end_txt = str(section.get("start") or "").strip(), str(section.get("end") or "").strip()
    if not start_txt and not end_txt:
        return None
    start = _parse_time(start_txt) if start_txt else 0.0
    end = _parse_time(end_txt) if end_txt else None
    if start is None or (end_txt and end is None):
        raise ValueError("Use times like 1:30, 1:02:03 or 90 for the part to download")
    if end is not None and end <= start:
        raise ValueError("The end of the part must be after its start")
    return {"start": start, "end": end}


def _section_label(section: dict) -> str:
    """For the file name: "1-00–2-30" (no colons, which some systems don't allow)."""
    end = _fmt_secs(section["end"]) if section.get("end") is not None else "end"
    return f"{_fmt_secs(section['start'])}–{end}".replace(":", "-")


def _expected_duration(item: dict) -> float | None:
    """Length the finished file should have: as verified at download (parts and cut sponsor segments make
    it shorter than the video), else the part asked for, else the video's length."""
    if item.get("media_duration"):
        return item["media_duration"]
    section = (item.get("options") or {}).get("section")
    if section:
        end = section.get("end") if section.get("end") is not None else item.get("duration")
        return end - section["start"] if end else None
    if (item.get("options") or {}).get("sponsorblock") == "remove":
        return None                                  # length after cutting isn't known
    return item.get("duration")


def _estimate_bytes(item: dict, options: dict) -> tuple[int, bool]:
    """-> (bytes still to download, exact?). Uses the size yt-dlp reported, else duration × typical bitrate."""
    if item.get("total") and item["status"] in ("queued", "downloading"):
        return max(0, int(item["total"]) - int(item.get("downloaded") or 0)), True
    quality = str(options.get("quality", "best"))
    bps = AUDIO_BITRATE if quality in AUDIO_FORMATS else BITRATES.get(int(quality), BEST_BITRATE) \
        if quality.isdigit() else BEST_BITRATE
    duration = item.get("duration")
    section = options.get("section")
    if duration and section:
        duration = (section["end"] if section.get("end") is not None else duration) - section["start"]
    return (int(max(0, duration) * bps / 8) if duration else 0), False


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


def _retry_sleep(n: int) -> float:
    return min(2 ** n, 30)          # 1, 2, 4, … capped at 30 s between yt-dlp's own retries


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


class _PlaylistTags(PostProcessor):
    """Adds the playlist name (album) and the video's number (track) to the file's metadata."""

    def __init__(self, tags: dict):
        super().__init__()
        self.tags = {f"meta_{k}": v for k, v in tags.items() if v}

    def run(self, info):
        info.update(self.tags)
        return [], info


def _describe_format(info: dict) -> str:
    """ "1920x1080 · avc1 + mp4a · mp4" for the formats yt-dlp picked."""
    fmts = info.get("requested_formats") or [info]
    res = next((f.get("resolution") for f in fmts if f.get("height")), None) or info.get("resolution")
    codecs = [next((f.get(k) for f in fmts if f.get(k) not in (None, "none")), None) for k in ("vcodec", "acodec")]
    bits = [res if res and res != "audio only" else None,
            " + ".join(c.split(".")[0] for c in codecs if c), info.get("ext")]
    return " · ".join(b for b in bits if b)


class _FormatNote(PostProcessor):
    """Runs after format selection, before the download: reports which format was chosen."""

    def __init__(self, report):
        super().__init__()
        self.report = report

    def run(self, info):
        try:
            self.report(_describe_format(info))
        except Exception:                          # a display detail must never break a download
            pass
        return [], info


class _SafeSponsorBlock(SponsorBlockPP):
    """SponsorBlock that never fails a download: if the segment list can't be fetched, download without it."""

    def run(self, info):
        try:
            return super().run(info)
        except Exception as exc:
            self.report_warning(f"SponsorBlock segments unavailable ({_clean_error(exc)}); continuing without them")
            return [], info


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


class AlreadyRunning(RuntimeError):
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


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class _ItemLogger(_QuietLogger):
    """Keeps yt-dlp's warnings and errors for one video (shown under "Details") and writes them to app.log."""

    def __init__(self, add):
        self._add = add

    def warning(self, msg):
        self._add("warning", msg)

    def error(self, msg):
        self._add("error", msg)


def _base_opts(settings: dict, logger=None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": logger or _QuietLogger(),
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
        "socket_timeout": 30,        # a stalled connection is dropped and retried after 30 s
        "retries": 15,
        "extractor_retries": 5,
        "retry_sleep_functions": {"http": _retry_sleep, "fragment": _retry_sleep,
                                  "extractor": _retry_sleep, "file_access": lambda n: 1},
    }
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"].strip()
    if settings.get("cookies_browser"):
        opts["cookiesfrombrowser"] = (settings["cookies_browser"],)
    return opts


# ---------------------------------------------------------------- inspection

def inspect_url(url: str, settings: dict) -> dict:
    """Return metadata for a video or (flattened) playlist without downloading anything."""
    opts = _base_opts(settings) | {"skip_download": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)

        if info.get("_type") in ("playlist", "multi_video"):
            entries = list(info.get("entries") or [])
            # Channel pages return their tabs (Videos, Shorts, Live) as nested playlists: flatten one level.
            if entries and all(e.get("_type") == "playlist" or e.get("ie_key") == "YoutubeTab" for e in entries):
                nested = []
                for tab in entries:
                    sub = ydl.sanitize_info(ydl.extract_info(tab["url"], download=False))
                    nested.extend(sub.get("entries") or [])
                entries = nested

            seen, items = set(), []
            for e in entries:
                if not e or not e.get("id") or e["id"] in seen:
                    continue
                seen.add(e["id"])
                items.append({
                    "index": len(items) + 1,
                    "id": e["id"],
                    "title": e.get("title") or e["id"],
                    "url": e.get("url") if str(e.get("url", "")).startswith("http")
                           else f"https://www.youtube.com/watch?v={e['id']}",
                    "duration": e.get("duration"),
                    "extractor": e.get("ie_key") or info.get("extractor_key") or "youtube",
                })
            return {
                "kind": "playlist",
                "source_id": info.get("id"),
                "title": info.get("title") or "Playlist",
                "uploader": info.get("uploader") or info.get("channel"),
                "thumbnail": _pick_thumb(info) or (_pick_thumb(entries[0]) if entries else None),
                "items": items,
                "heights": VIDEO_HEIGHTS,
            }

        heights = sorted({f["height"] for f in info.get("formats") or [] if f.get("height")}, reverse=True)
        return {
            "kind": "video",
            "source_id": info.get("id"),
            "title": info.get("title") or info.get("id"),
            "uploader": info.get("uploader") or info.get("channel"),
            "thumbnail": info.get("thumbnail") or _pick_thumb(info),
            "items": [{
                "index": 1,
                "id": info["id"],
                "title": info.get("title") or info["id"],
                "url": info.get("webpage_url") or url,
                "duration": info.get("duration"),
                "extractor": info.get("extractor_key") or "youtube",
            }],
            "heights": [h for h in VIDEO_HEIGHTS if any(x >= h for x in heights)] or heights,
        }


def _pick_thumb(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return best.get("url")
    return info.get("thumbnail")


# ---------------------------------------------------------------- options

def build_format_opts(options: dict) -> tuple[dict, list]:
    """Translate UI options into yt-dlp format options and postprocessors."""
    quality = str(options.get("quality", "best"))
    container = options.get("container", "mp4")
    pps: list[dict] = []
    opts: dict = {}

    if quality in AUDIO_FORMATS:
        opts["format"] = {"m4a": "ba[ext=m4a]/ba/b", "opus": "ba[acodec=opus]/ba/b"}.get(quality, "ba/b")
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": quality,
                    "preferredquality": "0" if quality == "mp3" else None})
        pps[-1] = {k: v for k, v in pps[-1].items() if v is not None}
    else:
        opts["format"] = "bv*+ba/b"
        sort = [] if quality == "best" else [f"res:{int(quality)}"]
        if container == "mp4":
            # Prefer H.264/AAC so files play everywhere (QuickTime, phones, TVs).
            sort += ["vcodec:h264", "acodec:aac"]
        opts["format_sort"] = sort
        opts["merge_output_format"] = container
        if options.get("subtitles"):
            opts |= {"writesubtitles": True, "subtitleslangs": _sub_langs(options.get("sub_langs") or "en"),
                     "subtitlesformat": "srt/best"}
            if options.get("auto_subs"):
                opts["writeautomaticsub"] = True       # YouTube's auto-generated subtitles
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    sponsor = options.get("sponsorblock")
    if sponsor in ("mark", "remove"):
        # The segments are fetched by _SafeSponsorBlock (added in _download_locked). ModifyChapters must run
        # before FFmpegMetadata: it cuts the segments ("remove") or turns them into chapters ("mark").
        cats = set(options.get("sponsor_categories") or SPONSOR_CATEGORIES) & set(SponsorBlockPP.CATEGORIES)
        pps.append({"key": "ModifyChapters", "remove_sponsor_segments": cats if sponsor == "remove" else set(),
                    "force_keyframes": False})
    if options.get("metadata", True):
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    elif sponsor == "mark":
        pps.append({"key": "FFmpegMetadata", "add_metadata": False, "add_chapters": True})
    if options.get("thumbnail", True):
        opts["writethumbnail"] = True
        pps.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return opts, pps


# ---------------------------------------------------------------- storage

class Store:
    """The library on disk: SQLite, one row per playlist and per video, so a save only writes what changed.

    WAL journaling with synchronous=FULL survives crashes and power loss; a damaged database is
    moved aside (never deleted) and the latest backup is used instead.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS jobs  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS items (job_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                          PRIMARY KEY (job_id, id));
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                                           job_id TEXT, job_title TEXT, item_id TEXT, title TEXT, url TEXT,
                                           status TEXT, kind TEXT, error TEXT, bytes INTEGER, seconds REAL,
                                           filepath TEXT, hidden INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS events_time ON events (time);
    """
    EVENT_COLS = ("time", "job_id", "job_title", "item_id", "title", "url", "status", "kind", "error", "bytes",
                  "seconds", "filepath")

    def __init__(self, path: Path, backup: Path):
        self.path, self.backup_path = path, backup
        self._lock = threading.Lock()     # one connection, shared by the workers and the saver
        try:
            self.conn = self._connect()
        except sqlite3.DatabaseError as exc:
            aside = path.with_name(f"library.damaged-{int(time.time())}.db")
            log.error("Library database is damaged (%s); kept it as %s and loading the backup", exc, aside.name)
            print(f"Library database was damaged; kept it as {aside.name} and loaded the backup.", file=sys.stderr)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    src.replace(Path(str(aside) + suffix))
            if backup.exists():
                shutil.copyfile(backup, path)
            self.conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(self.SCHEMA)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
            self.version = conn.execute("PRAGMA user_version").fetchone()[0]   # 0: event log not filled yet
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def load(self) -> list[dict]:
        with self._lock:
            jobs = {jid: json.loads(data) for jid, data in self.conn.execute("SELECT id, data FROM jobs")}
            for job in jobs.values():
                job["items"] = []
            for jid, data in self.conn.execute("SELECT job_id, data FROM items"):
                if jid in jobs:
                    jobs[jid]["items"].append(json.loads(data))
        for job in jobs.values():
            job["items"].sort(key=lambda it: it.get("index", 0))
        return list(jobs.values())

    def write(self, jobs: list[tuple[str, str]], items: list[tuple[str, str, str]], deleted: list[str]):
        if not (jobs or items or deleted):
            return
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany("DELETE FROM items WHERE job_id = ?", [(j,) for j in deleted])
                cur.executemany("DELETE FROM jobs WHERE id = ?", [(j,) for j in deleted])
                cur.executemany("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", jobs)
                cur.executemany("INSERT OR REPLACE INTO items (job_id, id, data) VALUES (?, ?, ?)", items)
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def backup(self):
        tmp = self.backup_path.with_name(self.backup_path.name + ".tmp")
        dst = sqlite3.connect(tmp)
        try:
            with self._lock:
                self.conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, self.backup_path)

    # ---- event log: one row per finished or failed download, kept even when the video is retried later

    def add_events(self, rows: list[dict]) -> int:
        """Append events; returns the id of the last one."""
        if not rows:
            return self.last_event_id()
        cols = ", ".join(self.EVENT_COLS)
        marks = ", ".join("?" * len(self.EVENT_COLS))
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(f"INSERT INTO events ({cols}) VALUES ({marks})",
                                [tuple(r.get(c) for c in self.EVENT_COLS) for r in rows])
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            return cur.lastrowid

    def set_version(self, version: int):
        with self._lock:
            self.conn.execute(f"PRAGMA user_version = {int(version)}")
            self.version = version

    def last_event_id(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def events(self, offset: int = 0, limit: int = 30, since_id: int = 0, hidden: bool = False) -> list[dict]:
        """Newest first. hidden=False leaves out rows the user cleared from "Recently finished"."""
        where = "id > ?" + ("" if hidden else " AND hidden = 0")
        with self._lock:
            cur = self.conn.execute(f"SELECT id, {', '.join(self.EVENT_COLS)} FROM events WHERE {where} "
                                    "ORDER BY time DESC, id DESC LIMIT ? OFFSET ?", (since_id, limit, offset))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def count_events(self, hidden: bool = False) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM events" + ("" if hidden else " WHERE hidden = 0")).fetchone()[0]

    def counts_since(self, since: float) -> dict[str, int]:
        with self._lock:
            return dict(self.conn.execute("SELECT status, COUNT(*) FROM events WHERE time >= ? GROUP BY status",
                                          (since,)).fetchall())

    def hide_events(self) -> int:
        with self._lock:
            return self.conn.execute("UPDATE events SET hidden = 1 WHERE hidden = 0").rowcount

    def stats(self, since: float) -> dict:
        """Totals and per-day figures for the History & stats view (all events, cleared or not)."""
        with self._lock:
            days = self.conn.execute(
                "SELECT date(time, 'unixepoch', 'localtime') AS day, "
                "SUM(status = 'done'), SUM(status = 'failed'), COALESCE(SUM(CASE WHEN status = 'done' THEN bytes END), 0) "
                "FROM events WHERE time >= ? GROUP BY day ORDER BY day", (since,)).fetchall()
            done, failed, total_bytes = self.conn.execute(
                "SELECT COALESCE(SUM(status = 'done'), 0), COALESCE(SUM(status = 'failed'), 0), "
                "COALESCE(SUM(CASE WHEN status = 'done' THEN bytes END), 0) FROM events WHERE time >= ?",
                (since,)).fetchone()
            timed_bytes, seconds = self.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0), COALESCE(SUM(seconds), 0) FROM events "
                "WHERE time >= ? AND status = 'done' AND seconds > 0 AND bytes > 0", (since,)).fetchone()
            kinds = self.conn.execute(
                "SELECT COALESCE(kind, 'other'), COUNT(*) FROM events WHERE time >= ? AND status = 'failed' "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 6", (since,)).fetchall()
        return {
            "days": [{"day": d, "done": n or 0, "failed": f or 0, "bytes": b or 0} for d, n, f, b in days],
            "done": done, "failed": failed, "bytes": total_bytes,
            "success": round(100 * done / (done + failed), 1) if done + failed else None,
            "avg_speed": timed_bytes / seconds if seconds else None,
            "kinds": [{"kind": k, "count": c} for k, c in kinds],
        }


def _read_legacy_state() -> list:
    """The old JSON library (state.json), falling back to its backup if damaged."""
    for path in (LEGACY_STATE, LEGACY_BACKUP):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return []


# ---------------------------------------------------------------- library manager
#
# Every fetched link is saved in a library ("jobs") with ALL of its videos.
# Item status:  new (not downloaded) -> queued -> downloading -> processing -> done | failed
# "skipped" means the file was already in the folder's archive (counts as downloaded).
# Item fields:  index   = position in the playlist right now (updated on every refresh)
#               number  = file-name prefix, given once and never changed, so files on disk keep their names
#               removed = no longer in the playlist, or private/deleted (can't be downloaded)
#
# Reliability rules:
#  * a video is only "done" (and written to the folder's archive) after the file on disk is verified;
#  * connection problems never use up the normal retry budget, and the queue waits while offline;
#  * disk-full and YouTube rate limiting pause the whole queue instead of failing videos one by one;
#  * interrupted downloads keep their .part files and resume where they stopped;
#  * every change to an item goes through the lock (self.cond).

ITEM_KEYS = ("index", "id", "title", "url", "duration", "number", "status", "progress", "speed",
             "eta", "downloaded", "total", "error", "attempts", "filepath", "removed", "health",
             "started", "finished", "priority", "held", "step", "fmt", "section")
LIVE_LEVELS = {"excellent", "good", "fair", "weak"}
LIVE = {"queued", *ACTIVE}         # statuses kept in the scheduler's index
HISTORY_LEN = 20                   # attempts kept per video for the dashboard's detail view
SPEED_SAMPLE = 2                   # seconds between total-speed samples (speed chart, queue ETA)

# Postprocessor (pp_key) -> sub-step shown while a video is processing. Keys starting with "_" are ours.
PP_STEPS = {"Merger": "Merging", "ExtractAudio": "Converting audio", "EmbedThumbnail": "Embedding thumbnail",
            "Metadata": "Writing metadata", "EmbedSubtitle": "Embedding subtitles",
            "ModifyChapters": "Removing sponsor segments", "VideoConvertor": "Converting", "VideoRemuxer": "Remuxing"}
PP_QUIET = {"ThumbnailsConvertor", "SponsorBlock", "MoveFilesAfterDownload"}   # run before/around the download


class Manager:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        # One app per library: a second copy would fight over the same database and files.
        self._instance_lock = _FileLock(DATA_DIR / ".lock")
        if not self._instance_lock.acquire():
            raise AlreadyRunning(f"YT Downloader is already running with this library ({DATA_DIR}).")
        setup_logging()
        self.cond = threading.Condition(threading.RLock())
        self.jobs: dict[str, dict] = {}
        try:
            self.settings = _coerce_settings(json.loads(SETTINGS_FILE.read_text()))
        except (OSError, ValueError):
            self.settings = _coerce_settings({})
        self.active = 0
        self.inflight: set[tuple[str, str]] = set()   # (folder, archive key) being downloaded right now
        self.hold: dict | None = None                 # queue-wide pause: {"reason", "until"}
        self.rate_strikes = 0
        self.next_start = 0.0                         # "pause between videos" spacing
        self.net_level = lambda: "checking"           # wired to NetMonitor by the server
        self.on_network_error = lambda: None
        self._dirty = False
        self._saved_rev: dict[str, int] = {}          # job id -> rev already written to the database
        self._save_lock = threading.Lock()
        self._archive_lock = threading.Lock()
        self._last_backup = time.time()
        self._live: dict[tuple[str, str], tuple[dict, dict]] = {}   # queued/active items, for the scheduler
        self._order: list | None = None                # _live in scheduler order (None: sort again)
        self.version = 0                               # bumped on every change; the live stream watches it
        self.speed_hist: deque = deque(maxlen=150)     # total speed every SPEED_SAMPLE seconds (5 minutes)
        self.clock = time.time                         # for the download schedule (tests replace it)
        self._ydls: set = set()                        # running YoutubeDL objects, for the total speed limit
        self.importing: dict | None = None             # progress of "add many links"
        self.updating: dict | None = None              # progress of a yt-dlp update
        self._latest = {"version": None, "checked": 0, "checking": False}   # newest yt-dlp on PyPI
        self.store = Store(DB_FILE, DB_BACKUP)
        self._load_state()
        self.last_event = self.store.last_event_id()
        if self.store.version < 1:
            self._backfill_events()
        if not DB_BACKUP.exists():
            self.store.backup()                          # never run without a backup to fall back on
        log.info("Started with %d playlists", len(self.jobs))
        for i in range(MAX_WORKERS):
            threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True).start()
        threading.Thread(target=self._saver, name="state-saver", daemon=True).start()
        threading.Thread(target=self._sampler, name="speed-sampler", daemon=True).start()
        threading.Thread(target=self._syncer, name="auto-sync", daemon=True).start()

    # ---- persistence

    def _load_state(self):
        jobs = self.store.load()
        migrated = False
        if not jobs and (LEGACY_STATE.exists() or LEGACY_BACKUP.exists()):
            jobs, migrated = _read_legacy_state(), True
        for job in jobs:
            job.setdefault("heights", VIDEO_HEIGHTS)
            job.setdefault("width", 2)
            job.setdefault("source_id", None)
            job.setdefault("updated", job.get("created"))
            job.pop("cancelled", None)
            job.pop("saved", None)
            if (job.get("check") or {}).get("running"):
                job["check"].update(running=False, stop=False)   # a check interrupted by a restart
            for it in job["items"]:
                if it["status"] in ACTIVE:
                    it["status"] = "queued"              # interrupted by a restart: resume it
                elif it["status"] == "cancelled":
                    it["status"] = "new"
                it.setdefault("options", job.get("options", {}))
                it.setdefault("outdir", job["outdir"])
                it.setdefault("net_attempts", 0)
                it.setdefault("removed", False)
                it.setdefault("log", [])
                it.setdefault("priority", 0)
                it.setdefault("held", False)
                it.setdefault("history", [])
                it.update(speed=None, eta=None, next_try=0, cancel=False, step=None)
            self.jobs[job["id"]] = job
            self._reindex(job)
            if not migrated:
                self._saved_rev[job["id"]] = job["rev"]
        if migrated:
            self.save_state()                            # write everything into the database first …
            for path in (LEGACY_STATE, LEGACY_BACKUP):   # … then retire the JSON files (kept, not deleted)
                if path.exists():
                    path.replace(path.with_name(path.name + ".migrated"))
            log.info("Moved %d playlists from state.json into library.db", len(jobs))

    def save_state(self):
        """Write only playlists and videos that changed since the last save."""
        with self._save_lock:
            with self.cond:
                jobs, items = [], []
                for jid, job in self.jobs.items():
                    since = self._saved_rev.get(jid, -1)
                    if job["rev"] > since:
                        jobs.append((jid, json.dumps({k: v for k, v in job.items() if k != "items"})))
                        items += [(jid, it["id"], json.dumps(it)) for it in job["items"] if it["rev"] > since]
                deleted = [jid for jid in self._saved_rev if jid not in self.jobs]
                revs = {jid: job["rev"] for jid, job in self.jobs.items()}
                self._dirty = False
            try:
                self.store.write(jobs, items, deleted)
            except BaseException:
                self._dirty = True
                raise
            self._saved_rev = revs
            if time.time() - self._last_backup > BACKUP_EVERY:
                self.store.backup()
                self._last_backup = time.time()

    def _sampler(self):
        while True:
            time.sleep(SPEED_SAMPLE)
            self.speed_hist.append(self.total_speed())

    def _backfill_events(self):
        """First start with the event log: add the videos downloaded before it existed, dated by their files."""
        rows = []
        with self.cond:
            items = [(job, it) for job in self.jobs.values() for it in job["items"]
                     if it["status"] in ("done", "skipped") and it.get("filepath")]
        for job, it in items:
            try:
                st = os.stat(it["filepath"])
            except OSError:
                continue
            rows.append({"time": it.get("finished") or st.st_mtime, "job_id": job["id"], "job_title": job["title"],
                         "item_id": it["id"], "title": it["title"], "url": it.get("url"), "status": "done",
                         "bytes": st.st_size, "filepath": it["filepath"]})
        rows.sort(key=lambda r: r["time"])
        try:
            self.last_event = self.store.add_events(rows)
            self.store.set_version(1)
        except sqlite3.Error as exc:
            log.error("Could not fill the event log: %s", exc)
            return
        if rows:
            log.info("Event log: added %d earlier downloads", len(rows))

    def _event(self, job: dict, item: dict, status: str, kind: str | None = None):
        """Record a finished or failed download in the event log (call outside self.cond)."""
        with self.cond:
            fp = item.get("filepath") if status == "done" else None
            started, finished = item.get("started"), item.get("finished") or time.time()
            row = {"time": finished, "job_id": job["id"], "job_title": job["title"], "item_id": item["id"],
                   "title": item["title"], "url": item.get("url"), "status": status, "kind": kind,
                   "error": item.get("error") if status == "failed" else None, "filepath": fp,
                   "seconds": finished - started if status == "done" and started else None}
        try:
            row["bytes"] = os.path.getsize(fp) if fp else None
        except OSError:
            row["bytes"] = None
        try:
            last = self.store.add_events([row])
        except sqlite3.Error as exc:
            log.error("Could not write to the event log: %s", exc)
            return
        with self.cond:
            self.last_event = last
            self.version += 1

    def _saver(self):
        while True:
            time.sleep(3)
            if self._dirty:
                try:
                    self.save_state()
                except (OSError, sqlite3.Error) as exc:
                    log.error("Could not save the library: %s", exc)

    def update_settings(self, patch: dict) -> dict:
        with self.cond:
            self.settings = _coerce_settings(self.settings | {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            _atomic_write(SETTINGS_FILE, json.dumps(self.settings, indent=2))
            self.version += 1
            self.cond.notify_all()
            self._rebalance()
            log.info("Settings changed: %s", {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            return dict(self.settings)

    def _rate_for(self, running: int) -> float | None:
        """Speed limit for each download (bytes/s): the per-download limit, or an equal share of the total."""
        limits = []
        if self.settings.get("rate_limit_mb"):
            limits.append(float(self.settings["rate_limit_mb"]) * 1024 * 1024)
        if self.settings.get("total_rate_mb"):
            limits.append(float(self.settings["total_rate_mb"]) * 1024 * 1024 / max(1, running))
        return min(limits) if limits else None

    def _rebalance(self):
        """Share the total speed limit between the downloads running now. yt-dlp reads params["ratelimit"]
        on every chunk, so this applies to downloads already in progress."""
        with self.cond:
            rate = self._rate_for(len(self._ydls))
            for ydl in self._ydls:
                ydl.params["ratelimit"] = rate

    # ---- helpers

    def _touch(self, job: dict, item: dict | None = None):
        """Publish a change. With an item, only that item changed; without one, any item of the job may have."""
        with self.cond:
            job["rev"] += 1
            job["updated"] = time.time()
            if item is not None:
                item["rev"] = job["rev"]
                self._index(job, item)
            else:
                self._reindex(job)
            self.version += 1
            self._dirty = True

    # The scheduler's index: only queued and active items, so picking the next video and the dashboard
    # don't scan every video of every playlist.

    def _index(self, job: dict, item: dict):
        key = (job["id"], item["id"])
        if item["status"] in LIVE and self.jobs.get(job["id"]) is job:
            if key not in self._live:
                self._live[key] = (job, item)
                self._order = None
        elif self._live.pop(key, None):
            self._order = None

    def _reindex(self, job: dict):
        for key in [k for k in self._live if k[0] == job["id"]]:
            del self._live[key]
        if self.jobs.get(job["id"]) is job:
            for it in job["items"]:
                if it["status"] in LIVE:
                    self._live[(job["id"], it["id"])] = (job, it)
        self._order = None

    def _queue_order(self) -> list[tuple[dict, dict]]:
        """Queued and active items in the order the scheduler picks them: priority, then playlist age and order."""
        if self._order is None:
            self._order = sorted(self._live.values(),
                                 key=lambda p: (-(p[1].get("priority") or 0), p[0]["created"], p[1]["index"]))
        return self._order

    def _set(self, job: dict, item: dict, **changes):
        """Change an item under the lock and publish the change."""
        with self.cond:
            item.update(changes)
            if "priority" in changes:
                self._order = None
            self._touch(job, item)

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("Playlist not found")
        return job

    def _get_item(self, job: dict, item_id: str) -> dict:
        item = next((i for i in job["items"] if i["id"] == item_id), None)
        if not item:
            raise KeyError("Video not found")
        return item

    @staticmethod
    def _new_item(it: dict, kind: str, width: int, outdir: str, rev: int, number: int | None = None) -> dict:
        return {
            **it,
            "number": f"{number or it['index']:0{width}d}" if kind == "playlist" else "",
            "status": "new", "progress": 0.0, "speed": None, "eta": None,
            "downloaded": 0, "total": None, "error": None, "attempts": 0, "net_attempts": 0,
            "filepath": None, "next_try": 0, "rev": rev, "cancel": False,
            "options": {}, "outdir": outdir, "log": [], "priority": 0, "held": False, "history": [],
            "removed": bool(UNAVAILABLE_TITLE.match(it.get("title") or "")),
        }

    @staticmethod
    def _archive_key(item: dict) -> str:
        return f"{item['extractor'].lower()} {item['id']}"

    @staticmethod
    def _archive_ids(outdir: str) -> set[str]:
        try:
            return set((Path(outdir) / ARCHIVE_NAME).read_text(errors="ignore").splitlines())
        except OSError:
            return set()

    def _archive_add(self, outdir: str, key: str):
        with self._archive_lock, open(Path(outdir) / ARCHIVE_NAME, "a", encoding="utf-8") as f:
            f.write(key + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _archive_remove(self, outdir: str, *keys: str):
        path = Path(outdir) / ARCHIVE_NAME
        drop = set(keys)
        with self._archive_lock:
            try:
                lines = path.read_text(errors="ignore").splitlines()
            except OSError:
                return
            keep = [line for line in lines if line.strip() not in drop]
            if len(keep) != len(lines):
                _atomic_write(path, "".join(line + "\n" for line in keep))

    def _requeue(self, job: dict, item: dict, **changes):
        """Put a video (back) in the queue. Every re-queue goes through here so a missing or damaged file is
        always dropped from the archive first (otherwise the video would just be "skipped" again)."""
        self._forget_file(job, item)
        item.update(status="queued", error=None, attempts=0, net_attempts=0, next_try=0, cancel=False, held=False,
                    **changes)

    def _forget_file(self, job: dict, item: dict):
        """Before re-downloading a video whose file was found missing or damaged, drop it from the archive."""
        if item.get("file_problem"):
            self._archive_remove(item.get("outdir") or job["outdir"], self._archive_key(item))
            item.update(file_problem=None, filepath=None, health=None)

    def _mark_existing(self, job: dict):
        """Videos already in the folder's download archive count as downloaded."""
        have = self._archive_ids(job["outdir"])
        for it in job["items"]:
            if it["status"] == "new" and self._archive_key(it) in have:
                it.update(status="skipped", progress=100.0, rev=job["rev"] + 1)

    def _item_log(self, job: dict, item: dict, level: str, msg: str):
        msg = ANSI.sub("", str(msg)).strip()
        with self.cond:
            item.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {level.upper()}: {msg[:500]}")
            del item["log"][:-ITEM_LOG_LINES]
            self._touch(job, item)
        log.log(logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO,
                "[%s] %s", item["id"], msg)

    def _hold_queue(self, reason: str, seconds: float):
        with self.cond:
            until = time.time() + seconds
            if not self.hold or self.hold["until"] < until:
                self.hold = {"reason": reason, "until": until}
                self.version += 1
                log.warning("Queue paused for %ds: %s", seconds, reason)

    def _wait_for_network(self) -> bool:
        return bool(self.settings.get("wait_for_network", True))

    def _limit(self) -> int:
        """Parallel downloads allowed right now: fewer on a weak connection so each one can finish."""
        c = self.settings["concurrency"]
        if self._wait_for_network():
            level = self.net_level()
            if level == "weak":
                return 1
            if level == "fair":
                return min(c, 2)
        return c

    def network_changed(self, old: str, new: str):
        """Called by the connection monitor. When the connection comes back, retry waiting videos at once."""
        log.info("Connection: %s -> %s", old, new)
        if new in LIVE_LEVELS and old not in LIVE_LEVELS and old != "checking":
            with self.cond:
                for job in self.jobs.values():
                    for it in job["items"]:
                        if it["status"] == "queued" and it.get("net_attempts") and it["next_try"] > time.time():
                            it["next_try"] = 0
                self.cond.notify_all()
        with self.cond:
            self.version += 1

    def _in_schedule(self) -> bool:
        """True when downloads may start now (always, unless a download schedule is set)."""
        s = self.settings
        if not s.get("schedule_on"):
            return True
        t = time.localtime(self.clock())
        now, start, end = t.tm_hour * 60 + t.tm_min, _minutes(s["schedule_start"]), _minutes(s["schedule_end"])
        if start == end:
            return True
        return start <= now < end if start < end else now >= start or now < end   # e.g. 23:00–06:00

    def waiting_reason(self) -> str | None:
        """Why queued videos aren't starting, if it's a queue-wide reason."""
        now = time.time()
        if self.hold and now < self.hold["until"]:
            return f"{self.hold['reason']} (trying again in {_fmt_secs(self.hold['until'] - now)})"
        if not self._in_schedule():
            s = self.settings
            return (f"Outside the download schedule ({s['schedule_start']}–{s['schedule_end']}) — "
                    f"downloads start at {s['schedule_start']}")
        if self._wait_for_network():
            level = self.net_level()
            if level == "offline":
                return "No internet connection — downloads will continue when it's back"
            if level == "blocked":
                return "YouTube is unreachable — turn on your VPN or set a proxy; downloads continue automatically"
        return None

    # ---- library

    def open_url(self, url: str) -> dict:
        """Fetch a link and save it in the library (or update the saved copy). Returns the summary."""
        info = inspect_url(url, self.settings)          # slow network call: outside the lock
        with self.cond:
            job = next((j for j in self.jobs.values()
                        if (info.get("source_id") and j.get("source_id") == info["source_id"]) or j["url"] == url), None)
            if job:
                added = self._merge(job, info)
            else:
                job = self._create(url, info)
                added = len(job["items"])
            self._touch(job)
            self.cond.notify_all()
            log.info("Saved %r (%d videos, %d new)", job["title"], len(job["items"]), added)
            return self.summary(job) | {"added": added}

    def _create(self, url: str, info: dict) -> dict:
        outdir = Path(os.path.expanduser(self.settings["output_dir"]))
        if info["kind"] == "playlist":
            outdir = outdir / _safe_name(info["title"])
        width = max(2, len(str(len(info["items"]))))
        job = {
            "id": uuid.uuid4().hex[:12],
            "source_id": info.get("source_id"),
            "url": url,
            "title": info["title"],
            "kind": info["kind"],
            "thumbnail": info.get("thumbnail"),
            "heights": info.get("heights") or VIDEO_HEIGHTS,
            "outdir": str(outdir),
            "options": {"quality": "1080", "container": "mp4"},
            "created": time.time(),
            "updated": time.time(),
            "paused": False,
            "width": width,
            "rev": 1,
            "items": [self._new_item(it, info["kind"], width, str(outdir), 1) for it in info["items"]],
        }
        self._mark_existing(job)
        self.jobs[job["id"]] = job
        return job

    def _merge(self, job: dict, info: dict, added_ids: list | None = None) -> int:
        """Bring a saved playlist up to date with a fresh fetch.

        Known videos keep their status and file number; their position follows the playlist.
        New videos get the next free file number, so numbers never collide after a reorder.
        Videos that left the playlist (or became private/deleted) are flagged, never deleted.
        """
        fresh_items = info["items"]
        if not fresh_items:
            return 0                                     # an empty fetch is a glitch: don't flag everything
        by_id = {it["id"]: it for it in job["items"]}
        rev = job["rev"] + 1
        next_number = 1 + max((int(it["number"]) for it in job["items"] if str(it.get("number")).isdigit()),
                              default=0)
        new_count = sum(1 for f in fresh_items if f["id"] not in by_id)
        # Keep the number width unless the new numbers need more digits (e.g. going past 999).
        width = max(job.get("width", 2), len(str(next_number - 1 + new_count)))
        added = 0
        for fresh in fresh_items:
            gone = bool(UNAVAILABLE_TITLE.match(fresh["title"] or ""))
            old = by_id.get(fresh["id"])
            if old:
                changes = {}
                if old["index"] != fresh["index"]:
                    changes["index"] = fresh["index"]
                if old.get("removed") != gone:
                    changes["removed"] = gone
                if not gone and old["title"] != fresh["title"]:
                    changes["title"] = fresh["title"]
                if fresh.get("duration") and not old.get("duration"):
                    changes["duration"] = fresh["duration"]
                if changes:
                    old.update(changes, rev=rev)
            else:
                job["items"].append(self._new_item(fresh, job["kind"], width, job["outdir"], rev, number=next_number))
                next_number += 1
                added += 1
                if added_ids is not None and not gone:
                    added_ids.append(fresh["id"])
        # Videos no longer in the playlist go to the end, in their old order. If most of the playlist seems to
        # be gone, the fetch was probably cut short: keep their flags as they are instead of hiding them.
        fresh_ids = {f["id"] for f in fresh_items}
        missing = [i for i in job["items"] if i["id"] not in fresh_ids]
        known = sum(1 for i in job["items"] if not i.get("removed"))
        trust_removals = len(missing) <= max(10, known // 2)
        if not trust_removals:
            log.warning("Refresh of %r returned %d of %d videos; not marking the rest as removed",
                        job["title"], len(fresh_items), len(job["items"]))
        position = len(fresh_items)
        for it in sorted(missing, key=lambda i: i["index"]):
            position += 1
            changes = {"index": position} if it["index"] != position else {}
            if trust_removals and not it.get("removed"):
                changes["removed"] = True
            if changes:
                it.update(changes, rev=rev)
        job["items"].sort(key=lambda it: it["index"])
        job.update(title=info["title"] or job["title"], width=width,
                   thumbnail=info.get("thumbnail") or job.get("thumbnail"),
                   source_id=info.get("source_id") or job.get("source_id"))
        if info.get("heights"):
            job["heights"] = info["heights"]
        self._mark_existing(job)
        return added

    def refresh(self, job_id: str) -> int:
        return len(self._refresh(job_id))

    def _refresh(self, job_id: str) -> list[str]:
        """Fetch the playlist again; returns the ids of new videos."""
        job = self.get(job_id)
        info = inspect_url(job["url"], self.settings)
        new_ids: list[str] = []
        with self.cond:
            added = self._merge(job, info, new_ids)
            # Quick health check: a downloaded video whose file has disappeared is flagged.
            for it in job["items"]:
                if it["status"] == "done" and it.get("filepath") and not os.path.exists(it["filepath"]):
                    self._mark_missing(job, it)
            if job.get("sync"):
                job["sync"].update(last=time.time(), error=None)
            self._touch(job)
            log.info("Refreshed %r: %d new videos", job["title"], added)
            return new_ids

    # ---- subscriptions: refresh saved playlists automatically

    def set_sync(self, job_id: str, every_hours: float, auto_download: bool) -> dict:
        with self.cond:
            job = self.get(job_id)
            every = float(every_hours or 0)
            if every not in SYNC_CHOICES:
                raise ValueError("Choose how often to check: off, 6, 12, 24 hours or weekly")
            old = job.get("sync") or {}
            job["sync"] = {"every": every, "auto": bool(auto_download), "last": old.get("last") or time.time(),
                           "error": None}
            self._touch(job)
            log.info("Auto-check for %r: every %sh, auto-download %s", job["title"], every, bool(auto_download))
            return dict(job["sync"])

    def _syncer(self):
        while True:
            time.sleep(SYNC_TICK)
            try:
                self.sync_due()
            except Exception:
                log.exception("Automatic playlist check failed")

    def sync_due(self) -> list[str]:
        """Refresh every playlist whose automatic check is due; queue the new videos if asked. Returns the
        ids of the playlists checked."""
        if self.net_level() in ("offline", "blocked"):
            return []
        now = time.time()
        with self.cond:
            due = [j["id"] for j in self.jobs.values()
                   if (j.get("sync") or {}).get("every") and now - (j["sync"].get("last") or 0) >= j["sync"]["every"] * 3600]
        for job_id in due:
            try:
                new_ids = self._refresh(job_id)
            except KeyError:
                continue
            except Exception as exc:
                with self.cond:
                    job = self.jobs.get(job_id)
                    if job and job.get("sync"):
                        # try again in 30 minutes rather than at every tick
                        job["sync"].update(error=_clean_error(exc), last=now - job["sync"]["every"] * 3600 + 1800)
                        self._touch(job)
                log.warning("Automatic check of %s failed: %s", job_id, _clean_error(exc))
                continue
            with self.cond:
                job = self.jobs.get(job_id)
                if job and new_ids and job["sync"].get("auto"):
                    paused = job["paused"]
                    try:
                        n = self.queue(job_id, new_ids, dict(job["options"]))
                    except (OSError, ValueError) as exc:
                        log.warning("Auto-download of new videos in %r failed: %s", job["title"], exc)
                        continue
                    job["paused"] = paused                   # queueing must not resume a paused playlist
                    self._touch(job)
                    log.info("Auto-download: queued %d new videos of %r", n, job["title"])
        return due

    # ---- adding many links at once

    def open_many(self, urls: list[str]) -> dict:
        """Add a list of links one after another in the background (progress in overview()["import"])."""
        clean, seen = [], set()
        for url in urls:
            url = str(url).strip()
            if re.match(r"https?://", url) and url not in seen:
                seen.add(url)
                clean.append(url)
        if not clean:
            raise ValueError("No links found — paste links starting with https://")
        with self.cond:
            if self.importing and self.importing["running"]:
                raise ValueError("Still adding the previous links — wait for them to finish")
            self.importing = {"running": True, "total": len(clean[:500]), "done": 0, "added": 0, "failed": [],
                              "current": None, "started": time.time()}
            self.version += 1
        threading.Thread(target=self._import, args=(clean[:500],), name="import-links", daemon=True).start()
        return dict(self.importing)

    def _import(self, urls: list[str]):
        for url in urls:
            with self.cond:
                self.importing["current"] = url
                self.version += 1
            try:
                self.open_url(url)
                ok = True
            except Exception as exc:
                ok = False
                err = _clean_error(exc)
            with self.cond:
                self.importing["done"] += 1
                if ok:
                    self.importing["added"] += 1
                else:
                    self.importing["failed"].append({"url": url, "error": err})
                self.version += 1
        with self.cond:
            self.importing.update(running=False, current=None, finished=time.time())
            self.version += 1
        log.info("Added %d of %d links", self.importing["added"], len(urls))

    # ---- size estimate before queueing

    def estimate(self, job_id: str, ids: list[str] | None, options: dict) -> dict:
        options = dict(options)
        options["section"] = _parse_section(options.get("section"))
        with self.cond:
            job = self.get(job_id)
            wanted = set(ids) if ids is not None else None
            chosen = [it for it in job["items"] if it["status"] in ("new", "failed") and not it.get("removed")
                      and (wanted is None or it["id"] in wanted)]
            sizes = [_estimate_bytes(it, options) for it in chosen]
            outdir = os.path.expanduser((options.get("output_dir") or "").strip()) or job["outdir"]
        total = sum(b for b, _ in sizes)
        disk = _disk_free(outdir)
        free = disk.get("free")
        return {"bytes": total, "count": len(chosen), "unknown": sum(1 for (b, _) in sizes if not b),
                "free": free, "problem": disk.get("problem"),
                "low": free is not None and total > free - MIN_FREE_BYTES}

    # ---- yt-dlp version and update

    def ytdlp_info(self) -> dict:
        """The yt-dlp version in use, and the newest one on PyPI (looked up in the background, cached)."""
        with self.cond:
            stale = time.time() - self._latest["checked"] > YTDLP_CHECK_EVERY and not self._latest["checking"]
            if stale:
                self._latest["checking"] = True
        if stale:
            threading.Thread(target=self._check_latest, name="ytdlp-latest", daemon=True).start()
        with self.cond:
            return {"version": yt_dlp.version.__version__, "latest": self._latest["version"],
                    "checked": self._latest["checked"] or None, "update": dict(self.updating) if self.updating else None}

    def _check_latest(self):
        version = None
        try:
            with yt_dlp.YoutubeDL(_base_opts(self.settings)) as ydl:
                data = json.loads(ydl.urlopen("https://pypi.org/pypi/yt-dlp/json").read())
            version = data["info"]["version"]
        except Exception as exc:
            log.info("Couldn't check for a newer yt-dlp: %s", _clean_error(exc))
        with self.cond:
            self._latest.update(checking=False, checked=time.time() if version else 0, version=version or self._latest["version"])
            self.version += 1

    def _update_cmd(self) -> list[str]:
        if shutil.which("uv"):
            return ["uv", "pip", "install", "-q", "-p", sys.executable, "--upgrade", "yt-dlp[default]"]
        return [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp[default]"]

    @staticmethod
    def _installed_ytdlp() -> str | None:
        """Version on disk (may be newer than the one loaded until the app restarts)."""
        try:
            res = subprocess.run([sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
                                 capture_output=True, text=True, timeout=60)
            return res.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def update_ytdlp(self) -> dict:
        with self.cond:
            if self.updating and self.updating["running"]:
                return dict(self.updating)
            self.updating = {"running": True, "ok": None, "output": "", "installed": None, "started": time.time()}
            self.version += 1
        threading.Thread(target=self._run_update, name="ytdlp-update", daemon=True).start()
        return dict(self.updating)

    def _run_update(self):
        try:
            res = subprocess.run(self._update_cmd(), capture_output=True, text=True, timeout=600)
            ok, output = res.returncode == 0, (res.stdout + res.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, output = False, str(exc)
        installed = self._installed_ytdlp() if ok else None
        with self.cond:
            self.updating.update(running=False, ok=ok, output=output[-2000:], installed=installed,
                                 restart=bool(installed and installed != yt_dlp.version.__version__))
            if installed:
                self._latest.update(version=installed if installed > (self._latest["version"] or "") else self._latest["version"])
            self.version += 1
        log.info("yt-dlp update: %s (installed %s)", "ok" if ok else "failed", installed)

    # ---- file health ("Check files") and clean-up

    def _mark_missing(self, job: dict, it: dict):
        it.update(status="failed", health="missing", file_problem="missing", checked=time.time(), rev=job["rev"] + 1,
                  error="File check: the file is not in the folder (moved or deleted?) — press Retry to download it again")

    def start_check(self, job_id: str) -> bool:
        """Verify every downloaded video of a playlist, and pick up good files already in the folder."""
        with self.cond:
            job = self.get(job_id)
            if (job.get("check") or {}).get("running"):
                return False
            ids = [it["id"] for it in job["items"] if it["status"] in ("done", "skipped", "new", "failed")]
            job["check"] = {"running": True, "stop": False, "done": 0, "total": len(ids), "ok": 0, "damaged": 0,
                            "missing": 0, "found": 0, "started": time.time(), "finished": None}
            self._touch(job)
        log.info("Checking %d files of %r", len(ids), job["title"])
        threading.Thread(target=self._run_check, args=(job, ids), name="file-check", daemon=True).start()
        return True

    def stop_check(self, job_id: str):
        with self.cond:
            job = self.get(job_id)
            if (job.get("check") or {}).get("running"):
                job["check"]["stop"] = True
                self._touch(job)

    def _run_check(self, job: dict, ids: list[str]):
        by_id = {it["id"]: it for it in job["items"]}
        folders: dict[str, dict[str, list[str]]] = {}

        def candidates(it: dict) -> list[str]:
            outdir = it.get("outdir") or job["outdir"]
            if outdir not in folders:
                index: dict[str, list[str]] = {}
                try:
                    names = os.listdir(outdir)
                except OSError:
                    names = []
                for name in names:
                    m = FINAL_FILE.search(name)
                    if m:
                        index.setdefault(m.group(1), []).append(os.path.join(outdir, name))
                folders[outdir] = index
            return folders[outdir].get(it["id"], [])

        work = []
        for vid in ids:
            it = by_id.get(vid)
            if it:
                known = it.get("filepath")
                paths = ([known] if known and os.path.isfile(known) else []) + \
                        [p for p in candidates(it) if p != known]
                work.append((it, it["status"], paths))

        def verify(path: str, duration: float | None) -> tuple[str | None, bool]:
            """-> (problem, rescued). A file ffprobe can't open is only damaged if it also fails a full decode."""
            problem = _verify_file(path, duration)
            if problem == DAMAGED and _decodes_fully(path, duration):
                return None, True
            return problem, False

        def check_one(args):
            it, was, paths = args
            if job["check"]["stop"] or job["id"] not in self.jobs:
                return
            try:
                if was in ("done", "skipped"):
                    if not paths:
                        outcome, path, problem, rescued = "missing", None, None, False
                    else:
                        path, (problem, rescued) = paths[0], verify(paths[0], _expected_duration(it))
                        outcome = "damaged" if problem else "ok"
                elif paths:                                   # not marked downloaded, but a file is there
                    path, (problem, rescued) = paths[0], verify(paths[0], it.get("duration"))
                    outcome = "damaged-copy" if problem else "found"
                else:
                    outcome, path, problem, rescued = "nothing", None, None, False
                self._apply_check(job, it, was, outcome, path, problem, rescued)
            except Exception:
                log.exception("File check failed for %s", it["id"])

        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            list(pool.map(check_one, work))
        with self.cond:
            chk = job["check"]
            chk.update(running=False, finished=time.time())
            self._touch(job)
        log.info("File check of %r: %s", job["title"],
                 {k: chk[k] for k in ("done", "ok", "damaged", "missing", "found")} | {"stopped": chk["stop"]})

    def _apply_check(self, job: dict, it: dict, was: str, outcome: str, path: str | None, problem: str | None,
                     rescued: bool = False):
        note = "File check: ffprobe couldn't read the file, but it plays to the end — kept" if rescued else None
        with self.cond:
            chk = job["check"]
            chk["done"] += 1
            if it["status"] != was:                           # queued or downloading meanwhile: leave it alone
                self._touch(job)
                return
            now = time.time()
            if outcome == "ok":
                it.update(filepath=path, health="ok", checked=now, file_problem=None)
                chk["ok"] += 1
            elif outcome == "missing":
                self._mark_missing(job, it)
                chk["missing"] += 1
                note = "File check: file missing from the folder"
            elif outcome == "damaged":
                aside = _move_aside(path)
                it.update(status="failed", health="damaged", file_problem="damaged", filepath=None, checked=now,
                          error=f"File check: {problem}. It was renamed to “{os.path.basename(aside)}” — "
                                "press Retry to download it again")
                chk["damaged"] += 1
                note = f"File check: {problem} → renamed to {os.path.basename(aside)}"
            elif outcome == "found":
                outdir, key = it.get("outdir") or job["outdir"], self._archive_key(it)
                if key not in self._archive_ids(outdir):
                    self._archive_add(outdir, key)
                it.update(status="skipped", progress=100.0, filepath=path, health="ok", checked=now, error=None,
                          file_problem=None)
                chk["found"] += 1
                note = f"File check: found a good copy in the folder ({os.path.basename(path)})"
            elif outcome == "damaged-copy":
                aside = _move_aside(path)
                it.update(error=f"File check: found a damaged copy in the folder ({problem}); "
                                f"it was renamed to “{os.path.basename(aside)}”")
                chk["damaged"] += 1
                note = f"File check: damaged copy {problem} → renamed to {os.path.basename(aside)}"
            self._touch(job, it)
        if note:
            self._item_log(job, it, "info" if outcome in ("found", "ok") else "warning", note)

    def cleanup(self, job_id: str, dry_run: bool = True) -> dict:
        """Find (and delete) leftovers of this playlist: partial downloads, unmerged streams, stray
        thumbnails/subtitles and renamed damaged files. Videos queued or downloading are never touched."""
        with self.cond:
            job = self.get(job_id)
            entries: dict[str, dict[str, str]] = {}        # folder -> {video id: status}
            for it in job["items"]:
                outdir = it.get("outdir") or job["outdir"]
                busy = it["status"] in ("queued", *ACTIVE) or self._flight_key(job, it) in self.inflight
                if not busy:
                    entries.setdefault(outdir, {})[it["id"]] = it["status"]
        files = []
        for outdir, statuses in entries.items():
            try:
                names = os.listdir(outdir)
            except OSError:
                continue
            media: dict[str, str] = {}                 # video id -> its finished file
            owned = []
            for name in names:
                tags = ID_TAG.findall(name)
                vid = next((t for t in reversed(tags) if t in statuses), None)
                if vid is None:
                    continue
                is_part = re.search(re.escape(f"[{vid}]") + r"\.f[\w-]+\.\w+$", name)
                owned.append((name, vid, is_part))
                if not (is_part or LEFTOVER.search(name) or SIDECAR.search(name)):
                    media[vid] = os.path.join(outdir, name)
            for name, vid, is_part in owned:
                downloaded = statuses[vid] in ("done", "skipped")
                # A downloaded video's thumbnail is a leftover only once the picture is inside the video file.
                stray = SIDECAR.search(name) and (not downloaded or IMAGE_EXT.search(name) and vid in media
                                                  and _has_cover(media[vid]))
                if LEFTOVER.search(name) or is_part or stray:
                    path = os.path.join(outdir, name)
                    if os.path.isfile(path):
                        files.append(path)
        size = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        if not dry_run and files:
            _remove(files)
            log.info("Cleaned up %d leftover files (%d bytes) of %r", len(files), size, job["title"])
        return {"files": len(files), "bytes": size, "names": [os.path.basename(p) for p in files[:50]]}

    def queue(self, job_id: str, ids: list[str] | None, options: dict) -> int:
        """Queue the chosen videos (all downloadable, not-downloaded ones when ids is None)."""
        with self.cond:
            job = self.get(job_id)
            outdir = os.path.expanduser((options.pop("output_dir", "") or "").strip()) or job["outdir"]
            section = _parse_section(options.pop("section", None))
            problem = _folder_problem(outdir)
            if problem:
                raise OSError(problem)
            job.update(outdir=outdir, options=dict(options), paused=False)   # a part is only for this download
            if section:
                options["section"] = section
            wanted = set(ids) if ids is not None else None
            count = 0
            for it in job["items"]:
                if it["status"] not in ("new", "failed"):
                    continue
                if wanted is None and it.get("removed") or wanted is not None and it["id"] not in wanted:
                    continue
                self._requeue(job, it, options=dict(options), outdir=outdir, progress=0.0, priority=0,
                              rev=job["rev"] + 1)
                count += 1
            self._touch(job)
            self.cond.notify_all()
            log.info("Queued %d videos of %r (%s)", count, job["title"], options.get("quality"))
            return count

    def action(self, job_id: str, action: str):
        with self.cond:
            job = self.get(job_id)
            if action == "pause":
                job["paused"] = True
            elif action == "resume":
                job["paused"] = False
            elif action == "stop":                       # un-queue everything still waiting/running
                for it in job["items"]:
                    if it["status"] == "queued":
                        it.update(status="new", error=None, rev=job["rev"] + 1)
                    elif it["status"] in ACTIVE:
                        it["cancel"] = True
                job["paused"] = False
            elif action == "retry":
                job["paused"] = False
                self.hold = None
                for it in job["items"]:
                    if it["status"] == "failed" or (it["status"] == "queued" and it["next_try"] > time.time()):
                        self._requeue(job, it, rev=job["rev"] + 1)
            elif action == "remove":
                self.remove(job_id)
                return
            elif action == "open":
                problem = _folder_problem(job["outdir"])
                if problem:
                    raise ValueError(problem)
                _open_folder(job["outdir"])
                return
            else:
                raise ValueError(f"Unknown action {action}")
            log.info("%s %r", action.capitalize(), job["title"])
            self._touch(job)
            self.cond.notify_all()

    def item_action(self, job_id: str, item_id: str, action: str):
        with self.cond:
            job = self.get(job_id)
            item = self._get_item(job, item_id)
            st = item["status"]
            if action == "cancel":
                if st == "queued":
                    item.update(status="new", error=None, held=False)
                elif st in ACTIVE:
                    item["cancel"] = True
            elif action == "retry" and st in ("failed", "queued"):
                self._requeue(job, item)
            elif action == "hold":                      # pause this one video; a running one stops and keeps its .part
                if st not in LIVE:
                    raise ValueError("Only videos in the queue can be paused")
                item["held"] = True
            elif action == "unhold":
                item["held"] = False
            elif action == "now":                       # skip the wait before the next retry
                if st == "queued":
                    item.update(next_try=0, held=False)
            elif action in ("reveal", "play"):
                path = item.get("filepath")
                if not path or not os.path.isfile(path):
                    raise ValueError("The file isn't in the folder any more")
                _open_path(path, reveal=action == "reveal")
                return
            elif action == "delete-file":
                self._delete_files(job, item)
            elif action == "mark-done":
                if st not in ("new", "failed"):
                    raise ValueError("Only videos that aren't downloaded can be marked as downloaded")
                outdir = item.get("outdir") or job["outdir"]
                problem = _folder_problem(outdir)
                if problem:
                    raise ValueError(problem)
                if self._archive_key(item) not in self._archive_ids(outdir):
                    self._archive_add(outdir, self._archive_key(item))
                item.update(status="skipped", progress=100.0, error=None, file_problem=None, outdir=outdir)
                log.info("Marked %s as downloaded", item["id"])
            else:
                raise ValueError(f"Unknown action {action}")
            self._touch(job, item)
            self.cond.notify_all()

    def _video_files(self, outdir: str, vid: str) -> list[Path]:
        """Every file in outdir whose last "[...]" tag is this video id (final file, parts, sidecars)."""
        try:
            entries = list(Path(outdir).iterdir())
        except OSError:
            return []
        return [p for p in entries if (ID_TAG.findall(p.name) or [None])[-1] == vid and p.is_file()]

    def _delete_files(self, job: dict, item: dict):
        """Delete a downloaded video's files (on the user's request) and mark it not downloaded."""
        if item["status"] in LIVE or self._flight_key(job, item) in self.inflight:
            raise ValueError("Stop the download first")
        outdir = item.get("outdir") or job["outdir"]
        files = self._video_files(outdir, item["id"])
        size = sum(p.stat().st_size for p in files)
        _remove(files)
        self._archive_remove(outdir, self._archive_key(item))
        item.update(status="new", filepath=None, health=None, file_problem=None, progress=0.0, error=None)
        log.info("Deleted %d files (%d bytes) of %s", len(files), size, item["id"])

    # ---- queue-wide controls

    def move(self, job_id: str, item_id: str, to: str | None = None, before: tuple[str, str] | None = None):
        """Reorder the queue: to="top"/"bottom", or drop the video just before another one (before=(job, id),
        None = at the end)."""
        with self.cond:
            job = self.get(job_id)
            item = self._get_item(job, item_id)
            if item["status"] != "queued":
                raise ValueError("Only videos waiting in the queue can be moved")
            queued = [(j, it) for j, it in self._queue_order() if it["status"] == "queued"]
            prios = [it.get("priority") or 0 for j, it in queued if it is not item]
            if to == "top":
                item["priority"] = max(prios, default=0) + 1
            elif to == "bottom":
                item["priority"] = min(prios, default=0) - 1
            else:
                order = [(j, it) for j, it in queued if it is not item]
                pos = next((i for i, (j, it) in enumerate(order) if before and (j["id"], it["id"]) == tuple(before)),
                           len(order))
                order.insert(pos, (job, item))
                for i, (j, it) in enumerate(order):     # number the whole queue in its new order
                    if it.get("priority") != len(order) - i:
                        it["priority"] = len(order) - i
                        if it is not item:
                            self._touch(j, it)
            self._order = None
            self._touch(job, item)
            self.cond.notify_all()

    def clear_hold(self):
        """The "Try now" on a queue-wide pause (rate limit, disk full) once the user has fixed the cause."""
        with self.cond:
            self.hold = None
            self.next_start = 0.0
            self.version += 1
            self.cond.notify_all()
        log.info("Queue-wide pause cleared by the user")

    def retry_failed(self) -> int:
        """Retry every failed video (that can still be downloaded) in every playlist."""
        count = 0
        with self.cond:
            for job in self.jobs.values():
                n = 0
                for it in job["items"]:
                    if it["status"] == "failed" and not it.get("removed"):
                        self._requeue(job, it, rev=job["rev"] + 1)
                        n += 1
                if n:
                    self._touch(job)
                    count += n
            self.cond.notify_all()
        log.info("Retry all failed: %d videos", count)
        return count

    def pause_all(self, paused: bool):
        with self.cond:
            for job in self.jobs.values():
                if job["paused"] != paused:
                    job["paused"] = paused
                    self._touch(job)
            self.cond.notify_all()
        log.info("%s all playlists", "Paused" if paused else "Resumed")

    def remove(self, job_id: str, delete_files: bool = False):
        """Remove a playlist from the library; with delete_files, also delete the files it downloaded."""
        with self.cond:
            job = self.get(job_id)
            for it in job["items"]:
                it["cancel"] = True
            del self.jobs[job_id]
            self._touch(job)
            self.cond.notify_all()
            keys = {self._flight_key(job, it) for it in job["items"]}
        log.info("Removed %r%s", job["title"], " and its files" if delete_files else "")
        if delete_files:
            threading.Thread(target=self._delete_job_files, args=(job, keys), name="remove-files", daemon=True).start()

    def _delete_job_files(self, job: dict, keys: set):
        end = time.time() + 60
        while True:                                     # let cancelled downloads let go of their files first
            with self.cond:
                busy = keys & self.inflight
            if not busy:
                break
            if time.time() > end:
                log.warning("Not deleting the files of %r: downloads didn't stop", job["title"])
                return
            time.sleep(0.5)
        folders: dict[str, list[dict]] = {}
        for it in job["items"]:
            folders.setdefault(it.get("outdir") or job["outdir"], []).append(it)
        removed, size = 0, 0
        for outdir, items in folders.items():
            with self.cond:                             # never delete a video another playlist here still has
                shared = {it["id"] for j in self.jobs.values() for it in j["items"]
                          if os.path.normpath(it.get("outdir") or j["outdir"]) == os.path.normpath(outdir)}
            mine = [it for it in items if it["id"] not in shared]
            for it in mine:
                files = self._video_files(outdir, it["id"])
                size += sum(p.stat().st_size for p in files if p.exists())
                _remove(files)
                removed += len(files)
            self._archive_remove(outdir, *(self._archive_key(it) for it in mine))
            for leftover in (Path(outdir) / ARCHIVE_NAME, Path(outdir) / LOCK_DIR_NAME, Path(outdir)):
                try:
                    if leftover.is_dir():
                        is_root = os.path.normpath(leftover) == os.path.normpath(
                            os.path.expanduser(self.settings["output_dir"]))
                        if not is_root:
                            leftover.rmdir()            # only when empty
                    elif leftover.exists() and leftover.stat().st_size == 0:
                        leftover.unlink()
                except OSError:
                    pass
        log.info("Deleted %d files (%d bytes) of removed playlist %r", removed, size, job["title"])

    def export_m3u(self, job_id: str) -> dict:
        """Write "<playlist>.m3u8" into the playlist's folder, listing its downloaded videos in playlist order."""
        with self.cond:
            job = self.get(job_id)
            outdir = job["outdir"]
            entries = sorted((it["index"], it["title"], it.get("duration"), it["filepath"]) for it in job["items"]
                             if it["status"] in ("done", "skipped") and it.get("filepath"))
        lines = ["#EXTM3U", f"#PLAYLIST:{job['title']}"]
        count = 0
        for _, title, duration, path in entries:
            if not os.path.isfile(path):
                continue
            try:
                rel = os.path.relpath(path, outdir)
            except ValueError:                          # another drive (Windows)
                rel = path
            title = " ".join(str(title).split())
            lines += [f"#EXTINF:{int(duration) if duration else -1},{title}", rel.replace(os.sep, "/")]
            count += 1
        if not count:
            raise ValueError("No downloaded videos in this playlist yet")
        path = Path(outdir) / (_safe_name(job["title"]) + ".m3u8")
        _atomic_write(path, "\n".join(lines) + "\n")
        log.info("Exported %d videos of %r to %s", count, job["title"], path)
        return {"path": str(path), "name": path.name, "count": count}

    def item_log(self, job_id: str, item_id: str) -> dict:
        """Everything the detail views show about one video."""
        with self.cond:
            job = self.get(job_id)
            item = self._get_item(job, item_id)
            options = item.get("options") or job["options"]
            return {"title": item["title"], "error": item.get("error"), "lines": list(item.get("log") or []),
                    "status": item["status"], "options": dict(options), "outdir": item.get("outdir") or job["outdir"],
                    "filepath": item.get("filepath"), "fmt": item.get("fmt"), "step": item.get("step"),
                    "history": list(item.get("history") or []), "attempts": item.get("attempts") or 0,
                    "net_attempts": item.get("net_attempts") or 0, "started": item.get("started"),
                    "finished": item.get("finished")}

    # ---- views

    def summary(self, job: dict) -> dict:
        counts = {s: 0 for s in ("new", "queued", "downloading", "processing", "done", "failed", "removed")}
        speed = 0.0
        for it in job["items"]:
            key = "done" if it["status"] == "skipped" else it["status"]
            if it.get("removed") and key in ("new", "failed"):
                key = "removed"                          # gone from YouTube: not counted as missing
            counts[key] = counts.get(key, 0) + 1
            if it["status"] in ACTIVE:
                speed += it.get("speed") or 0
        total = len(job["items"])
        available = total - counts["removed"]
        active = counts["downloading"] + counts["processing"]
        waiting = counts["queued"]

        if active and job["paused"]:
            status = "pausing"
        elif active:
            status = "downloading"
        elif waiting:
            status = "paused" if job["paused"] else "queued"
        elif available and counts["done"] >= available:
            status = "complete"
        else:
            status = "saved"

        return {
            "id": job["id"], "title": job["title"], "kind": job["kind"], "url": job["url"],
            "thumbnail": job.get("thumbnail"), "outdir": job["outdir"], "options": job["options"],
            "heights": job["heights"], "created": job["created"], "updated": job.get("updated"),
            "status": status, "rev": job["rev"], "total": total, "counts": counts, "speed": speed,
            "percent": round(100 * min(counts["done"], available) / available, 2) if available else 0,
            "check": {k: v for k, v in (job.get("check") or {}).items() if k != "stop"} or None,
            "sync": job.get("sync"), "paused": job["paused"],
        }

    def items_since(self, job_id: str, since: int) -> dict:
        with self.cond:
            job = self.get(job_id)
            changed = [{k: it.get(k) for k in ITEM_KEYS} for it in job["items"] if it["rev"] > since]
            return {"rev": job["rev"], "items": changed}

    def total_speed(self) -> float:
        with self.cond:
            return sum(it.get("speed") or 0 for _, it in self._live.values() if it["status"] in ACTIVE)

    def overview(self) -> dict:
        with self.cond:
            jobs = [self.summary(j) for j in sorted(self.jobs.values(), key=lambda j: -(j.get("updated") or 0))]
        queued = sum(j["counts"]["queued"] for j in jobs)
        return {
            "jobs": jobs,
            "active": sum(j["counts"]["downloading"] + j["counts"]["processing"] for j in jobs),
            "speed": sum(j["speed"] for j in jobs),
            "waiting": self.waiting_reason() if queued else None,
            "settings": self.settings,
            "import": dict(self.importing) if self.importing else None,
            "last_event": self.last_event,
        }

    def activity(self, job_id: str | None = None, q: str | None = None) -> dict:
        """Everything in flight across all playlists, for the dashboard: what is downloading, what is
        queued (in the order the scheduler will pick it) and what finished recently."""
        now = time.time()
        q = (q or "").strip().lower()
        with self.cond:
            reason = self.waiting_reason()
            limit = self._limit()

            def row(job, it, **extra):
                return {"job": job["id"], "job_title": job["title"], "id": it["id"], "title": it["title"],
                        "url": it.get("url"), "index": it.get("index"), "duration": it.get("duration"),
                        "status": it["status"], "error": it.get("error"), "priority": it.get("priority") or 0,
                        "held": bool(it.get("held")), **extra}

            active, queue = [], []
            for job, it in self._queue_order():
                if job_id and job["id"] != job_id or q and q not in it["title"].lower() and q not in job["title"].lower():
                    continue
                st = it["status"]
                if st in ACTIVE:
                    active.append(row(job, it, progress=it.get("progress") or 0, speed=it.get("speed"),
                                      eta=it.get("eta"), downloaded=it.get("downloaded"), total=it.get("total"),
                                      started=it.get("started"), step=it.get("step"), fmt=it.get("fmt"),
                                      attempts=(it.get("attempts") or 0) + (it.get("net_attempts") or 0)))
                elif st == "queued":
                    if it.get("held"):
                        wait = "Paused"
                    elif job["paused"]:
                        wait = "Playlist paused"
                    elif it.get("next_try", 0) > now:
                        wait = f"Retry in {_fmt_secs(it['next_try'] - now)}"
                    else:
                        wait = reason or "Waiting for a free slot"
                    queue.append(row(job, it, wait=wait, progress=it.get("progress") or 0,
                                     next_try=it.get("next_try") or 0,
                                     retrying=it.get("next_try", 0) > now or bool(job["paused"]) or bool(it.get("held"))))
            speed_hist = list(self.speed_hist)
            jobs = [{"id": j["id"], "title": j["title"]} for j in sorted(self.jobs.values(), key=lambda j: j["title"].lower())]
            hold = dict(self.hold) if self.hold and now < self.hold["until"] else None
        recent = self.history(limit=30)["rows"]
        day = self.store.counts_since(now - 86400)
        return {
            "active": active,
            "queue": queue[:200], "queued": len(queue),
            "recent": recent,
            "jobs": jobs,
            "last_event": self.last_event,
            "stats": {
                "active": len(active), "limit": limit, "concurrency": self.settings["concurrency"],
                "queued": len(queue), "ready": sum(not q["retrying"] for q in queue),
                "speed": sum(a["speed"] or 0 for a in active),
                "speed_history": speed_hist[-60:],
                "done_today": day.get("done", 0),
                "failed_today": day.get("failed", 0),
                "waiting": reason,
                "hold": hold,
            },
        }

    def history(self, offset: int = 0, limit: int = 30, since_id: int = 0) -> dict:
        """Finished and failed downloads from the event log, newest first, each with the video's status now
        (so the page only offers Retry while the video is still failed)."""
        rows = self.store.events(offset, limit, since_id)
        with self.cond:
            by_job: dict[str, dict] = {}
            for r in rows:
                job = self.jobs.get(r["job_id"])
                if job and r["job_id"] not in by_job:
                    by_job[r["job_id"]] = {it["id"]: it for it in job["items"]}
                it = by_job.get(r["job_id"], {}).get(r["item_id"])
                r.update(event=r.pop("id"), job=r.pop("job_id"), id=r.pop("item_id"), finished=r.pop("time"),
                         current=it["status"] if it else None, current_file=it.get("filepath") if it else None)
        return {"rows": rows, "total": self.store.count_events(), "last_id": self.last_event}

    def clear_history(self) -> int:
        n = self.store.hide_events()
        with self.cond:
            self.version += 1
        return n

    def stats(self, days: int = 30) -> dict:
        days = max(1, min(int(days), 3650))
        return self.store.stats(time.time() - days * 86400) | {"period": days}

    # ---- workers

    def _flight_key(self, job: dict, item: dict) -> tuple[str, str]:
        return os.path.normpath(item.get("outdir") or job["outdir"]), self._archive_key(item)

    def _next_item(self):
        now = time.time()
        if (self.hold and now < self.hold["until"]) or now < self.next_start or not self._in_schedule():
            return None
        level = self.net_level() if self._wait_for_network() else "unknown"
        if level == "offline":
            return None
        for job, it in self._queue_order():
            if job["paused"] or it["status"] != "queued" or it.get("held") or it["next_try"] > now:
                continue
            if level == "blocked" and it["extractor"].lower().startswith("youtube"):
                continue
            if self._flight_key(job, it) in self.inflight:   # same video, same folder: never twice at once
                continue
            return job, it
        return None

    def _worker(self):
        while True:
            with self.cond:
                while True:
                    pick = self._next_item() if self.active < self._limit() else None
                    if pick:
                        break
                    self.cond.wait(timeout=2)
                job, item = pick
                key = self._flight_key(job, item)
                self.inflight.add(key)
                if self.settings.get("sleep_between"):
                    self.next_start = time.time() + float(self.settings["sleep_between"])
                item.update(status="downloading", speed=None, eta=None, started=time.time(), step=None)
                self.active += 1
                self._touch(job, item)
            try:
                self._download(job, item)
            except Exception as exc:  # never let a worker die
                log.exception("Unexpected error downloading %s", item["id"])
                self._set(job, item, status="failed", error=_clean_error(exc), finished=time.time())
                self._event(job, item, "failed", "other")
            finally:
                with self.cond:
                    self.active -= 1
                    self.inflight.discard(key)
                    item.update(speed=None, eta=None, cancel=False, step=None)
                    self._touch(job, item)
                    self.cond.notify_all()

    def _download(self, job: dict, item: dict):
        options = item.get("options") or job["options"]
        outdir = item.get("outdir") or job["outdir"]
        archive_key = self._archive_key(item)
        # A part of a video is never "the video": it isn't checked against or written to the archive.
        skip_existing = options.get("skip_existing", True) and not options.get("section")

        problem = _folder_problem(outdir)
        if problem:                                       # e.g. external drive unplugged: wait, don't fail
            self._set(job, item, status="queued", error=problem, next_try=time.time() + 60)
            return
        if skip_existing and archive_key in self._archive_ids(outdir):
            self._set(job, item, status="skipped", progress=100.0, error=None)
            return
        if shutil.disk_usage(outdir).free < MIN_FREE_BYTES:
            self._hold_queue("Disk almost full — free up space to continue", 60)
            self._set(job, item, status="queued", error="Waiting for free disk space")
            return

        # Another copy of the app (or another library) writing the same .part file would corrupt it.
        lock = _FileLock(_lock_path(outdir, archive_key))
        if not lock.acquire():
            self._set(job, item, status="queued", next_try=time.time() + 30,
                      error="Another copy of the app is downloading this video — waiting")
            return
        try:
            self._download_locked(job, item, options, outdir, archive_key, skip_existing)
        finally:
            lock.release()

    def _download_locked(self, job: dict, item: dict, options: dict, outdir: str, archive_key: str,
                         skip_existing: bool):
        # Re-check under the lock: the other copy may have just finished this video.
        if skip_existing and archive_key in self._archive_ids(outdir):
            self._set(job, item, status="skipped", progress=100.0, error=None)
            return
        self._item_log(job, item, "info", f"Download started ({options.get('quality', 'best')}) → {outdir}")
        last = [0.0]

        def stopping() -> bool:
            return item["cancel"] or item.get("held") or job["paused"] or job["id"] not in self.jobs

        def progress_hook(d):
            if stopping():
                raise DownloadCancelled()
            if d["status"] == "downloading":
                now = time.time()
                if now - last[0] < 0.4:
                    return
                last[0] = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                changes = dict(
                    status="downloading", step=None,  # a before_dl postprocessor may have flagged "processing"
                    downloaded=done, total=total, speed=d.get("speed"), eta=d.get("eta"),
                    progress=round(100 * done / total, 1) if total else item["progress"],
                )
                if item["error"] and done:
                    changes["error"] = None               # data is flowing again: clear the retry note
                self._set(job, item, **changes)

        remerge = [False]

        def pp_hook(d):
            name = d.get("postprocessor") or ""
            if d["status"] != "started" or name.startswith("_") or name in PP_QUIET:
                return
            step = "Re-merging from kept streams" if remerge[0] and name == "Merger" else PP_STEPS.get(name, "Processing")
            if item["status"] != "processing" or item.get("step") != step:
                self._set(job, item, status="processing", progress=100.0, speed=None, eta=None, step=step)

        fmt_opts, pps = build_format_opts(options)
        prefix = f"{item['number']} - " if item["number"] and options.get("numbering", True) else ""
        section = options.get("section")
        # A part gets its own name ("Title (1-00–2-30) [id].mp4"), so it never collides with the full video.
        suffix = f" ({_section_label(section)})" if section else ""
        tmpl = os.path.join(_escape_tmpl(outdir), _escape_tmpl(prefix) + "%(title).150B" + _escape_tmpl(suffix)
                            + " [%(id)s].%(ext)s")

        logger = _ItemLogger(lambda level, msg: self._item_log(job, item, level, msg))
        opts = _base_opts(self.settings, logger) | fmt_opts | {
            "outtmpl": {"default": tmpl},
            "postprocessors": pps,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "noplaylist": True,
            "fragment_retries": 15,
            "file_access_retries": 10,
            "skip_unavailable_fragments": False,   # a missing piece must fail the download, not leave a gap
            "continuedl": True,                    # resume .part files
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "overwrites": False,
        }
        rate = self._rate_for(len(self._ydls) + 1)
        if rate:
            opts["ratelimit"] = rate
        if section:
            opts["download_ranges"] = download_range_func(None, [(section["start"], section["end"] or float("inf"))])
            opts["force_keyframes_at_cuts"] = True
        if str(options.get("quality", "best")) not in AUDIO_FORMATS:
            # Keep the separate video/audio streams until the merged file is verified, so a bad merge can be
            # redone locally instead of downloading everything again. They are deleted below once it's good.
            opts["keepvideo"] = True

        try:
            for attempt in (1, 2):
                with yt_dlp.YoutubeDL(opts) as ydl:
                    if options.get("metadata", True) and job["kind"] == "playlist":
                        ydl.add_post_processor(_PlaylistTags({"album": job["title"], "track": item["number"].lstrip("0")}),
                                               when="pre_process")
                    ydl.add_post_processor(_FormatNote(lambda fmt: self._set(job, item, fmt=fmt)), when="before_dl")
                    if options.get("sponsorblock") in ("mark", "remove"):
                        cats = set(options.get("sponsor_categories") or SPONSOR_CATEGORIES)
                        ydl.add_post_processor(_SafeSponsorBlock(ydl, categories=cats), when="after_filter")
                    with self.cond:
                        self._ydls.add(ydl)
                    self._rebalance()
                    try:
                        info = ydl.extract_info(item["url"], download=True) or {}
                    finally:
                        with self.cond:
                            self._ydls.discard(ydl)
                        self._rebalance()
                done_info = (info.get("requested_downloads") or [{}])[0]
                filepath = done_info.get("filepath") or info.get("filepath")
                # The processed info has the real length: shorter for a part, or after cutting sponsor segments.
                expected = done_info.get("duration") or info.get("duration") or item.get("duration")
                if section:                               # yt-dlp doesn't shorten the duration for a part
                    end = section["end"] if section["end"] is not None else expected
                    expected = max(0.0, min(end, expected or end) - section["start"]) if end else None
                parts = _part_files(outdir, item["id"])
                self._set(job, item, status="processing", step="Checking stream sizes", speed=None, eta=None)
                bad = _check_part_sizes(info, parts)
                if bad:                                   # a stream itself is broken: get it again
                    _remove([filepath, *parts.values()] if filepath else parts.values())
                    raise _IncompleteFile(bad)
                live = info.get("is_live") or info.get("was_live")
                self._set(job, item, step="Verifying")
                problem = _verify_file(filepath, None if live else expected)
                if not problem:
                    break
                if filepath and os.path.isfile(filepath):
                    os.remove(filepath)                   # so the next attempt makes it again
                if attempt == 1 and parts:
                    self._item_log(job, item, "warning", f"{problem} — merging again from the downloaded streams")
                    remerge[0] = True
                    self._set(job, item, step="Re-merging from kept streams")
                    continue                              # yt-dlp reuses the streams on disk: no re-download
                _remove(parts.values())
                raise _IncompleteFile(problem)
            _remove(parts.values())                       # merged file verified: the streams aren't needed
            # keepvideo also stops yt-dlp deleting the thumbnail and subtitle files it embedded: remove them
            # here, but keep the thumbnail if the container couldn't take it (e.g. webm).
            sidecars = [p for p in _files_for(outdir, item["id"]) if SIDECAR.search(p.name) and str(p) != filepath]
            embedded_cover = options.get("thumbnail", True) and _has_cover(filepath)
            _remove(p for p in sidecars if (embedded_cover if IMAGE_EXT.search(p.name) else options.get("subtitles")))
            _remove(p for p in _files_for(outdir, item["id"]) if p.name.endswith(".damaged"))
            # The archive is written only now, after the file is verified, so a broken file is never "done".
            if skip_existing:
                self._archive_add(outdir, archive_key)
            self._set(job, item, status="done", progress=100.0, filepath=filepath, error=None, finished=time.time(),
                      attempts=0, net_attempts=0, health="ok", checked=time.time(), file_problem=None,
                      media_duration=None if live else expected, section=section)
            self._item_log(job, item, "info", f"Done and verified: {os.path.basename(filepath)}")
            self._event(job, item, "done")
            with self.cond:
                self.rate_strikes = 0
        except DownloadCancelled:
            # paused: back in the queue, the .part file resumes later. Stopped: back to "not downloaded".
            self._set(job, item, status="new" if item["cancel"] else "queued")
        except Exception as exc:
            if stopping():
                self._set(job, item, status="new" if item["cancel"] else "queued")
            else:
                self._handle_failure(job, item, exc)

    def _handle_failure(self, job: dict, item: dict, exc: Exception):
        msg = _clean_error(exc)
        kind = "network" if isinstance(exc, _IncompleteFile) else _error_kind(exc)
        now = time.time()
        with self.cond:
            if kind == "permanent":
                item.update(status="failed", error=msg)

            elif kind == "disk":
                self._hold_queue("Disk is full — free up space to continue", 120)
                item.update(status="queued", error="Disk full — waiting for free space")

            elif kind == "ratelimit":
                self.rate_strikes += 1
                wait = min(60 * 2 ** (self.rate_strikes - 1), 1800)       # 1, 2, 4 … 30 min
                self._hold_queue("YouTube is limiting requests — all downloads paused", wait)
                item["net_attempts"] += 1
                if item["net_attempts"] >= MAX_RATE_STRIKES:
                    item.update(status="failed", error=f"{msg} — try “Use cookies from browser” in Settings")
                else:
                    item.update(status="queued", error=f"YouTube is limiting requests — waiting {_fmt_secs(wait)}")

            elif kind == "network":
                item["net_attempts"] += 1
                n = item["net_attempts"]
                if n >= MAX_NET_ATTEMPTS:
                    item.update(status="failed", error=f"Gave up after {n} connection problems — {msg}")
                else:
                    backoff = min(10 * 2 ** (n - 1), 300)               # 10 s, 20 s, 40 s … 5 min
                    item.update(status="queued", next_try=now + backoff,
                                error=f"Connection problem (try {n}) — resuming in {_fmt_secs(backoff)}: {msg}")

            else:
                item["attempts"] += 1
                if item["attempts"] < MAX_ATTEMPTS:
                    backoff = 15 * item["attempts"] ** 2                # 15 s, 60 s, 135 s
                    item.update(status="queued", error=f"Retrying in {backoff}s — {msg}", next_try=now + backoff)
                else:
                    item.update(status="failed", error=msg)
            if item["status"] == "failed":
                item["finished"] = now
            item.setdefault("history", []).append({"t": now, "kind": kind, "msg": msg[:300],
                                                   "result": item["status"]})
            del item["history"][:-HISTORY_LEN]
            self._touch(job, item)
        self._item_log(job, item, "error", f"{kind} error → {item['status']}: {msg}")
        if item["status"] == "failed":
            self._event(job, item, "failed", kind)
        if kind == "network":
            self.on_network_error()


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
