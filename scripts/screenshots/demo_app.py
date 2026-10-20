"""The real app on a scratch library with a made-up YouTube, for the README screenshots (see shoot.mjs). No internet.

    .venv/bin/python scripts/screenshots/demo_app.py --port 8790 --media-port 8789 --data <empty scratch dir>

The playlists below are invented (no real channels). Their videos download from a local media server:
  GET /__live/on    big clips, served slowly, so downloads stay "in progress" for the screenshots
  GET /__live/off   tiny clips at full speed (to fill the library quickly)
"""
import argparse
import http.server
import os
import random
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--media-port", type=int, required=True)
parser.add_argument("--data", required=True, help="an empty scratch directory (never the real data/)")
args = parser.parse_args()

work = Path(args.data).resolve()
if work == (ROOT / "data").resolve() or (ROOT / "data").resolve() in work.parents:
    sys.exit("Refusing to run on the real library")
for d in ("data", "out", "media"):
    (work / d).mkdir(parents=True, exist_ok=True)
os.environ["YTDL_DATA_DIR"] = str(work / "data")
(work / "data" / "settings.json").write_text('{"output_dir": "%s", "concurrency": 3}' % (work / "out"))
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests")]

from yt_dlp.utils import DownloadError  # noqa: E402

from helpers import FakeExtractor, ff, serve  # noqa: E402
from ytdl import extract, netcheck  # noqa: E402

# ---------------------------------------------------------------- media: tiny clips and big ones

media = work / "media"
if not (media / "big-a.m4a").exists():
    for name, size, secs, extra in (("small", "320x180", 6, []), ("big", "1920x1080", 40, ["-crf", "8"])):
        ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30", "-f", "lavfi", "-i", "sine=frequency=330",
           "-t", str(secs), "-c:v", "libx264", "-preset", "ultrafast", *extra, "-c:a", "aac",
           "-movflags", "+faststart", str(media / f"{name}.mp4"))
        ff("-i", str(media / f"{name}.mp4"), "-an", "-c", "copy", str(media / f"{name}-v.mp4"))
        ff("-i", str(media / f"{name}.mp4"), "-vn", "-c", "copy", str(media / f"{name}-a.m4a"))

state = {"live": False}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/__live/"):
            state["live"] = self.path.endswith("/on")
            self.send_response(204)
            self.end_headers()
            return
        data = (media / self.path.lstrip("/")).read_bytes()
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
            for i in range(0, len(body), 65536):
                self.wfile.write(body[i:i + 65536])
                if self.path.startswith("/big"):
                    time.sleep(random.uniform(0.02, 0.05))     # a few MB/s, with a believable wobble
        except (BrokenPipeError, ConnectionResetError):
            pass


srv = serve(Handler, args.media_port)


class Server:
    port = srv.server_address[1]

    @staticmethod
    def url(name):
        return f"http://127.0.0.1:{Server.port}/{name}"


class DemoExtractor(FakeExtractor):
    """Serves the big clips while live is on (for the videos queued then), the tiny ones otherwise."""

    def info(self, url):
        name = "big" if state["live"] else "small"
        vid = str(url)[len("test://"):]
        size = lambda file: (media / file).stat().st_size   # noqa: E731
        return {"id": vid, "title": self.titles.get(vid, vid), "extractor": "test", "extractor_key": "Test",
                "webpage_url": url, "duration": 40 if name == "big" else 6, "formats": [
                    {"format_id": "137", "url": Server.url(f"{name}-v.mp4"), "ext": "mp4", "vcodec": "avc1.640028",
                     "acodec": "none", "width": 1920, "height": 1080, "filesize": size(f"{name}-v.mp4"),
                     "protocol": "http"},
                    {"format_id": "140", "url": Server.url(f"{name}-a.m4a"), "ext": "m4a", "vcodec": "none",
                     "acodec": "mp4a.40.2", "filesize": size(f"{name}-a.m4a"), "protocol": "http"}]}


fake = DemoExtractor(Server, media, duration=6)
fake.install()

# ---------------------------------------------------------------- the made-up library

LECTURES = ["Vectors and spaces", "Linear combinations and span", "Linear independence", "Matrix multiplication",
            "Systems of equations", "Gaussian elimination", "The inverse of a matrix", "Determinants",
            "Vector spaces and subspaces", "Column space and null space", "Basis and dimension", "Rank",
            "Orthogonality", "Projections", "Least squares", "Gram–Schmidt", "Eigenvalues and eigenvectors",
            "Diagonalization", "Symmetric matrices", "Positive definite matrices", "Singular value decomposition",
            "Linear transformations", "Change of basis", "Review and final exam problems"]
LOFI = ["rainy window", "late train home", "coffee & code", "midnight library", "slow sunday", "city lights",
        "paper planes", "warm static", "notebook margins", "after hours", "soft focus", "blue hour"]
TRIP = ["Landing in Reykjavík", "The Golden Circle", "Black sand at Reynisfjara", "Glacier lagoon at sunrise",
        "Camping under the aurora", "Driving the east fjords", "Lake Mývatn & the lava fields", "Whale watching in Húsavík",
        "The Westfjords by ferry", "Snæfellsnes peninsula", "Hidden hot springs", "Last day: what it all cost"]
COOKING = ["Knife skills", "Perfect rice every time", "Stocks and broths", "Pan sauces", "Roasting vegetables",
           "Fresh pasta", "Bread from scratch", "Eggs five ways", "Braising", "Weeknight curry", "Soups for winter",
           "Pizza at home", "Simple desserts", "Meal prep for the week"]

PLAYLISTS = {
    "linear-algebra": ("Linear Algebra — Full Course", "#4f46e5", "#06b6d4", "∑",
                       [f"Lecture {i + 1:02d} · {t}" for i, t in enumerate(LECTURES)], (900, 3300)),
    "lofi": ("Lo-fi Beats for Deep Work", "#db2777", "#f59e0b", "♪",
             [f"{t} — lo-fi mix" for t in LOFI * 4][:40], (1500, 3600)),
    "iceland": ("Iceland Road Trip · 4K Travel Diary", "#0f766e", "#84cc16", "▲",
                [f"Day {i + 1} · {t}" for i, t in enumerate(TRIP)], (480, 1500)),
    "cooking": ("Home Cooking Basics", "#ea580c", "#facc15", "◐", COOKING, (360, 1200)),
}


def cover(c1: str, c2: str, glyph: str) -> str:
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 320 180'><defs><linearGradient id='g' x1='0' y1='0'"
           f" x2='1' y2='1'><stop offset='0' stop-color='{c1}'/><stop offset='1' stop-color='{c2}'/></linearGradient>"
           f"</defs><rect width='320' height='180' fill='url(#g)'/><text x='160' y='118' font-size='86' "
           f"text-anchor='middle' fill='white' fill-opacity='.9' font-family='Helvetica'>{glyph}</text></svg>")
    return "data:image/svg+xml," + urllib.parse.quote(svg)


def inspect_url(url, settings):
    key = url.rsplit("list=", 1)[-1].removeprefix("PLdemo-")
    if key not in PLAYLISTS:
        raise DownloadError(f"ERROR: Unsupported URL: {url}")
    title, c1, c2, glyph, titles, (lo, hi) = PLAYLISTS[key]
    rnd = random.Random(key)
    ids = [f"{key[:3]}{i + 1:03d}" for i in range(len(titles))]
    for vid, t in zip(ids, titles):
        fake.titles[vid] = t
    return {"kind": "playlist", "source_id": f"demo-{key}", "title": title, "thumbnail": cover(c1, c2, glyph),
            "uploader": "Demo", "heights": [2160, 1440, 1080, 720, 480, 360],
            "items": [{"index": i + 1, "id": v, "title": t, "url": f"test://{v}", "duration": rnd.randint(lo, hi),
                       "extractor": "Test"} for i, (v, t) in enumerate(zip(ids, titles))]}


extract.inspect_url = inspect_url
fake.fail_ids.update({"lin014", "lof007"})           # "Private video": a couple of failures for the Failed tab


class Online:
    """NetMonitor without probes: a steady, excellent connection with a believable response-time history."""

    def __init__(self, get_settings, get_speed, on_change=None):
        self.state = {"level": "excellent"}
        self._get_speed = get_speed
        self.peak = 0.0
        rnd = random.Random(7)
        self.history = [round(rnd.gauss(92, 14)) for _ in range(40)]

    def check_now(self):
        pass

    def snapshot(self):
        speed = self._get_speed()
        self.peak = max(self.peak, speed)
        return {"level": "excellent", "label": "Excellent", "advice": "Fast, stable connection to YouTube.", "bars": 4,
                "checked": time.time() - 3, "speed": speed, "peak_speed": self.peak, "history": self.history,
                "internet": {"ok": True, "latency": 41, "loss": 0, "jitter": 9},
                "youtube": {"ok": True, "latency": self.history[-1], "loss": 0, "jitter": 22}}


netcheck.NetMonitor = Online           # before ytdl.web.engine imports it
from ytdl.web.main import main  # noqa: E402

print(f"demo app: http://127.0.0.1:{args.port}  media :{Server.port}  data {work}", flush=True)
sys.argv = [str(ROOT / "server.py"), "--port", str(args.port), "--no-browser"]
main()
