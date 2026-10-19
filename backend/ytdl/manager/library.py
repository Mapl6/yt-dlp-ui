"""The library: adding links (one or many), refreshing, removing, size estimates and .m3u export."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from pathlib import Path

from .. import extract
from ..config import ARCHIVE_NAME, LOCK_DIR_NAME, MIN_FREE_BYTES, log
from ..errors import _clean_error
from ..extract import UNAVAILABLE_TITLE
from ..files import _disk_free, _remove
from ..formats import VIDEO_HEIGHTS, _estimate_bytes
from ..util import _atomic_write, _parse_section, _safe_name


class LibraryMixin:
    # ---- library

    def open_url(self, url: str) -> dict:
        """Fetch a link and save it in the library (or update the saved copy). Returns the summary."""
        info = extract.inspect_url(url, self.settings)          # slow network call: outside the lock
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
        info = extract.inspect_url(job["url"], self.settings)
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
