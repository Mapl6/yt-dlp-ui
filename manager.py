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
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import fcntl                                   # POSIX file locks (macOS / Linux)
except ImportError:                                # Windows: locks are skipped
    fcntl = None

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ContentTooShortError, DownloadCancelled, ExtractorError, GeoRestrictedError

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
}

VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
AUDIO_FORMATS = {"mp3", "m4a", "opus"}

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
    return out


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
        return "The downloaded file is damaged"
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


class _FileLock:
    """An exclusive, non-blocking OS lock. Released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def acquire(self) -> bool:
        if fcntl is None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


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
            opts |= {"writesubtitles": True, "subtitleslangs": ["en.*", "en"], "subtitlesformat": "srt/best"}
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    if options.get("metadata", True):
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
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
    """

    def __init__(self, path: Path, backup: Path):
        self.path, self.backup_path = path, backup
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
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def load(self) -> list[dict]:
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
            self.conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, self.backup_path)


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
             "eta", "downloaded", "total", "error", "attempts", "filepath", "removed", "health")
LIVE_LEVELS = {"excellent", "good", "fair", "weak"}


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
        self.store = Store(DB_FILE, DB_BACKUP)
        self._load_state()
        if not DB_BACKUP.exists():
            self.store.backup()                          # never run without a backup to fall back on
        log.info("Started with %d playlists", len(self.jobs))
        for i in range(MAX_WORKERS):
            threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True).start()
        threading.Thread(target=self._saver, name="state-saver", daemon=True).start()

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
                it.update(speed=None, eta=None, next_try=0, cancel=False)
            self.jobs[job["id"]] = job
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
            self.cond.notify_all()
            log.info("Settings changed: %s", {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            return dict(self.settings)

    # ---- helpers

    def _touch(self, job: dict, item: dict | None = None):
        with self.cond:
            job["rev"] += 1
            job["updated"] = time.time()
            if item is not None:
                item["rev"] = job["rev"]
            self._dirty = True

    def _set(self, job: dict, item: dict, **changes):
        """Change an item under the lock and publish the change."""
        with self.cond:
            item.update(changes)
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
            "options": {}, "outdir": outdir, "log": [],
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

    def _archive_remove(self, outdir: str, key: str):
        path = Path(outdir) / ARCHIVE_NAME
        with self._archive_lock:
            try:
                lines = path.read_text(errors="ignore").splitlines()
            except OSError:
                return
            keep = [line for line in lines if line.strip() != key]
            if len(keep) != len(lines):
                _atomic_write(path, "".join(line + "\n" for line in keep))

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

    def waiting_reason(self) -> str | None:
        """Why queued videos aren't starting, if it's a queue-wide reason."""
        now = time.time()
        if self.hold and now < self.hold["until"]:
            return f"{self.hold['reason']} (trying again in {_fmt_secs(self.hold['until'] - now)})"
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

    def _merge(self, job: dict, info: dict) -> int:
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
        job = self.get(job_id)
        info = inspect_url(job["url"], self.settings)
        with self.cond:
            added = self._merge(job, info)
            # Quick health check: a downloaded video whose file has disappeared is flagged.
            for it in job["items"]:
                if it["status"] == "done" and it.get("filepath") and not os.path.exists(it["filepath"]):
                    self._mark_missing(job, it)
            self._touch(job)
            log.info("Refreshed %r: %d new videos", job["title"], added)
            return added

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

        def check_one(args):
            it, was, paths = args
            if job["check"]["stop"] or job["id"] not in self.jobs:
                return
            try:
                if was in ("done", "skipped"):
                    if not paths:
                        outcome, path, problem = "missing", None, None
                    else:
                        path, problem = paths[0], _verify_file(paths[0], it.get("duration"))
                        outcome = "damaged" if problem else "ok"
                elif paths:                                   # not marked downloaded, but a file is there
                    path, problem = paths[0], _verify_file(paths[0], it.get("duration"))
                    outcome = "damaged-copy" if problem else "found"
                else:
                    outcome, path, problem = "nothing", None, None
                self._apply_check(job, it, was, outcome, path, problem)
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

    def _apply_check(self, job: dict, it: dict, was: str, outcome: str, path: str | None, problem: str | None):
        note = None
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
            self._item_log(job, it, "warning" if outcome != "found" else "info", note)

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
            for name in names:
                tags = ID_TAG.findall(name)
                vid = next((t for t in reversed(tags) if t in statuses), None)
                if vid is None:
                    continue
                downloaded = statuses[vid] in ("done", "skipped")
                is_part = re.search(re.escape(f"[{vid}]") + r"\.f[\w-]+\.\w+$", name)
                if LEFTOVER.search(name) or is_part or (SIDECAR.search(name) and not downloaded):
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
            problem = _folder_problem(outdir)
            if problem:
                raise OSError(problem)
            job.update(outdir=outdir, options=options, paused=False)
            wanted = set(ids) if ids is not None else None
            count = 0
            for it in job["items"]:
                if it["status"] not in ("new", "failed"):
                    continue
                if wanted is None and it.get("removed") or wanted is not None and it["id"] not in wanted:
                    continue
                self._forget_file(job, it)
                it.update(status="queued", options=dict(options), outdir=outdir, error=None, attempts=0,
                          net_attempts=0, next_try=0, cancel=False, progress=0.0, rev=job["rev"] + 1)
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
                        self._forget_file(job, it)
                        it.update(status="queued", error=None, attempts=0, net_attempts=0, next_try=0,
                                  rev=job["rev"] + 1)
            elif action == "remove":
                for it in job["items"]:
                    it["cancel"] = True
                del self.jobs[job_id]
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
            if action == "cancel":
                if item["status"] == "queued":
                    item.update(status="new", error=None)
                elif item["status"] in ACTIVE:
                    item["cancel"] = True
            elif action == "retry" and item["status"] in ("failed", "queued"):
                self._forget_file(job, item)
                item.update(status="queued", error=None, attempts=0, net_attempts=0, next_try=0, cancel=False)
            self._touch(job, item)
            self.cond.notify_all()

    def item_log(self, job_id: str, item_id: str) -> dict:
        with self.cond:
            item = self._get_item(self.get(job_id), item_id)
            return {"title": item["title"], "error": item.get("error"), "lines": list(item.get("log") or [])}

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
        }

    def items_since(self, job_id: str, since: int) -> dict:
        with self.cond:
            job = self.get(job_id)
            changed = [{k: it.get(k) for k in ITEM_KEYS} for it in job["items"] if it["rev"] > since]
            return {"rev": job["rev"], "items": changed}

    def total_speed(self) -> float:
        with self.cond:
            return sum(it.get("speed") or 0 for j in self.jobs.values() for it in j["items"] if it["status"] in ACTIVE)

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
        }

    # ---- workers

    def _flight_key(self, job: dict, item: dict) -> tuple[str, str]:
        return os.path.normpath(item.get("outdir") or job["outdir"]), self._archive_key(item)

    def _next_item(self):
        now = time.time()
        if (self.hold and now < self.hold["until"]) or now < self.next_start:
            return None
        level = self.net_level() if self._wait_for_network() else "unknown"
        if level == "offline":
            return None
        for job in sorted(self.jobs.values(), key=lambda j: j["created"]):
            if job["paused"]:
                continue
            for it in job["items"]:
                if it["status"] != "queued" or it["next_try"] > now:
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
                item.update(status="downloading", speed=None, eta=None)
                self.active += 1
                self._touch(job, item)
            try:
                self._download(job, item)
            except Exception as exc:  # never let a worker die
                log.exception("Unexpected error downloading %s", item["id"])
                self._set(job, item, status="failed", error=_clean_error(exc))
            finally:
                with self.cond:
                    self.active -= 1
                    self.inflight.discard(key)
                    item.update(speed=None, eta=None, cancel=False)
                    self._touch(job, item)
                    self.cond.notify_all()

    def _download(self, job: dict, item: dict):
        options = item.get("options") or job["options"]
        outdir = item.get("outdir") or job["outdir"]
        archive_key = self._archive_key(item)
        skip_existing = options.get("skip_existing", True)

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
            return item["cancel"] or job["paused"] or job["id"] not in self.jobs

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
                    status="downloading",  # a before_dl postprocessor may have flagged "processing"
                    downloaded=done, total=total, speed=d.get("speed"), eta=d.get("eta"),
                    progress=round(100 * done / total, 1) if total else item["progress"],
                )
                if item["error"] and done:
                    changes["error"] = None               # data is flowing again: clear the retry note
                self._set(job, item, **changes)

        def pp_hook(d):
            if d["status"] == "started" and item["status"] != "processing" and "Thumbnail" not in d.get("postprocessor", ""):
                self._set(job, item, status="processing", progress=100.0, speed=None, eta=None)

        fmt_opts, pps = build_format_opts(options)
        prefix = f"{item['number']} - " if item["number"] and options.get("numbering", True) else ""
        tmpl = os.path.join(_escape_tmpl(outdir), _escape_tmpl(prefix) + "%(title).150B [%(id)s].%(ext)s")

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
        if self.settings.get("rate_limit_mb"):
            opts["ratelimit"] = float(self.settings["rate_limit_mb"]) * 1024 * 1024
        if str(options.get("quality", "best")) not in AUDIO_FORMATS:
            # Keep the separate video/audio streams until the merged file is verified, so a bad merge can be
            # redone locally instead of downloading everything again. They are deleted below once it's good.
            opts["keepvideo"] = True

        try:
            for attempt in (1, 2):
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(item["url"], download=True) or {}
                filepath = ((info.get("requested_downloads") or [{}])[0].get("filepath")) or info.get("filepath")
                parts = _part_files(outdir, item["id"])
                bad = _check_part_sizes(info, parts)
                if bad:                                   # a stream itself is broken: get it again
                    _remove([filepath, *parts.values()] if filepath else parts.values())
                    raise _IncompleteFile(bad)
                live = info.get("is_live") or info.get("was_live")
                problem = _verify_file(filepath, None if live else (info.get("duration") or item.get("duration")))
                if not problem:
                    break
                if filepath and os.path.isfile(filepath):
                    os.remove(filepath)                   # so the next attempt makes it again
                if attempt == 1 and parts:
                    self._item_log(job, item, "warning", f"{problem} — merging again from the downloaded streams")
                    continue                              # yt-dlp reuses the streams on disk: no re-download
                _remove(parts.values())
                raise _IncompleteFile(problem)
            _remove(parts.values())                       # merged file verified: the streams aren't needed
            _remove(p for p in _files_for(outdir, item["id"]) if p.name.endswith(".damaged"))
            # The archive is written only now, after the file is verified, so a broken file is never "done".
            if skip_existing:
                self._archive_add(outdir, archive_key)
            self._set(job, item, status="done", progress=100.0, filepath=filepath, error=None,
                      attempts=0, net_attempts=0, health="ok", checked=time.time(), file_problem=None)
            self._item_log(job, item, "info", f"Done and verified: {os.path.basename(filepath)}")
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
            self._touch(job, item)
        self._item_log(job, item, "error", f"{kind} error → {item['status']}: {msg}")
        if kind == "network":
            self.on_network_error()


def _open_folder(path: str):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])
