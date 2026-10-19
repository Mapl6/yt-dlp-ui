"""Loading and saving the library, the queue-wide pause and the settings file."""

from __future__ import annotations

import json
import sqlite3
import time

from .. import config
from ..config import ACTIVE, BACKUP_EVERY, _queue_state_file, log
from ..formats import VIDEO_HEIGHTS
from ..store import _read_legacy_state
from ..util import _atomic_write


class PersistenceMixin:
    # ---- persistence

    def _load_queue_state(self):
        """A rate-limit pause survives a restart: otherwise every queued video would hit YouTube again at once."""
        try:
            state = json.loads(_queue_state_file().read_text())
        except (OSError, ValueError):
            return
        hold = state.get("hold")
        if isinstance(hold, dict) and float(hold.get("until") or 0) > time.time():
            self.hold = {"reason": str(hold.get("reason") or "Paused"), "until": float(hold["until"]),
                         "kind": hold.get("kind") or "other", "since": float(hold.get("since") or time.time())}
        self.rate_strikes = int(state.get("rate_strikes") or 0)
        self.rate_wave = float(state.get("rate_wave") or 0)
        self.rate_probe = bool(state.get("rate_probe"))
        self.last_success = float(state.get("last_success") or 0)
        if self.hold or self.rate_probe:
            log.info("Restored queue pause: %s (one video first: %s)", self.hold, self.rate_probe)

    def _save_queue_state(self):
        state = {"hold": self.hold, "rate_strikes": self.rate_strikes, "rate_wave": self.rate_wave,
                 "rate_probe": self.rate_probe, "last_success": self.last_success}
        try:
            _atomic_write(_queue_state_file(), json.dumps(state))
        except OSError as exc:
            log.error("Could not save the queue pause: %s", exc)

    def _load_state(self):
        jobs = self.store.load()
        migrated = False
        if not jobs and (config.LEGACY_STATE.exists() or config.LEGACY_BACKUP.exists()):
            jobs, migrated = _read_legacy_state(), True
        for job in jobs:
            self._normalize_job(job)
            for it in job["items"]:
                if it["status"] in ACTIVE:
                    it["status"] = "queued"              # interrupted by a restart: resume it
            self.jobs[job["id"]] = job
            self._reindex(job)
            if not migrated:
                self._saved_rev[job["id"]] = job["rev"]
        if migrated:
            self.save_state()                            # write everything into the database first …
            for path in (config.LEGACY_STATE, config.LEGACY_BACKUP):   # … then retire the JSON files (kept, not deleted)
                if path.exists():
                    path.replace(path.with_name(path.name + ".migrated"))
            log.info("Moved %d playlists from state.json into library.db", len(jobs))

    @staticmethod
    def _normalize_job(job: dict):
        """Fill in fields that older libraries (or export files) lack and reset runtime-only state."""
        job.setdefault("heights", VIDEO_HEIGHTS)
        job.setdefault("width", 2)
        job.setdefault("source_id", None)
        job.setdefault("updated", job.get("created"))
        job.pop("cancelled", None)
        job.pop("saved", None)
        if (job.get("check") or {}).get("running"):
            job["check"].update(running=False, stop=False)   # a check interrupted by a restart
        for it in job["items"]:
            PersistenceMixin._normalize_item(job, it)

    @staticmethod
    def _normalize_item(job: dict, it: dict):
        if it["status"] == "cancelled":
            it["status"] = "new"
        it.setdefault("options", job.get("options", {}))
        it.setdefault("outdir", job["outdir"])
        it.setdefault("net_attempts", 0)
        it.setdefault("rate_attempts", 0)
        it.setdefault("removed", False)
        it.setdefault("log", [])
        it.setdefault("priority", 0)
        it.setdefault("held", False)
        it.setdefault("history", [])
        it.update(speed=None, eta=None, next_try=0, cancel=False, step=None)

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
