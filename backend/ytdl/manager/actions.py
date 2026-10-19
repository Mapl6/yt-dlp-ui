"""One video's actions (retry, pause, play, delete its file...) and its log."""

from __future__ import annotations

import os
from pathlib import Path

from .. import files
from ..config import ACTIVE, log
from ..files import ID_TAG, _folder_problem, _remove
from .constants import LIVE


class ActionsMixin:
    def item_action(self, job_id: str, item_id: str, action: str):
        with self.cond:
            job = self.get(job_id)
            item = self._get_item(job, item_id)
            st = item["status"]
            if action == "cancel":
                if st == "queued":
                    item.update(status="new", error=None, held=False)
                elif st in ACTIVE:
                    item["cancel"] = True
            elif action == "retry" and st in ("failed", "queued"):
                self._requeue(job, item)
            elif action == "hold":                      # pause this one video; a running one stops and keeps its .part
                if st not in LIVE:
                    raise ValueError("Only videos in the queue can be paused")
                item["held"] = True
            elif action == "unhold":
                item["held"] = False
            elif action == "now":                       # skip the wait before the next retry
                if st == "queued":
                    item.update(next_try=0, held=False)
            elif action in ("reveal", "play"):
                path = item.get("filepath")
                if not path or not os.path.isfile(path):
                    raise ValueError("The file isn't in the folder any more")
                files._open_path(path, reveal=action == "reveal")
                return
            elif action == "delete-file":
                self._delete_files(job, item)
            elif action == "mark-done":
                if st not in ("new", "failed"):
                    raise ValueError("Only videos that aren't downloaded can be marked as downloaded")
                outdir = item.get("outdir") or job["outdir"]
                problem = _folder_problem(outdir)
                if problem:
                    raise ValueError(problem)
                if self._archive_key(item) not in self._archive_ids(outdir):
                    self._archive_add(outdir, self._archive_key(item))
                item.update(status="skipped", progress=100.0, error=None, file_problem=None, outdir=outdir)
                log.info("Marked %s as downloaded", item["id"])
            else:
                raise ValueError(f"Unknown action {action}")
            self._touch(job, item)
            self.cond.notify_all()

    def _video_files(self, outdir: str, vid: str) -> list[Path]:
        """Every file in outdir whose last "[...]" tag is this video id (final file, parts, sidecars)."""
        try:
            entries = list(Path(outdir).iterdir())
        except OSError:
            return []
        return [p for p in entries if (ID_TAG.findall(p.name) or [None])[-1] == vid and p.is_file()]

    def _delete_files(self, job: dict, item: dict):
        """Delete a downloaded video's files (on the user's request) and mark it not downloaded."""
        if item["status"] in LIVE or self._flight_key(job, item) in self.inflight:
            raise ValueError("Stop the download first")
        outdir = item.get("outdir") or job["outdir"]
        files = self._video_files(outdir, item["id"])
        size = sum(p.stat().st_size for p in files)
        _remove(files)
        self._archive_remove(outdir, self._archive_key(item))
        item.update(status="new", filepath=None, health=None, file_problem=None, progress=0.0, error=None)
        log.info("Deleted %d files (%d bytes) of %s", len(files), size, item["id"])


    def item_log(self, job_id: str, item_id: str) -> dict:
        """Everything the detail views show about one video."""
        with self.cond:
            job = self.get(job_id)
            item = self._get_item(job, item_id)
            options = item.get("options") or job["options"]
            return {"title": item["title"], "error": item.get("error"), "lines": list(item.get("log") or []),
                    "status": item["status"], "options": dict(options), "outdir": item.get("outdir") or job["outdir"],
                    "filepath": item.get("filepath"), "fmt": item.get("fmt"), "step": item.get("step"),
                    "history": list(item.get("history") or []), "attempts": item.get("attempts") or 0,
                    "net_attempts": item.get("net_attempts") or 0, "started": item.get("started"),
                    "finished": item.get("finished")}
