"""Paths, limits and the app log. The library lives in <repo>/data unless YTDL_DATA_DIR says otherwise.

Other modules read the paths as config.DATA_DIR etc. (not imported by name), so tests can point them at a
scratch library."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]               # the repo: backend/ytdl/ -> ../..
DATA_DIR = Path(os.environ.get("YTDL_DATA_DIR") or ROOT / "data")   # the library stays in <repo>/data
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
MAX_RATE_STRIKES = 8     # per item: a video fails only if other downloads work while it alone is blocked
RATE_BLOCK_STRIKES = 3   # waves of rate limiting before the banner says to change cookies or VPN
COOKIE_HOLD = 3600       # a cookie error pauses the queue until the settings change (or this runs out)
MIN_FREE_BYTES = 1 << 30 # pause everything below 1 GB free
BACKUP_EVERY = 600       # seconds between copies of the library to library.backup.db
ITEM_LOG_LINES = 40      # warnings/errors kept per video for the "Details" view
ACTIVE = {"downloading", "processing"}


SYNC_TICK = 60           # seconds between checks for playlists due an automatic refresh
SYNC_CHOICES = (0, 6, 12, 24, 168)                # hours between automatic refreshes (0 = off)
YTDLP_CHECK_EVERY = 6 * 3600
YTDLP_RETRY_AFTER = 600  # after a failed lookup of the newest yt-dlp (offline)


CHECK_WORKERS = 3        # files verified in parallel by "Check files"


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


def _queue_state_file() -> Path:
    return DATA_DIR / "queue_state.json"          # the queue-wide pause, kept across restarts
