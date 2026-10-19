"""Publishing changes (revisions and the scheduler's index), lookups, the folder archive and re-queuing."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..config import ARCHIVE_NAME, ITEM_LOG_LINES, log
from ..extract import UNAVAILABLE_TITLE
from ..util import ANSI, _atomic_write
from .constants import LIVE


class CoreMixin:
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
            "downloaded": 0, "total": None, "error": None, "attempts": 0, "net_attempts": 0, "rate_attempts": 0,
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
        item.update(status="queued", error=None, attempts=0, net_attempts=0, rate_attempts=0, rate_first=None,
                    next_try=0, cancel=False, held=False, **changes)

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
