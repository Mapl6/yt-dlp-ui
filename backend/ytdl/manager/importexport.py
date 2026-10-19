"""Export and import of the whole library (settings, playlists with their videos, history)."""

from __future__ import annotations

import copy
import sqlite3
import threading
import time
import uuid

from ..config import ACTIVE, log
from ..store import Store
from .constants import EXPORT_APP, EXPORT_FORMAT, STATUSES, TRANSIENT


class ImportExportMixin:
    # ---- export and import of the whole library (settings, playlists with their videos, history)

    def export_data(self) -> dict:
        with self.cond:
            jobs = [{**{k: v for k, v in job.items() if k not in ("items", "check")},
                     "summary": self._export_summary(job),
                     "items": [{k: v for k, v in it.items() if k not in TRANSIENT} for it in job["items"]]}
                    for job in self.jobs.values()]
            jobs = copy.deepcopy(jobs)                   # serialised outside the lock
            settings = dict(self.settings)
        return {"app": EXPORT_APP, "format": EXPORT_FORMAT, "exported": time.time(), "settings": settings,
                "jobs": jobs, "events": self.store.all_events()}

    def _export_summary(self, job: dict) -> dict:
        """Counts for people reading the file; ignored on import."""
        c = self.summary(job)["counts"]
        return {"videos": len(job["items"]), "downloaded": c["done"], "queued": c["queued"],
                "downloading": c["downloading"] + c["processing"], "failed": c["failed"], "not_downloaded": c["new"],
                "unavailable": c["removed"]}

    @staticmethod
    def _import_item(raw) -> dict | None:
        """A clean copy of an exported video, or None if it is unusable. It keeps its state: queued videos
        stay queued and a download interrupted by the export resumes."""
        if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k]
                                                for k in ("id", "title", "extractor")):
            return None
        it = copy.deepcopy(raw)
        try:
            it["index"] = int(it.get("index") or 0)
        except (TypeError, ValueError):
            it["index"] = 0
        if it.get("status") not in STATUSES:
            it.update(status="new", progress=0.0, downloaded=0)
        elif it["status"] in ACTIVE:
            it["status"] = "queued"
        it["number"] = str(it.get("number") or "")
        it.setdefault("url", f"https://www.youtube.com/watch?v={it['id']}")
        for key, default in (("progress", 0.0), ("downloaded", 0), ("total", None), ("error", None),
                             ("attempts", 0), ("filepath", None), ("duration", None)):
            it.setdefault(key, default)
        return it

    def import_data(self, data: dict, with_settings: bool = False, check_files: bool = True) -> dict:
        """Merge an "Export all data" file into the library. Never deletes or overwrites what is here:
        unknown playlists are added, known ones (same id, source or link) only gain the videos they lack,
        and history rows already in the log are skipped. Imported videos keep their state (queued ones
        continue downloading), then the folders of the imported playlists are checked one after another,
        so files that are there (or missing) are found."""
        if not isinstance(data, dict) or data.get("app") != EXPORT_APP or not isinstance(data.get("jobs"), list):
            raise ValueError("This is not a YT Downloader export file.")
        if not isinstance(data.get("format"), int) or data["format"] > EXPORT_FORMAT:
            raise ValueError("This export file was made by a newer version of the app. Update the app first.")
        result = {"playlists_added": 0, "playlists_merged": 0, "videos_added": 0, "events_added": 0,
                  "settings": False, "skipped": 0, "checking": 0}
        id_map: dict[str, str] = {}                    # job id in the file -> job id here
        to_check: list[str] = []                       # playlists that gained videos: look at their folders
        with self.cond:
            for raw in data["jobs"]:
                if not (isinstance(raw, dict) and isinstance(raw.get("items"), list)
                        and all(isinstance(raw.get(k), str) and raw[k] for k in ("id", "url", "title", "outdir"))
                        and raw.get("kind") in ("playlist", "video")):
                    result["skipped"] += 1
                    continue
                items = [self._import_item(it) for it in raw["items"]]
                result["skipped"] += sum(it is None for it in items)
                items = [it for it in items if it]
                job = self.jobs.get(raw["id"])
                if job is None or job["url"] != raw["url"] and job.get("source_id") != raw.get("source_id"):
                    job = next((j for j in self.jobs.values()
                                if (raw.get("source_id") and j.get("source_id") == raw["source_id"])
                                or j["url"] == raw["url"]), None)
                if job:
                    id_map[raw["id"]] = job["id"]
                    added = self._import_into(job, items)
                    result["videos_added"] += added
                    result["playlists_merged"] += 1
                    if added:
                        to_check.append(job["id"])
                else:
                    job = {k: copy.deepcopy(v) for k, v in raw.items() if k not in ("items", "check", "summary")}
                    if job["id"] in self.jobs:
                        job["id"] = uuid.uuid4().hex[:12]
                    id_map[raw["id"]] = job["id"]
                    job.setdefault("options", {"quality": "1080", "container": "mp4"})
                    job["created"] = float(job.get("created") or time.time())
                    job["paused"] = bool(job.get("paused"))
                    job["rev"] = 1
                    seen = set()
                    job["items"] = [it for it in items if it["id"] not in seen and not seen.add(it["id"])]
                    for it in job["items"]:
                        it["rev"] = 1
                    job["items"].sort(key=lambda it: it["index"])
                    self._normalize_job(job)
                    self.jobs[job["id"]] = job
                    self._mark_existing(job)
                    self._touch(job)
                    result["playlists_added"] += 1
                    result["videos_added"] += len(job["items"])
                    to_check.append(job["id"])
            self.cond.notify_all()
        events = []
        for r in data.get("events") or []:
            if isinstance(r, dict) and isinstance(r.get("time"), (int, float)) and r.get("status") in ("done", "failed"):
                r = {k: r.get(k) for k in (*Store.EVENT_COLS, "hidden")}
                r["job_id"] = id_map.get(r["job_id"], r["job_id"])
                events.append(r)
        try:
            result["events_added"] = self.store.merge_events(events)
        except sqlite3.Error as exc:
            log.error("Could not import the history: %s", exc)
        if with_settings and isinstance(data.get("settings"), dict):
            self.update_settings(data["settings"])
            result["settings"] = True
        with self.cond:
            self.last_event = self.store.last_event_id()
            self.version += 1
        self.save_state()
        result["checking"] = len(to_check)
        if to_check and check_files:
            threading.Thread(target=self._check_all, args=(to_check,), name="import-check", daemon=True).start()
        log.info("Imported a library export: %s", result)
        return result

    def _check_all(self, job_ids: list[str]):
        """Run "Check files" on several playlists, one at a time (each already verifies files in parallel)."""
        for jid in dict.fromkeys(job_ids):
            try:
                self.start_check(jid)
            except KeyError:
                continue                                 # removed meanwhile
            while (self.jobs.get(jid, {}).get("check") or {}).get("running"):
                time.sleep(0.5)

    def _import_into(self, job: dict, items: list[dict]) -> int:
        """Add the videos a saved playlist doesn't have yet, after its own, with free file numbers."""
        have = {it["id"] for it in job["items"]}
        numbers = {int(it["number"]) for it in job["items"] if str(it.get("number")).isdigit()}
        next_number = 1 + max(numbers, default=0)
        position = max((it["index"] for it in job["items"]), default=0)
        rev = job["rev"] + 1
        added = []
        for it in sorted(items, key=lambda it: it["index"]):
            if it["id"] in have:
                continue                                 # what is here wins
            have.add(it["id"])
            position += 1
            it.update(index=position, rev=rev)
            if job["kind"] == "playlist":
                if not it["number"].isdigit() or int(it["number"]) in numbers:
                    it["number"] = str(next_number)
                numbers.add(int(it["number"]))
                next_number = max(next_number, int(it["number"]) + 1)
            added.append(it)
        if not added:
            return 0
        width = max(job.get("width", 2), len(str(next_number - 1)))
        for it in added:
            if job["kind"] == "playlist":
                it["number"] = f"{int(it['number']):0{width}d}"
            self._normalize_item(job, it)
        job["items"].extend(added)
        job["width"] = width
        self._mark_existing(job)
        self._touch(job)
        return len(added)
