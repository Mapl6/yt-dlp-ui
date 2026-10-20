"""The queue: queue-wide pauses, network and schedule gating, queue controls and the workers that pick the next video."""

from __future__ import annotations

import os
import time

from ..config import ACTIVE, log
from ..errors import _clean_error
from ..files import _folder_problem, _open_folder
from ..settings import _minutes
from ..util import _fmt_secs, _parse_section
from .constants import LIVE_LEVELS


class QueueMixin:
    def _hold_queue(self, reason: str, seconds: float, kind: str = "other"):
        """Pause the whole queue (kind: ratelimit, cookies, disk, other). A longer pause replaces a shorter one."""
        with self.cond:
            now = time.time()
            until = now + seconds
            if not self.hold or self.hold["until"] < until:
                old = self._active_hold()
                since = old["since"] if old and old.get("kind") == kind else now
                self.hold = {"reason": reason, "until": until, "kind": kind, "since": since}
                self.version += 1
                self._save_queue_state()
                log.warning("Queue paused for %ds: %s", seconds, reason)

    def _active_hold(self) -> dict | None:
        return self.hold if self.hold and time.time() < self.hold["until"] else None

    def _clear_hold(self, probe: bool, kinds: tuple[str, ...] | None = None):
        """End the queue-wide pause (only one of these kinds, if given). probe=False also ends "one video first"
        and forgets the rate-limit waves: used when the cookies or the proxy change, which is what fixes a block."""
        with self.cond:
            if kinds is None or (self.hold and self.hold.get("kind") in kinds):
                self.hold = None
            self.next_start = 0.0
            if not probe:
                self.rate_probe = False
                self.rate_strikes = 0
                self.rate_wave = 0.0
            self._save_queue_state()
            self.version += 1
            self.cond.notify_all()

    def _wait_for_network(self) -> bool:
        return bool(self.settings.get("wait_for_network", True))

    def _limit(self) -> int:
        """Parallel downloads allowed right now: fewer on a weak connection so each one can finish."""
        c = self.settings["concurrency"]
        if self.rate_probe:
            return 1                                    # after rate limiting: see if one download works first
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
        # "checking" counts as down too: a failed connection check reports it, and at startup nothing waits anyway
        if new in LIVE_LEVELS and old not in LIVE_LEVELS:
            with self.cond:
                for job in self.jobs.values():
                    waiting = [it for it in job["items"] if it["status"] == "queued" and it.get("net_attempts")
                               and it["next_try"] > time.time()]
                    for it in waiting:
                        it.update(next_try=0, rev=job["rev"] + 1)
                    if waiting:
                        self._touch(job)
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


    def _apply_options(self, job: dict, options: dict) -> str:
        """Make these the playlist's options (used by Download and by auto-download of new videos). Takes
        output_dir out of options; returns the folder."""
        outdir = os.path.expanduser((options.pop("output_dir", "") or "").strip()) or job["outdir"]
        problem = _folder_problem(outdir)
        if problem:
            raise OSError(problem)
        job.update(outdir=outdir, options=dict(options))
        return outdir

    def set_options(self, job_id: str, options: dict) -> dict:
        """The playlist's Save button: keep the options without queueing anything."""
        with self.cond:
            job = self.get(job_id)
            options = dict(options)
            options.pop("section", None)                 # a part is only ever for one download
            self._apply_options(job, options)
            self._touch(job)
            log.info("Saved the options of %r: %s", job["title"], job["options"])
            return {"options": dict(job["options"]), "outdir": job["outdir"]}

    def queue(self, job_id: str, ids: list[str] | None, options: dict) -> int:
        """Queue the chosen videos (all downloadable, not-downloaded ones when ids is None)."""
        with self.cond:
            job = self.get(job_id)
            section = _parse_section(options.pop("section", None))
            outdir = self._apply_options(job, options)   # a part is only for this download: not saved
            job["paused"] = False
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
                self._clear_hold(probe=True)
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
        self._clear_hold(probe=True)                     # still one video first after rate limiting
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
