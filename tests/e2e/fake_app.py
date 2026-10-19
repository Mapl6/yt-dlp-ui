"""The real app (server.py) on a scratch library with a fake YouTube, for the Playwright tests. No internet.

    .venv/bin/python tests/e2e/fake_app.py --port 8798 --media-port 8797 --data <empty scratch dir>

Links the tests paste:
  https://e2e.test/list/<name>/<n>   a playlist "List <name>" of n short videos (<name>1 … <name>n)
  https://e2e.test/bot/<n>           a playlist whose videos all hit YouTube's "confirm you're not a bot"
The media server also answers GET /__throttle/on and /__throttle/off (slow every transfer down, to catch a
download while it runs).
"""
import argparse
import os
import sys
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
settings = work / "data" / "settings.json"
if not settings.exists():
    settings.write_text('{"output_dir": "%s"}' % (work / "out"))
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests")]

from ytdl import manager as M  # noqa: E402
from ytdl import netcheck  # noqa: E402
from yt_dlp.utils import DownloadError  # noqa: E402

from helpers import FakeExtractor, MediaServer, ff  # noqa: E402

media = work / "media"
if not (media / "a.m4a").exists():
    ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart", str(media / "full.mp4"))
    ff("-i", str(media / "full.mp4"), "-an", "-c", "copy", str(media / "v.mp4"))
    ff("-i", str(media / "full.mp4"), "-vn", "-c", "copy", str(media / "a.m4a"))

server = MediaServer(media, args.media_port)
original_get = server.srv.RequestHandlerClass.do_GET


def do_GET(handler):
    if handler.path.startswith("/__throttle/"):
        server.throttle = handler.path.endswith("/on")
        handler.send_response(204)
        handler.end_headers()
        return
    original_get(handler)


server.srv.RequestHandlerClass.do_GET = do_GET
fake = FakeExtractor(server, media, duration=6)
fake.install()


def inspect_url(url, settings):
    parts = url.split("/")
    if url.startswith("https://e2e.test/list/") and len(parts) == 6:
        name, count = parts[4], int(parts[5])
    elif url.startswith("https://e2e.test/bot/") and len(parts) == 5:
        name, count = "bot", int(parts[4])
    else:
        raise DownloadError(f"ERROR: Unsupported URL: {url}")
    ids = [f"{name}{i + 1}" for i in range(count)]
    if name == "bot":
        fake.bot_ids.update(ids)
    for vid in ids:
        fake.titles[vid] = f"Video {vid}"
    return {"kind": "playlist", "source_id": f"e2e-{name}", "title": f"List {name}", "thumbnail": None,
            "heights": [180], "items": [{"index": i + 1, "id": v, "title": f"Video {v}", "url": f"test://{v}",
                                         "duration": 6, "extractor": "Youtube" if name == "bot" else "Test"}
                                        for i, v in enumerate(ids)]}


M.inspect_url = inspect_url


class Online:
    """NetMonitor without probes: always a good connection."""

    def __init__(self, get_settings, get_speed, on_change=None):
        self.state = {"level": "good"}
        self._get_speed = get_speed

    def check_now(self):
        pass

    def snapshot(self):
        return {"level": "good", "label": "Good", "advice": "Test connection", "bars": 3, "checked": None,
                "speed": self._get_speed(), "peak_speed": 0, "history": []}


netcheck.NetMonitor = Online           # before ytdl.server imports it
from ytdl import server as app  # noqa: E402

print(f"fake app: http://127.0.0.1:{args.port}  media :{server.port}  data {work}", flush=True)
sys.argv = [str(ROOT / "server.py"), "--port", str(args.port), "--no-browser"]
app.main()
