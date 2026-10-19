""""Check files" and "Clean up leftovers"."""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import verify
from ..config import ACTIVE, CHECK_WORKERS, log
from ..files import FINAL_FILE, ID_TAG, IMAGE_EXT, LEFTOVER, SIDECAR, _move_aside, _remove
from ..util import _expected_duration
from ..verify import DAMAGED, _decodes_fully, _has_cover


class HealthMixin:
    # ---- file health ("Check files") and clean-up

    def _mark_missing(self, job: dict, it: dict):
        it.update(status="failed", health="missing", file_problem="missing", checked=time.time(), rev=job["rev"] + 1,
                  error="File check: the file is not in the folder (moved or deleted?) — press Retry to download it again")

    def start_check(self, job_id: str) -> bool:
        """Verify every downloaded video of a playlist, and pick up good files already in the folder."""
        with self.cond:
            job = self.get(job_id)
            if (job.get("check") or {}).get("running"):
                return False
            ids = [it["id"] for it in job["items"] if it["status"] in ("done", "skipped", "new", "failed")]
            job["check"] = {"running": True, "stop": False, "done": 0, "total": len(ids), "ok": 0, "damaged": 0,
                            "missing": 0, "found": 0, "started": time.time(), "finished": None}
            self._touch(job)
        log.info("Checking %d files of %r", len(ids), job["title"])
        threading.Thread(target=self._run_check, args=(job, ids), name="file-check", daemon=True).start()
        return True

    def stop_check(self, job_id: str):
        with self.cond:
            job = self.get(job_id)
            if (job.get("check") or {}).get("running"):
                job["check"]["stop"] = True
                self._touch(job)

    def _run_check(self, job: dict, ids: list[str]):
        by_id = {it["id"]: it for it in job["items"]}
        folders: dict[str, dict[str, list[str]]] = {}

        def candidates(it: dict) -> list[str]:
            outdir = it.get("outdir") or job["outdir"]
            if outdir not in folders:
                index: dict[str, list[str]] = {}
                try:
                    names = os.listdir(outdir)
                except OSError:
                    names = []
                for name in names:
                    m = FINAL_FILE.search(name)
                    if m:
                        index.setdefault(m.group(1), []).append(os.path.join(outdir, name))
                folders[outdir] = index
            return folders[outdir].get(it["id"], [])

        work = []
        for vid in ids:
            it = by_id.get(vid)
            if it:
                known = it.get("filepath")
                paths = ([known] if known and os.path.isfile(known) else []) + \
                        [p for p in candidates(it) if p != known]
                work.append((it, it["status"], paths))

        def check_file(path: str, duration: float | None) -> tuple[str | None, bool]:
            """-> (problem, rescued). A file ffprobe can't open is only damaged if it also fails a full decode."""
            problem = verify._verify_file(path, duration)
            if problem == DAMAGED and _decodes_fully(path, duration):
                return None, True
            return problem, False

        def check_one(args):
            it, was, paths = args
            if job["check"]["stop"] or job["id"] not in self.jobs:
                return
            try:
                if was in ("done", "skipped"):
                    if not paths:
                        outcome, path, problem, rescued = "missing", None, None, False
                    else:
                        path, (problem, rescued) = paths[0], check_file(paths[0], _expected_duration(it))
                        outcome = "damaged" if problem else "ok"
                elif paths:                                   # not marked downloaded, but a file is there
                    path, (problem, rescued) = paths[0], check_file(paths[0], it.get("duration"))
                    outcome = "damaged-copy" if problem else "found"
                else:
                    outcome, path, problem, rescued = "nothing", None, None, False
                self._apply_check(job, it, was, outcome, path, problem, rescued)
            except Exception:
                log.exception("File check failed for %s", it["id"])

        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            list(pool.map(check_one, work))
        with self.cond:
            chk = job["check"]
            chk.update(running=False, finished=time.time())
            self._touch(job)
        log.info("File check of %r: %s", job["title"],
                 {k: chk[k] for k in ("done", "ok", "damaged", "missing", "found")} | {"stopped": chk["stop"]})

    def _apply_check(self, job: dict, it: dict, was: str, outcome: str, path: str | None, problem: str | None,
                     rescued: bool = False):
        note = "File check: ffprobe couldn't read the file, but it plays to the end — kept" if rescued else None
        with self.cond:
            chk = job["check"]
            chk["done"] += 1
            if it["status"] != was:                           # queued or downloading meanwhile: leave it alone
                self._touch(job)
                return
            now = time.time()
            if outcome == "ok":
                it.update(filepath=path, health="ok", checked=now, file_problem=None)
                chk["ok"] += 1
            elif outcome == "missing":
                self._mark_missing(job, it)
                chk["missing"] += 1
                note = "File check: file missing from the folder"
            elif outcome == "damaged":
                aside = _move_aside(path)
                it.update(status="failed", health="damaged", file_problem="damaged", filepath=None, checked=now,
                          error=f"File check: {problem}. It was renamed to “{os.path.basename(aside)}” — "
                                "press Retry to download it again")
                chk["damaged"] += 1
                note = f"File check: {problem} → renamed to {os.path.basename(aside)}"
            elif outcome == "found":
                outdir, key = it.get("outdir") or job["outdir"], self._archive_key(it)
                if key not in self._archive_ids(outdir):
                    self._archive_add(outdir, key)
                it.update(status="skipped", progress=100.0, filepath=path, health="ok", checked=now, error=None,
                          file_problem=None)
                chk["found"] += 1
                note = f"File check: found a good copy in the folder ({os.path.basename(path)})"
            elif outcome == "damaged-copy":
                aside = _move_aside(path)
                it.update(error=f"File check: found a damaged copy in the folder ({problem}); "
                                f"it was renamed to “{os.path.basename(aside)}”")
                chk["damaged"] += 1
                note = f"File check: damaged copy {problem} → renamed to {os.path.basename(aside)}"
            self._touch(job, it)
        if note:
            self._item_log(job, it, "info" if outcome in ("found", "ok") else "warning", note)

    def cleanup(self, job_id: str, dry_run: bool = True) -> dict:
        """Find (and delete) leftovers of this playlist: partial downloads, unmerged streams, stray
        thumbnails/subtitles and renamed damaged files. Videos queued or downloading are never touched."""
        with self.cond:
            job = self.get(job_id)
            entries: dict[str, dict[str, str]] = {}        # folder -> {video id: status}
            for it in job["items"]:
                outdir = it.get("outdir") or job["outdir"]
                busy = it["status"] in ("queued", *ACTIVE) or self._flight_key(job, it) in self.inflight
                if not busy:
                    entries.setdefault(outdir, {})[it["id"]] = it["status"]
        files = []
        for outdir, statuses in entries.items():
            try:
                names = os.listdir(outdir)
            except OSError:
                continue
            media: dict[str, str] = {}                 # video id -> its finished file
            owned = []
            for name in names:
                tags = ID_TAG.findall(name)
                vid = next((t for t in reversed(tags) if t in statuses), None)
                if vid is None:
                    continue
                is_part = re.search(re.escape(f"[{vid}]") + r"\.f[\w-]+\.\w+$", name)
                owned.append((name, vid, is_part))
                if not (is_part or LEFTOVER.search(name) or SIDECAR.search(name)):
                    media[vid] = os.path.join(outdir, name)
            for name, vid, is_part in owned:
                downloaded = statuses[vid] in ("done", "skipped")
                # A downloaded video's thumbnail is a leftover only once the picture is inside the video file.
                stray = SIDECAR.search(name) and (not downloaded or IMAGE_EXT.search(name) and vid in media
                                                  and _has_cover(media[vid]))
                if LEFTOVER.search(name) or is_part or stray:
                    path = os.path.join(outdir, name)
                    if os.path.isfile(path):
                        files.append(path)
        size = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        if not dry_run and files:
            _remove(files)
            log.info("Cleaned up %d leftover files (%d bytes) of %r", len(files), size, job["title"])
        return {"files": len(files), "bytes": size, "names": [os.path.basename(p) for p in files[:50]]}
