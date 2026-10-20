"""Downloading one video: folder, archive and space checks, the per-video lock, yt-dlp, verification, and what to do when it fails."""

from __future__ import annotations

import os
import shutil
import time

from yt_dlp.utils import DownloadCancelled, download_range_func

from .. import verify
from ..config import (
    COOKIE_HOLD,
    MAX_ATTEMPTS,
    MAX_NET_ATTEMPTS,
    MAX_RATE_STRIKES,
    MIN_FREE_BYTES,
    RATE_BLOCK_STRIKES,
    log,
)
from ..cookies import _cookie_error_text
from ..errors import _clean_error, _error_kind, _IncompleteFile
from ..files import IMAGE_EXT, SIDECAR, _FileLock, _files_for, _folder_problem, _lock_path, _part_files, _remove
from ..formats import (
    AUDIO_FORMATS,
    SPONSOR_CATEGORIES,
    _FormatNote,
    _PlaylistTags,
    _SafeSponsorBlock,
    build_format_opts,
)
from ..util import _escape_tmpl, _fmt_secs, _section_label
from ..verify import _check_part_sizes, _has_cover
from ..ytdlp import _base_opts, _ItemLogger, _ytdl
from .constants import HISTORY_LEN, PP_QUIET, PP_STEPS


class PipelineMixin:
    def _download(self, job: dict, item: dict):
        options = item.get("options") or job["options"]
        outdir = item.get("outdir") or job["outdir"]
        archive_key = self._archive_key(item)
        # A part of a video is never "the video": it isn't checked against or written to the archive.
        skip_existing = options.get("skip_existing", True) and not options.get("section")

        problem = _folder_problem(outdir)
        if problem:                                       # e.g. external drive unplugged: wait, don't fail
            self._set(job, item, status="queued", error=problem, next_try=time.time() + 60)
            return
        if skip_existing and archive_key in self._archive_ids(outdir):
            self._set(job, item, status="skipped", progress=100.0, error=None)
            return
        if shutil.disk_usage(outdir).free < MIN_FREE_BYTES:
            self._hold_queue("Disk almost full — free up space to continue", 60, "disk")
            self._set(job, item, status="queued", error="Waiting for free disk space")
            return

        # Another copy of the app (or another library) writing the same .part file would corrupt it.
        lock = _FileLock(_lock_path(outdir, archive_key))
        if not lock.acquire():
            self._set(job, item, status="queued", next_try=time.time() + 30,
                      error="Another copy of the app is downloading this video — waiting")
            return
        try:
            self._download_locked(job, item, options, outdir, archive_key, skip_existing)
        finally:
            lock.release()

    def _download_locked(self, job: dict, item: dict, options: dict, outdir: str, archive_key: str,
                         skip_existing: bool):
        # Re-check under the lock: the other copy may have just finished this video.
        if skip_existing and archive_key in self._archive_ids(outdir):
            self._set(job, item, status="skipped", progress=100.0, error=None)
            return
        self._item_log(job, item, "info", f"Download started ({options.get('quality', 'best')}) → {outdir}")
        last = [0.0]

        def stopping() -> bool:
            return item["cancel"] or item.get("held") or job["paused"] or job["id"] not in self.jobs

        def progress_hook(d):
            if stopping():
                raise DownloadCancelled()
            if d["status"] == "downloading":
                now = time.time()
                if now - last[0] < 0.4:
                    return
                last[0] = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                changes = dict(
                    status="downloading", step=None,  # a before_dl postprocessor may have flagged "processing"
                    downloaded=done, total=total, speed=d.get("speed"), eta=d.get("eta"),
                    progress=round(100 * done / total, 1) if total else item["progress"],
                )
                if item["error"] and done:
                    changes["error"] = None               # data is flowing again: clear the retry note
                self._set(job, item, **changes)

        remerge = [False]

        def pp_hook(d):
            name = d.get("postprocessor") or ""
            if d["status"] != "started" or name.startswith("_") or name in PP_QUIET:
                return
            step = "Re-merging from kept streams" if remerge[0] and name == "Merger" else PP_STEPS.get(name, "Processing")
            if item["status"] != "processing" or item.get("step") != step:
                self._set(job, item, status="processing", progress=100.0, speed=None, eta=None, step=step)

        fmt_opts, pps = build_format_opts(options)
        prefix = f"{item['number']} - " if item["number"] and options.get("numbering", True) else ""
        section = options.get("section")
        # A part gets its own name ("Title (1-00–2-30) [id].mp4"), so it never collides with the full video.
        suffix = f" ({_section_label(section)})" if section else ""
        tmpl = os.path.join(_escape_tmpl(outdir), _escape_tmpl(prefix) + "%(title).150B" + _escape_tmpl(suffix)
                            + " [%(id)s].%(ext)s")

        logger = _ItemLogger(lambda level, msg: self._item_log(job, item, level, msg))
        opts = _base_opts(self.settings, logger) | fmt_opts | {
            "outtmpl": {"default": tmpl},
            "postprocessors": pps,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "noplaylist": True,
            "fragment_retries": 15,
            "file_access_retries": 10,
            "skip_unavailable_fragments": False,   # a missing piece must fail the download, not leave a gap
            "continuedl": True,                    # resume .part files
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "overwrites": False,
        }
        rate = self._rate_for(len(self._ydls) + 1)
        if rate:
            opts["ratelimit"] = rate
        if section:
            opts["download_ranges"] = download_range_func(None, [(section["start"], section["end"] or float("inf"))])
            opts["force_keyframes_at_cuts"] = True
        if str(options.get("quality", "best")) not in AUDIO_FORMATS:
            # Keep the separate video/audio streams until the merged file is verified, so a bad merge can be
            # redone locally instead of downloading everything again. They are deleted below once it's good.
            opts["keepvideo"] = True

        try:
            for attempt in (1, 2):
                with _ytdl(opts) as ydl:
                    if options.get("metadata", True) and job["kind"] == "playlist":
                        ydl.add_post_processor(_PlaylistTags({"album": job["title"], "track": item["number"].lstrip("0")}),
                                               when="pre_process")
                    ydl.add_post_processor(_FormatNote(lambda fmt: self._set(job, item, fmt=fmt)), when="before_dl")
                    if options.get("sponsorblock") in ("mark", "remove"):
                        cats = set(options.get("sponsor_categories") or SPONSOR_CATEGORIES)
                        ydl.add_post_processor(_SafeSponsorBlock(ydl, categories=cats), when="after_filter")
                    with self.cond:
                        self._ydls.add(ydl)
                    self._rebalance()
                    try:
                        info = ydl.extract_info(item["url"], download=True) or {}
                    finally:
                        with self.cond:
                            self._ydls.discard(ydl)
                        self._rebalance()
                done_info = (info.get("requested_downloads") or [{}])[0]
                filepath = done_info.get("filepath") or info.get("filepath")
                # The processed info has the real length: shorter for a part, or after cutting sponsor segments.
                expected = done_info.get("duration") or info.get("duration") or item.get("duration")
                if section:                               # yt-dlp doesn't shorten the duration for a part
                    end = section["end"] if section["end"] is not None else expected
                    expected = max(0.0, min(end, expected or end) - section["start"]) if end else None
                parts = _part_files(outdir, item["id"])
                self._set(job, item, status="processing", step="Checking stream sizes", speed=None, eta=None)
                bad = _check_part_sizes(info, parts)
                if bad:                                   # a stream itself is broken: get it again
                    _remove([filepath, *parts.values()] if filepath else parts.values())
                    raise _IncompleteFile(bad)
                live = info.get("is_live") or info.get("was_live")
                self._set(job, item, step="Verifying")
                problem = verify._verify_file(filepath, None if live else expected)
                if not problem:
                    break
                if filepath and os.path.isfile(filepath):
                    os.remove(filepath)                   # so the next attempt makes it again
                if attempt == 1 and parts:
                    self._item_log(job, item, "warning", f"{problem} — merging again from the downloaded streams")
                    remerge[0] = True
                    self._set(job, item, step="Re-merging from kept streams")
                    continue                              # yt-dlp reuses the streams on disk: no re-download
                _remove(parts.values())
                raise _IncompleteFile(problem)
            _remove(parts.values())                       # merged file verified: the streams aren't needed
            # keepvideo also stops yt-dlp deleting the thumbnail and subtitle files it embedded: remove them
            # here, but keep the thumbnail if the container couldn't take it (e.g. webm).
            sidecars = [p for p in _files_for(outdir, item["id"]) if SIDECAR.search(p.name) and str(p) != filepath]
            embedded_cover = options.get("thumbnail", True) and _has_cover(filepath)
            _remove(p for p in sidecars if (embedded_cover if IMAGE_EXT.search(p.name) else options.get("subtitles")))
            _remove(p for p in _files_for(outdir, item["id"]) if p.name.endswith(".damaged"))
            # The archive is written only now, after the file is verified, so a broken file is never "done".
            if skip_existing:
                self._archive_add(outdir, archive_key)
            self._set(job, item, status="done", progress=100.0, filepath=filepath, error=None, finished=time.time(),
                      attempts=0, net_attempts=0, rate_attempts=0, rate_first=None, health="ok",
                      checked=time.time(), file_problem=None,
                      media_duration=None if live else expected, section=section)
            self._item_log(job, item, "info", f"Done and verified: {os.path.basename(filepath)}")
            self._event(job, item, "done")
            with self.cond:
                self.last_success = time.time()
                if self.rate_strikes or self.rate_probe:     # it works again: back to normal
                    self.rate_strikes, self.rate_probe = 0, False
                    self.version += 1
                    self.cond.notify_all()
                    log.info("A download worked again after rate limiting: back to normal")
                self._save_queue_state()
        except DownloadCancelled:
            # paused: back in the queue, the .part file resumes later. Stopped: back to "not downloaded".
            self._set(job, item, status="new" if item["cancel"] else "queued")
        except Exception as exc:
            if stopping():
                self._set(job, item, status="new" if item["cancel"] else "queued")
            else:
                self._handle_failure(job, item, exc)

    def _handle_failure(self, job: dict, item: dict, exc: Exception):
        msg = _clean_error(exc)
        kind = "network" if isinstance(exc, _IncompleteFile) else _error_kind(exc)
        now = time.time()
        with self.cond:
            if kind == "permanent":
                item.update(status="failed", error=msg)

            elif kind == "disk":
                self._hold_queue("Disk is full — free up space to continue", 120, "disk")
                item.update(status="queued", error="Disk full — waiting for free space")

            elif kind == "cookies":
                # Every download would fail the same way: pause the queue until the cookie settings change.
                browser = self.settings.get("cookies_browser") or ""
                text = _cookie_error_text(browser, exc) if browser else f"{msg} — import the cookies file again"
                self._hold_queue(text, COOKIE_HOLD, "cookies")
                item.update(status="queued", error=text)

            elif kind == "ratelimit":
                # Downloads running together hit the block together: that is one wave, one strike. A download
                # that started before the last strike doesn't count again.
                new_wave = (item.get("started") or now) > self.rate_wave   # ">": on Windows the clock moves in ~16 ms steps
                if new_wave:
                    self.rate_strikes += 1
                    self.rate_wave = now
                self.rate_probe = True                                   # afterwards: one video first
                wait = min(60 * 2 ** (self.rate_strikes - 1), 1800)       # 1, 2, 4 … 30 min
                bot = "not a bot" in msg.lower()
                if self.rate_strikes >= RATE_BLOCK_STRIKES:
                    reason = ("YouTube keeps asking to confirm you're not a bot" if bot else
                              "YouTube keeps limiting requests") + \
                             " — import cookies or use cookies from your browser, or change your VPN or proxy"
                else:
                    reason = "YouTube is limiting requests — all downloads paused"
                if new_wave or not self._active_hold():
                    self._hold_queue(reason, wait, "ratelimit")
                self._save_queue_state()
                # Separate from net_attempts: connection problems don't use up this allowance.
                item["rate_attempts"] = (item.get("rate_attempts") or 0) + 1
                item["rate_first"] = item.get("rate_first") or now
                # A block on every video keeps the queue paused; a video fails only if others download meanwhile.
                alone = self.last_success > item["rate_first"]
                if item["rate_attempts"] >= MAX_RATE_STRIKES and alone:
                    item.update(status="failed",
                                error=f"{msg} — other videos download, this one is still blocked; try cookies")
                else:
                    item.update(status="queued", error=f"YouTube is limiting requests — waiting: {msg}")

            elif kind == "network":
                item["net_attempts"] += 1
                n = item["net_attempts"]
                if n >= MAX_NET_ATTEMPTS:
                    item.update(status="failed", error=f"Gave up after {n} connection problems — {msg}")
                else:
                    backoff = min(10 * 2 ** (n - 1), 300)               # 10 s, 20 s, 40 s … 5 min
                    item.update(status="queued", next_try=now + backoff,
                                error=f"Connection problem (try {n}) — resuming in {_fmt_secs(backoff)}: {msg}")

            else:
                item["attempts"] += 1
                if item["attempts"] < MAX_ATTEMPTS:
                    backoff = 15 * item["attempts"] ** 2                # 15 s, 60 s, 135 s
                    item.update(status="queued", error=f"Retrying in {backoff}s — {msg}", next_try=now + backoff)
                else:
                    item.update(status="failed", error=msg)
            if item["status"] == "failed":
                item["finished"] = now
            item.setdefault("history", []).append({"t": now, "kind": kind, "msg": msg[:300],
                                                   "result": item["status"]})
            del item["history"][:-HISTORY_LEN]
            self._touch(job, item)
        self._item_log(job, item, "error", f"{kind} error → {item['status']}: {msg}")
        if item["status"] == "failed":
            self._event(job, item, "failed", kind)
        if kind == "network":
            self.on_network_error()
