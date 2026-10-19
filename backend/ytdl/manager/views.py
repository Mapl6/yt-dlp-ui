"""What the page shows: playlist summaries, the overview, the dashboard and the speed history."""

from __future__ import annotations

import time

from ..config import ACTIVE, MIN_FREE_BYTES
from ..cookies import cookie_file_status
from ..files import _disk_free
from ..formats import _estimate_bytes
from ..util import _fmt_secs
from .constants import ITEM_KEYS, SPEED_SAMPLE


class ViewsMixin:
    def _sampler(self):
        while True:
            time.sleep(SPEED_SAMPLE)
            self.speed_hist.append(self.total_speed())


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
            "cookie_file": cookie_file_status(),
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
            # The totals (tiles) cover everything; the filter only narrows the lists.
            totals = {"active": 0, "queued": 0, "ready": 0, "speed": 0.0}
            for job, it in self._queue_order():
                st = it["status"]
                if st in ACTIVE:
                    totals["active"] += 1
                    totals["speed"] += it.get("speed") or 0
                elif st == "queued":
                    totals["queued"] += 1
                    totals["ready"] += not (it.get("next_try", 0) > now or job["paused"] or it.get("held"))
                if job_id and job["id"] != job_id or q and q not in it["title"].lower() and q not in job["title"].lower():
                    continue
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
            # What is left to download (all queued and active videos, not only the filtered ones), per folder.
            need: dict[str, int] = {}
            left, estimated = 0, False
            for job, it in self._live.values():
                size, exact = _estimate_bytes(it, it.get("options") or job["options"])
                outdir = it.get("outdir") or job["outdir"]
                need[outdir] = need.get(outdir, 0) + size
                left += size
                estimated |= not exact
        recent = self.history(limit=30)["rows"]
        day = self.store.counts_since(now - 86400)
        recent_speeds = [v for v in speed_hist[-60:] if v]
        avg = sum(recent_speeds) / len(recent_speeds) if recent_speeds else 0
        return {
            "active": active,
            "queue": queue[:200], "queued": len(queue),
            "recent": recent,
            "jobs": jobs,
            "last_event": self.last_event,
            "stats": {
                "active": totals["active"], "limit": limit, "concurrency": self.settings["concurrency"],
                "queued": totals["queued"], "ready": totals["ready"],
                "speed": totals["speed"],
                "speed_history": speed_hist[-60:],
                "avg_speed": avg or None,
                "left_bytes": left, "left_estimated": estimated,
                "eta": left / avg if avg and left else None,
                "disks": self._disks(need),
                "done_today": day.get("done", 0),
                "failed_today": day.get("failed", 0),
                "waiting": reason,
                "hold": hold,
                "probe": self.rate_probe,
            },
        }

    def _disks(self, need: dict[str, int]) -> list[dict]:
        """Free space on each drive that queued videos go to, with what they still need (cached 10 s)."""
        now, drives = time.time(), {}
        for outdir, size in need.items():
            hit = self._disk_cache.get(outdir)
            if not hit or now - hit[0] > 10:
                hit = self._disk_cache[outdir] = (now, _disk_free(outdir))
            d = hit[1]
            key = d.get("dev", outdir)
            drive = drives.setdefault(key, {"path": d.get("path") or outdir, "free": d.get("free"),
                                            "total": d.get("total"), "need": 0, "problem": d.get("problem")})
            drive["need"] += size
        for d in drives.values():
            d["low"] = d["free"] is not None and d["free"] - d["need"] < MIN_FREE_BYTES
        return list(drives.values())
