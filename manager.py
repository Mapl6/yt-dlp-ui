"""Download engine: playlist inspection, a persistent job queue, and a worker pool built on yt-dlp."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled

DATA_DIR = Path(os.environ.get("YTDL_DATA_DIR") or Path(__file__).parent / "data")
STATE_FILE = DATA_DIR / "state.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

MAX_WORKERS = 8          # hard upper bound for parallel downloads
MAX_ATTEMPTS = 4         # per item, for transient errors
ACTIVE = {"downloading", "processing"}

DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "Downloads" / "YT Downloader"),
    "concurrency": 3,
    "cookies_browser": "",       # "", chrome, safari, firefox, brave, edge, ...
    "rate_limit_mb": 0,          # per download, 0 = unlimited
    "sleep_between": 0,          # seconds to wait before each download (helps avoid 429s)
    "proxy": "",                 # e.g. socks5://127.0.0.1:1080 or http://127.0.0.1:8080
}

VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
AUDIO_FORMATS = {"mp3", "m4a", "opus"}

# Errors that will never succeed on retry.
PERMANENT_ERRORS = re.compile(
    r"private video|video unavailable|has been removed|members.only|not available in your country|"
    r"copyright|account associated with this video|sign in to confirm your age|premieres in|"
    r"this live event will begin|deleted video|is not a valid url|unsupported url|http error 404",
    re.I,
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean_error(exc: Exception) -> str:
    msg = ANSI.sub("", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)  # drop "[youtube] abc123: " prefix
    return msg[:400]


def _escape_tmpl(text: str) -> str:
    """Make literal text safe inside a yt-dlp output template."""
    return text.replace("%", "%%")


def _safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" .")
    return text[:120] or "Untitled"


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _base_opts(settings: dict) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
        "socket_timeout": 30,
    }
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"].strip()
    if settings.get("cookies_browser"):
        opts["cookiesfrombrowser"] = (settings["cookies_browser"],)
    return opts


# ---------------------------------------------------------------- inspection

def inspect_url(url: str, settings: dict) -> dict:
    """Return metadata for a video or (flattened) playlist without downloading anything."""
    opts = _base_opts(settings) | {"skip_download": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)

        if info.get("_type") in ("playlist", "multi_video"):
            entries = list(info.get("entries") or [])
            # Channel pages return their tabs (Videos, Shorts, Live) as nested playlists: flatten one level.
            if entries and all(e.get("_type") == "playlist" or e.get("ie_key") == "YoutubeTab" for e in entries):
                nested = []
                for tab in entries:
                    sub = ydl.sanitize_info(ydl.extract_info(tab["url"], download=False))
                    nested.extend(sub.get("entries") or [])
                entries = nested

            seen, items = set(), []
            for e in entries:
                if not e or not e.get("id") or e["id"] in seen:
                    continue
                seen.add(e["id"])
                items.append({
                    "index": len(items) + 1,
                    "id": e["id"],
                    "title": e.get("title") or e["id"],
                    "url": e.get("url") if str(e.get("url", "")).startswith("http")
                           else f"https://www.youtube.com/watch?v={e['id']}",
                    "duration": e.get("duration"),
                    "extractor": e.get("ie_key") or info.get("extractor_key") or "youtube",
                })
            return {
                "kind": "playlist",
                "source_id": info.get("id"),
                "title": info.get("title") or "Playlist",
                "uploader": info.get("uploader") or info.get("channel"),
                "thumbnail": _pick_thumb(info) or (_pick_thumb(entries[0]) if entries else None),
                "items": items,
                "heights": VIDEO_HEIGHTS,
            }

        heights = sorted({f["height"] for f in info.get("formats") or [] if f.get("height")}, reverse=True)
        return {
            "kind": "video",
            "source_id": info.get("id"),
            "title": info.get("title") or info.get("id"),
            "uploader": info.get("uploader") or info.get("channel"),
            "thumbnail": info.get("thumbnail") or _pick_thumb(info),
            "items": [{
                "index": 1,
                "id": info["id"],
                "title": info.get("title") or info["id"],
                "url": info.get("webpage_url") or url,
                "duration": info.get("duration"),
                "extractor": info.get("extractor_key") or "youtube",
            }],
            "heights": [h for h in VIDEO_HEIGHTS if any(x >= h for x in heights)] or heights,
        }


def _pick_thumb(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return best.get("url")
    return info.get("thumbnail")


# ---------------------------------------------------------------- options

def build_format_opts(options: dict) -> tuple[dict, list]:
    """Translate UI options into yt-dlp format options and postprocessors."""
    quality = str(options.get("quality", "best"))
    container = options.get("container", "mp4")
    pps: list[dict] = []
    opts: dict = {}

    if quality in AUDIO_FORMATS:
        opts["format"] = {"m4a": "ba[ext=m4a]/ba/b", "opus": "ba[acodec=opus]/ba/b"}.get(quality, "ba/b")
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": quality,
                    "preferredquality": "0" if quality == "mp3" else None})
        pps[-1] = {k: v for k, v in pps[-1].items() if v is not None}
    else:
        opts["format"] = "bv*+ba/b"
        sort = [] if quality == "best" else [f"res:{int(quality)}"]
        if container == "mp4":
            # Prefer H.264/AAC so files play everywhere (QuickTime, phones, TVs).
            sort += ["vcodec:h264", "acodec:aac"]
        opts["format_sort"] = sort
        opts["merge_output_format"] = container
        if options.get("subtitles"):
            opts |= {"writesubtitles": True, "subtitleslangs": ["en.*", "en"], "subtitlesformat": "srt/best"}
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    if options.get("metadata", True):
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    if options.get("thumbnail", True):
        opts["writethumbnail"] = True
        pps.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return opts, pps


# ---------------------------------------------------------------- library manager
#
# Every fetched link is saved in a library ("jobs") with ALL of its videos.
# Item status:  new (not downloaded) -> queued -> downloading -> processing -> done | failed
# "skipped" means the file was already in the folder's archive (counts as downloaded).

ITEM_KEYS = ("index", "id", "title", "url", "duration", "number", "status", "progress", "speed",
             "eta", "downloaded", "total", "error", "attempts", "filepath")


class Manager:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self.cond = threading.Condition(threading.RLock())
        self.jobs: dict[str, dict] = {}
        self.settings = DEFAULT_SETTINGS | self._read_json(SETTINGS_FILE, {})
        self.active = 0
        self._dirty = False
        self._load_state()
        for i in range(MAX_WORKERS):
            threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True).start()
        threading.Thread(target=self._saver, name="state-saver", daemon=True).start()

    # ---- persistence

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return default

    def _load_state(self):
        for job in self._read_json(STATE_FILE, []):
            job.setdefault("heights", VIDEO_HEIGHTS)
            job.setdefault("width", 2)
            job.setdefault("source_id", None)
            job.setdefault("updated", job.get("created"))
            job.pop("cancelled", None)
            job.pop("saved", None)
            for it in job["items"]:
                if it["status"] in ACTIVE:
                    it["status"] = "queued"              # interrupted by a restart: resume it
                elif it["status"] == "cancelled":
                    it["status"] = "new"
                it.setdefault("options", job.get("options", {}))
                it.setdefault("outdir", job["outdir"])
                it.update(speed=None, eta=None, next_try=0, cancel=False)
            self.jobs[job["id"]] = job

    def save_state(self):
        with self.cond:
            data = json.dumps(list(self.jobs.values()))
            self._dirty = False
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(STATE_FILE)

    def _saver(self):
        while True:
            time.sleep(3)
            if self._dirty:
                try:
                    self.save_state()
                except OSError:
                    pass

    def update_settings(self, patch: dict) -> dict:
        with self.cond:
            for key in DEFAULT_SETTINGS:
                if key in patch:
                    self.settings[key] = patch[key]
            self.settings["concurrency"] = max(1, min(MAX_WORKERS, int(self.settings["concurrency"])))
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
            self.cond.notify_all()
            return dict(self.settings)

    # ---- helpers

    def _touch(self, job: dict, item: dict | None = None):
        with self.cond:
            job["rev"] += 1
            job["updated"] = time.time()
            if item is not None:
                item["rev"] = job["rev"]
            self._dirty = True

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("Playlist not found")
        return job

    @staticmethod
    def _new_item(it: dict, kind: str, width: int, outdir: str, rev: int) -> dict:
        return {
            **it,
            "number": f"{it['index']:0{width}d}" if kind == "playlist" else "",
            "status": "new", "progress": 0.0, "speed": None, "eta": None,
            "downloaded": 0, "total": None, "error": None, "attempts": 0,
            "filepath": None, "next_try": 0, "rev": rev, "cancel": False,
            "options": {}, "outdir": outdir,
        }

    @staticmethod
    def _archive_ids(outdir: str) -> set[str]:
        try:
            return set((Path(outdir) / ".downloaded.txt").read_text(errors="ignore").splitlines())
        except OSError:
            return set()

    def _mark_existing(self, job: dict):
        """Videos already in the folder's download archive count as downloaded."""
        have = self._archive_ids(job["outdir"])
        for it in job["items"]:
            if it["status"] == "new" and f"{it['extractor'].lower()} {it['id']}" in have:
                it.update(status="skipped", progress=100.0, rev=job["rev"] + 1)

    # ---- library

    def open_url(self, url: str) -> dict:
        """Fetch a link and save it in the library (or update the saved copy). Returns the summary."""
        info = inspect_url(url, self.settings)          # slow network call: outside the lock
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

    def _merge(self, job: dict, info: dict) -> int:
        """Add videos that are new in the playlist; keep the status of every known one."""
        by_id = {it["id"]: it for it in job["items"]}
        width = max(job.get("width", 2), len(str(len(info["items"]))))
        rev = job["rev"] + 1
        added = 0
        for fresh in info["items"]:
            old = by_id.get(fresh["id"])
            if old:
                if old["title"] != fresh["title"] and not fresh["title"].startswith("["):
                    old.update(title=fresh["title"], rev=rev)
            else:
                job["items"].append(self._new_item(fresh, job["kind"], width, job["outdir"], rev))
                added += 1
        job["items"].sort(key=lambda it: it["index"])
        job.update(title=info["title"] or job["title"], width=width,
                   thumbnail=info.get("thumbnail") or job.get("thumbnail"),
                   source_id=info.get("source_id") or job.get("source_id"))
        if info.get("heights"):
            job["heights"] = info["heights"]
        self._mark_existing(job)
        return added

    def refresh(self, job_id: str) -> int:
        job = self.get(job_id)
        info = inspect_url(job["url"], self.settings)
        with self.cond:
            added = self._merge(job, info)
            self._touch(job)
            return added

    def queue(self, job_id: str, ids: list[str] | None, options: dict) -> int:
        """Queue the chosen videos (all not-downloaded ones when ids is None)."""
        with self.cond:
            job = self.get(job_id)
            outdir = os.path.expanduser((options.pop("output_dir", "") or "").strip()) or job["outdir"]
            Path(outdir).mkdir(parents=True, exist_ok=True)
            job.update(outdir=outdir, options=options, paused=False)
            wanted = set(ids) if ids is not None else None
            count = 0
            for it in job["items"]:
                if it["status"] in ("new", "failed") and (wanted is None or it["id"] in wanted):
                    it.update(status="queued", options=dict(options), outdir=outdir, error=None,
                              attempts=0, next_try=0, cancel=False, progress=0.0, rev=job["rev"] + 1)
                    count += 1
            self._touch(job)
            self.cond.notify_all()
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
                for it in job["items"]:
                    if it["status"] == "failed":
                        it.update(status="queued", error=None, attempts=0, next_try=0, rev=job["rev"] + 1)
            elif action == "remove":
                for it in job["items"]:
                    it["cancel"] = True
                del self.jobs[job_id]
            elif action == "open":
                Path(job["outdir"]).mkdir(parents=True, exist_ok=True)
                _open_folder(job["outdir"])
                return
            else:
                raise ValueError(f"Unknown action {action}")
            self._touch(job)
            self.cond.notify_all()

    def item_action(self, job_id: str, item_id: str, action: str):
        with self.cond:
            job = self.get(job_id)
            item = next((i for i in job["items"] if i["id"] == item_id), None)
            if not item:
                raise KeyError("Video not found")
            if action == "cancel":
                if item["status"] == "queued":
                    item.update(status="new", error=None)
                elif item["status"] in ACTIVE:
                    item["cancel"] = True
            elif action == "retry" and item["status"] == "failed":
                item.update(status="queued", error=None, attempts=0, next_try=0, cancel=False)
            self._touch(job, item)
            self.cond.notify_all()

    # ---- views

    def summary(self, job: dict) -> dict:
        counts = {s: 0 for s in ("new", "queued", "downloading", "processing", "done", "failed")}
        speed = 0.0
        for it in job["items"]:
            key = "done" if it["status"] == "skipped" else it["status"]
            counts[key] = counts.get(key, 0) + 1
            if it["status"] in ACTIVE:
                speed += it.get("speed") or 0
        total = len(job["items"])
        active = counts["downloading"] + counts["processing"]
        waiting = counts["queued"]

        if active and job["paused"]:
            status = "pausing"
        elif active:
            status = "downloading"
        elif waiting:
            status = "paused" if job["paused"] else "queued"
        elif total and counts["done"] == total:
            status = "complete"
        else:
            status = "saved"

        return {
            "id": job["id"], "title": job["title"], "kind": job["kind"], "url": job["url"],
            "thumbnail": job.get("thumbnail"), "outdir": job["outdir"], "options": job["options"],
            "heights": job["heights"], "created": job["created"], "updated": job.get("updated"),
            "status": status, "rev": job["rev"], "total": total, "counts": counts, "speed": speed,
            "percent": round(100 * counts["done"] / total, 2) if total else 0,
        }

    def items_since(self, job_id: str, since: int) -> dict:
        with self.cond:
            job = self.get(job_id)
            changed = [{k: it.get(k) for k in ITEM_KEYS} for it in job["items"] if it["rev"] > since]
            return {"rev": job["rev"], "items": changed}

    def overview(self) -> dict:
        with self.cond:
            jobs = [self.summary(j) for j in sorted(self.jobs.values(), key=lambda j: -(j.get("updated") or 0))]
        return {
            "jobs": jobs,
            "active": sum(j["counts"]["downloading"] + j["counts"]["processing"] for j in jobs),
            "speed": sum(j["speed"] for j in jobs),
            "settings": self.settings,
        }

    # ---- workers

    def _next_item(self):
        now = time.time()
        for job in sorted(self.jobs.values(), key=lambda j: j["created"]):
            if job["paused"]:
                continue
            for it in job["items"]:
                if it["status"] == "queued" and it["next_try"] <= now:
                    return job, it
        return None

    def _worker(self):
        while True:
            with self.cond:
                while True:
                    pick = self._next_item() if self.active < self.settings["concurrency"] else None
                    if pick:
                        break
                    self.cond.wait(timeout=2)
                job, item = pick
                item.update(status="downloading", error=None, speed=None, eta=None)
                self.active += 1
                self._touch(job, item)
            try:
                self._download(job, item)
            except Exception as exc:  # never let a worker die
                item.update(status="failed", error=_clean_error(exc))
            finally:
                with self.cond:
                    self.active -= 1
                    item.update(speed=None, eta=None, cancel=False)
                    self._touch(job, item)
                    self.cond.notify_all()

    def _download(self, job: dict, item: dict):
        options = item.get("options") or job["options"]
        outdir = item.get("outdir") or job["outdir"]
        Path(outdir).mkdir(parents=True, exist_ok=True)
        archive = Path(outdir) / ".downloaded.txt"
        archive_key = f"{item['extractor'].lower()} {item['id']}"

        if options.get("skip_existing", True) and archive_key in self._archive_ids(outdir):
            item.update(status="skipped", progress=100.0)
            return

        if self.settings.get("sleep_between"):
            time.sleep(float(self.settings["sleep_between"]))

        last = [0.0]

        def stopping() -> bool:
            return item["cancel"] or job["paused"] or job["id"] not in self.jobs

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
                item.update(
                    status="downloading",  # a before_dl postprocessor may have flagged "processing"
                    downloaded=done, total=total, speed=d.get("speed"), eta=d.get("eta"),
                    progress=round(100 * done / total, 1) if total else item["progress"],
                )
                self._touch(job, item)

        def pp_hook(d):
            if d["status"] == "started" and item["status"] != "processing" and "Thumbnail" not in d.get("postprocessor", ""):
                item.update(status="processing", progress=100.0, speed=None, eta=None)
                self._touch(job, item)

        fmt_opts, pps = build_format_opts(options)
        prefix = f"{item['number']} - " if item["number"] and options.get("numbering", True) else ""
        tmpl = os.path.join(_escape_tmpl(outdir), _escape_tmpl(prefix) + "%(title).150B [%(id)s].%(ext)s")

        opts = _base_opts(self.settings) | fmt_opts | {
            "outtmpl": {"default": tmpl},
            "postprocessors": pps,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "extractor_retries": 3,
            "continuedl": True,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "overwrites": False,
        }
        if options.get("skip_existing", True):
            opts["download_archive"] = str(archive)
        if self.settings.get("rate_limit_mb"):
            opts["ratelimit"] = float(self.settings["rate_limit_mb"]) * 1024 * 1024

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item["url"], download=True)
            downloads = (info or {}).get("requested_downloads") or [{}]
            item.update(status="done", progress=100.0, filepath=downloads[0].get("filepath"))
        except DownloadCancelled:
            # paused: back in the queue, the .part file resumes later. Stopped: back to "not downloaded".
            item.update(status="new" if item["cancel"] else "queued")
        except Exception as exc:
            msg = _clean_error(exc)
            if stopping():
                item.update(status="new" if item["cancel"] else "queued")
                return
            item["attempts"] += 1
            if item["attempts"] < MAX_ATTEMPTS and not PERMANENT_ERRORS.search(msg):
                backoff = 15 * item["attempts"] ** 2          # 15s, 60s, 135s
                item.update(status="queued", error=f"Retrying in {backoff}s — {msg}", next_try=time.time() + backoff)
            else:
                item.update(status="failed", error=msg)


def _open_folder(path: str):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])
