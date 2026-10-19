"""Small helpers shared by the test modules (fixtures are in conftest.py)."""
import http.server
import socketserver
import subprocess
import threading
import time
from pathlib import Path

from ytdl import manager as M

ROOT = Path(__file__).resolve().parent.parent
PATH_GLOBALS = ("DATA_DIR", "DB_FILE", "DB_BACKUP", "LEGACY_STATE", "LEGACY_BACKUP", "SETTINGS_FILE")


def use_data_dir(path) -> dict:
    """Point manager.py's library paths (computed at import) at `path`. Returns the previous values.
    LOG_FILE is left alone: the log handler is set up once per process."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    before = {k: getattr(M, k) for k in PATH_GLOBALS}
    M.DATA_DIR, M.DB_FILE, M.DB_BACKUP = path, path / "library.db", path / "library.backup.db"
    M.LEGACY_STATE, M.LEGACY_BACKUP = path / "state.json", path / "state.backup.json"
    M.SETTINGS_FILE = path / "settings.json"
    return before


def restore_paths(before: dict):
    for k, v in before.items():
        setattr(M, k, v)


class Net:
    """Stands in for NetMonitor: `mgr.net_level = net`, then set `net.level` ("offline" keeps workers idle)."""

    def __init__(self, level="offline"):
        self.level = level

    def __call__(self):
        return self.level


def new_manager(net_level):
    """Manager() starts its workers before anyone can set net_level, and its default ("checking") lets them start
    downloads at once. Hold the workers until `net_level` is in place."""
    gate = threading.Event()
    real = M.Manager._worker

    def gated(self):
        gate.wait()
        return real(self)

    M.Manager._worker = gated
    try:
        mgr = M.Manager()
    finally:
        M.Manager._worker = real
    mgr.net_level = net_level
    gate.set()
    return mgr


def stop_manager(mgr):
    """Managers can't be stopped (their threads run forever): park the workers and free the library lock."""
    mgr.net_level = lambda: "offline"
    mgr._instance_lock.release()


def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def wait_status(mgr, job, want, timeout=90):
    """Wait until every video of `job` has a status in `want`; returns the statuses (with errors on timeout)."""
    end = time.time() + timeout
    while time.time() < end:
        st = [it["status"] for it in mgr.jobs[job["id"]]["items"]]
        if all(s in want for s in st):
            return st
        time.sleep(0.3)
    return [(it["status"], it["error"]) for it in mgr.jobs[job["id"]]["items"]]


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(handler, port=0):
    """Start a local HTTP server (on a free port by default); returns it (port in .server_address[1])."""
    srv = _Server(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def entry(i, vid, title=None, duration=60, extractor="Youtube"):
    return {"index": i, "id": vid, "title": title or f"Video {vid}", "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": duration, "extractor": extractor}


def items_of(job):
    return {it["id"]: it for it in job["items"]}


def make_job(mgr, out, name, ids, created=None):
    """A playlist of `test://<id>` videos (served by the fake extractor), saved to `out`."""
    with mgr.cond:
        job = mgr._create(f"test://list-{name}", {
            "kind": "playlist", "source_id": name, "title": f"List {name}", "thumbnail": None, "heights": [360],
            "items": [{"index": i + 1, "id": v, "title": f"Video {v}", "url": f"test://{v}", "duration": 30,
                       "extractor": "Test"} for i, v in enumerate(ids)]})
        job["outdir"] = str(out)
        for it in job["items"]:
            it["outdir"] = str(out)
        if created:
            job["created"] = created
        mgr._touch(job)
    return job


# ---------- local media servers and a fake YouTube

class MediaServer:
    """Serves the media folder with Range support. `throttle` slows every transfer down (to pause one mid-way);
    `gets` counts requests."""

    def __init__(self, folder: Path, port=0):
        self.folder, self.gets, self.throttle = folder, 0, False
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                owner.gets += 1
                data = (owner.folder / self.path.lstrip("/")).read_bytes()
                start, end = 0, len(data) - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    start, end = int(a), int(b) if b else len(data) - 1
                body = data[start:end + 1]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers()
                try:
                    for i in range(0, len(body), 16384):
                        self.wfile.write(body[i:i + 16384])
                        if owner.throttle:
                            time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.srv = serve(Handler, port)
        self.port = self.srv.server_address[1]

    def url(self, name):
        return f"http://127.0.0.1:{self.port}/{name}"


class Flaky:
    """Serves one file and drops the connection mid-transfer for the first `drops_left` big GETs; supports
    Range, so a download can resume."""

    def __init__(self, data: bytes, drops=3):
        self.data, self.gets, self.drops_left, self.ranges = data, 0, drops, 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _range(self):
                r = self.headers.get("Range")
                if r and r.startswith("bytes="):
                    a, _, b = r[6:].partition("-")
                    return int(a), (int(b) if b else len(owner.data) - 1)
                return None

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(owner.data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                owner.gets += 1
                rng = self._range()
                start, end = rng if rng else (0, len(owner.data) - 1)
                if rng:
                    owner.ranges += 1
                body = owner.data[start:end + 1]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(body)))
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(owner.data)}")
                self.end_headers()
                if owner.drops_left > 0 and len(body) > 50_000:
                    owner.drops_left -= 1
                    self.wfile.write(body[: len(body) // 3])
                    self.wfile.flush()
                    self.connection.shutdown(2)              # the connection dies mid-download
                    return
                self.wfile.write(body)

        self.srv = serve(Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/clip.mp4"


class FakeExtractor:
    """Replaces YoutubeDL.extract_info for test:// URLs. `fail_ids` fail like a private video; `audio_extra` makes
    the reported audio size wrong; `titles` names videos; `extra` is merged into every video's info (thumbnails,
    uploader...)."""

    def __init__(self, server, media: Path, duration=30):
        self.server, self.media, self.duration = server, media, duration
        self.fail_ids: set[str] = set()
        self.bot_ids: set[str] = set()          # these fail with YouTube's "confirm you're not a bot"
        self.audio_extra = 0
        self.titles: dict[str, str] = {}
        self.extra: dict = {}

    def info(self, url):
        vid = str(url)[len("test://"):]
        return {"id": vid, "title": self.titles.get(vid, f"Video {vid}"), "extractor": "test", "extractor_key": "Test",
                "webpage_url": url, "duration": self.duration, "formats": [
                    {"format_id": "137", "url": self.server.url("v.mp4"), "ext": "mp4", "vcodec": "avc1.4d401e",
                     "acodec": "none", "width": 640, "height": 360,
                     "filesize": (self.media / "v.mp4").stat().st_size, "protocol": "http"},
                    {"format_id": "140", "url": self.server.url("a.m4a"), "ext": "m4a", "vcodec": "none",
                     "acodec": "mp4a.40.2", "filesize": (self.media / "a.m4a").stat().st_size + self.audio_extra,
                     "protocol": "http"}]} | self.extra

    def install(self):
        """Patch YoutubeDL.extract_info for test:// URLs; returns the function that undoes it."""
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
        real, fx = YoutubeDL.extract_info, self

        def extract(ydl, url, download=True, *a, **k):
            if str(url).startswith("test://"):
                vid = str(url)[len("test://"):]
                if vid in fx.fail_ids:
                    raise DownloadError("ERROR: [test] %s: Private video. Sign in if you've been granted access" % vid)
                if vid in fx.bot_ids:
                    raise DownloadError("ERROR: [youtube] %s: Sign in to confirm you’re not a bot. "
                                        "Use --cookies-from-browser or --cookies for the authentication." % vid)
                return ydl.process_ie_result(fx.info(url), download=download)
            return real(ydl, url, download, *a, **k)

        YoutubeDL.extract_info = extract
        return lambda: setattr(YoutubeDL, "extract_info", real)
