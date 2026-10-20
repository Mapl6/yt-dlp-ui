"""The download engine: a persistent library of playlists ("jobs") and their videos, a queue and a worker pool
built on yt-dlp.

Manager is put together from mixins, one per area (see each module). They share `self`: the library (jobs), the
lock (cond) and the scheduler state all live on the one Manager object."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import deque

from .. import config
from ..config import MAX_WORKERS, log, setup_logging
from ..errors import AlreadyRunning
from ..files import _FileLock
from ..settings import _coerce_settings
from ..store import Store
from .actions import ActionsMixin
from .constants import (
    EXPORT_APP,
    EXPORT_FORMAT,
    HISTORY_LEN,
    ITEM_KEYS,
    LIVE,
    LIVE_LEVELS,
    PP_QUIET,
    PP_STEPS,
    SPEED_SAMPLE,
    STATUSES,
    TRANSIENT,
)
from .core import CoreMixin
from .events import EventsMixin
from .health import HealthMixin
from .importexport import ImportExportMixin
from .library import LibraryMixin
from .options import OptionsMixin
from .persistence import PersistenceMixin
from .pipeline import PipelineMixin
from .queue import QueueMixin
from .sync import SyncMixin
from .updates import UpdatesMixin
from .views import ViewsMixin

__all__ = ["Manager", "EXPORT_APP", "EXPORT_FORMAT", "HISTORY_LEN", "ITEM_KEYS", "LIVE", "LIVE_LEVELS", "PP_QUIET",
           "PP_STEPS", "SPEED_SAMPLE", "STATUSES", "TRANSIENT"]


class Manager(PersistenceMixin, OptionsMixin, CoreMixin, QueueMixin, LibraryMixin, SyncMixin, UpdatesMixin,
              HealthMixin, ActionsMixin, ImportExportMixin, EventsMixin, ViewsMixin, PipelineMixin):
    def __init__(self):
        config.DATA_DIR.mkdir(exist_ok=True)
        # One app per library: a second copy would fight over the same database and files.
        self._instance_lock = _FileLock(config.DATA_DIR / ".lock")
        if not self._instance_lock.acquire():
            raise AlreadyRunning(f"YT Downloader is already running with this library ({config.DATA_DIR}).")
        setup_logging()
        self.cond = threading.Condition(threading.RLock())
        self.jobs: dict[str, dict] = {}
        try:
            self.settings = _coerce_settings(json.loads(config.SETTINGS_FILE.read_text()))
        except (OSError, ValueError):
            self.settings = _coerce_settings({})
        self.active = 0
        self.inflight: set[tuple[str, str]] = set()   # (folder, archive key) being downloaded right now
        self.hold: dict | None = None                 # queue-wide pause: {"reason", "until", "kind", "since"}
        self.rate_strikes = 0                         # waves of rate limiting in a row (each doubles the pause)
        self.rate_wave = 0.0                          # when the last wave was counted
        self.rate_probe = False                       # after rate limiting: one download at a time until one works
        self.last_success = 0.0                       # when the last download finished and was verified
        self._load_queue_state()
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
        self._disk_cache: dict[str, tuple] = {}        # folder -> (time, _disk_free result)
        self.updating: dict | None = None              # progress of a yt-dlp update
        self._latest = {"version": None, "checked": 0, "checking": False, "failed": 0}   # newest yt-dlp on PyPI
        shutil.rmtree(config.DATA_DIR / ".cookies-tmp", ignore_errors=True)   # copies left by a crash
        self.store = Store(config.DB_FILE, config.DB_BACKUP)
        self._load_state()
        self.last_event = self.store.last_event_id()
        if self.store.version < 1:
            self._backfill_events()
        if not config.DB_BACKUP.exists():
            self.store.backup()                          # never run without a backup to fall back on
        log.info("Started with %d playlists", len(self.jobs))
        for i in range(MAX_WORKERS):
            threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True).start()
        threading.Thread(target=self._saver, name="state-saver", daemon=True).start()
        threading.Thread(target=self._sampler, name="speed-sampler", daemon=True).start()
        threading.Thread(target=self._syncer, name="auto-sync", daemon=True).start()
