"""The event log: one row per finished or failed download, history and stats."""

from __future__ import annotations

import os
import sqlite3
import time

from ..config import log


class EventsMixin:
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
