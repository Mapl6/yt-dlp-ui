"""Subscriptions: checking saved playlists for new videos automatically."""

from __future__ import annotations

import time

from ..config import SYNC_CHOICES, SYNC_TICK, log
from ..errors import _clean_error


class SyncMixin:
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
