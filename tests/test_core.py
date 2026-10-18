"""Run: .venv/bin/python tests/test_core.py   (no internet required)

Core tests: playlist merge (reorder / removed videos / numbering), format options, error classification
by exception type, incremental SQLite saves and migration, per-video logs, and API protection.
"""
import io, json, os, re, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ytdl-core-"))
os.environ["YTDL_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT))
import manager as M
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")

def entry(i, vid, title=None):
    return {"index": i, "id": vid, "title": title or f"Video {vid}", "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": 60, "extractor": "Youtube"}

def playlist(*entries):
    return {"kind": "playlist", "source_id": "PL1", "title": "Test list", "thumbnail": None,
            "heights": M.VIDEO_HEIGHTS, "items": list(entries)}

mgr = M.Manager()
mgr.update_settings({"output_dir": str(TMP / "out")})
mgr.net_level = lambda: "offline"        # no real downloads in these tests

# ---------- merge: reorder, new videos, removed and private videos
with mgr.cond:
    job = mgr._create("https://youtube.com/playlist?list=PL1",
                      playlist(*(entry(i + 1, v) for i, v in enumerate("ABCDE"))))
    by = lambda: {it["id"]: it for it in job["items"]}
    by()["A"]["status"] = "done"
    numbers_before = {k: v["number"] for k, v in by().items()}
    # New order: F (new, at the top like a channel), B, A, C, X (private, new). D and E were removed.
    added = mgr._merge(job, playlist(entry(1, "F"), entry(2, "B"), entry(3, "A"), entry(4, "C"),
                                     entry(5, "X", "[Private video]")))
    items = by()
check("merge: two new entries added", added == 2, added)
check("merge: positions follow the playlist", [items[k]["index"] for k in "FBAC"] == [1, 2, 3, 4],
      {k: items[k]["index"] for k in items})
check("merge: known videos keep their file numbers", all(items[k]["number"] == numbers_before[k] for k in "ABCDE"))
check("merge: new videos get the next free numbers", items["F"]["number"] == "06" and items["X"]["number"] == "07",
      (items["F"]["number"], items["X"]["number"]))
check("merge: file numbers never collide", len({it["number"] for it in job["items"]}) == len(job["items"]))
check("merge: videos gone from the playlist are flagged and moved to the end",
      items["D"]["removed"] and items["E"]["removed"] and items["D"]["index"] == 6 and items["E"]["index"] == 7)
check("merge: private video is flagged unavailable", items["X"]["removed"] and not items["F"]["removed"])
check("merge: downloaded video keeps its status", items["A"]["status"] == "done")
s = mgr.summary(job)
check("summary: unavailable not counted as missing", s["counts"]["removed"] == 3 and s["counts"]["new"] == 3, s["counts"])
check("summary: percent uses available videos", abs(s["percent"] - 25.0) < 0.01, s["percent"])

with mgr.cond:
    check("merge: empty fetch changes nothing", mgr._merge(job, playlist()) == 0 and not items["A"]["removed"])
    mgr._merge(job, playlist(entry(1, "F"), entry(2, "B"), entry(3, "A"), entry(4, "C"), entry(5, "D")))
check("merge: a video that comes back is available again", not items["D"]["removed"] and items["D"]["index"] == 5)

# file-number width: refreshing a 5-video list must not widen numbers to 2+ digits more than needed
check("merge: number width unchanged by refresh", job["width"] == 2 and items["F"]["number"] == "06", job["width"])
with mgr.cond:
    big = mgr._create("https://youtube.com/playlist?list=BIG", {**playlist(*(entry(i + 1, f"b{i}") for i in range(120))),
                                                                "source_id": "BIG"})
    mgr._merge(big, {**playlist(*(entry(i + 1, f"b{i}") for i in range(121))), "source_id": "BIG"})
check("merge: 120-video list keeps 3-digit numbers after refresh",
      big["width"] == 3 and big["items"][-1]["number"] == "121", (big["width"], big["items"][-1]["number"]))
# a fetch cut short (40 of 121) must not flag the other 81 videos as unavailable
with mgr.cond:
    mgr._merge(big, {**playlist(*(entry(i + 1, f"b{i}") for i in range(40))), "source_id": "BIG"})
check("merge: partial fetch does not flag the rest as unavailable", not any(it["removed"] for it in big["items"]))
del mgr.jobs[big["id"]]

# queueing to an unplugged external drive must not create folders on the system disk
try:
    mgr.queue(job["id"], None, {"quality": "720", "output_dir": "/Volumes/NoSuchDrive_ytdl/x"})
    check("queue: unplugged drive refused", False)
except OSError as exc:
    check("queue: unplugged drive refused", "not connected" in str(exc) and not Path("/Volumes/NoSuchDrive_ytdl").exists(), exc)

n = mgr.queue(job["id"], None, {"quality": "720"})
check("queue all: skips unavailable videos", n == 4 and items["E"]["status"] == "new" and items["X"]["status"] == "new", n)
mgr.action(job["id"], "stop")

# ---------- format options
o, pps = M.build_format_opts({"quality": "mp3"})
ea = next(p for p in pps if p["key"] == "FFmpegExtractAudio")
check("format: mp3 extracts best-quality audio", ea["preferredcodec"] == "mp3" and ea["preferredquality"] == "0")
o, pps = M.build_format_opts({"quality": "720", "container": "mp4", "subtitles": True, "thumbnail": False})
check("format: 720p mp4 prefers h264 at 720", o["format_sort"] == ["res:720", "vcodec:h264", "acodec:aac"])
check("format: subtitles embedded, no thumbnail", o.get("writesubtitles") and "writethumbnail" not in o
      and any(p["key"] == "FFmpegEmbedSubtitle" for p in pps))

# ---------- error classification by exception type (not message text)
def http(status):
    err = HTTPError(Response(io.BytesIO(b""), "https://x", {}, status=status, reason="x"))
    return DownloadError("ERROR: something", exc_info=(type(err), err, None))
cases = [
    (http(429), "ratelimit"), (http(503), "network"), (http(403), "network"), (http(404), "permanent"),
    (DownloadError("ERROR: odd wording", exc_info=(OSError, OSError(28, "No space"), None)), "disk"),
    (DownloadError("ERROR: odd wording", exc_info=(TransportError, TransportError("weird"), None)), "network"),
    (DownloadError("ERROR: x", exc_info=(GeoRestrictedError, GeoRestrictedError("geo"), None)), "permanent"),
    (ExtractorError("Some new wording yt-dlp invented", expected=True), "permanent"),
    (ExtractorError("Unable to parse player", expected=False), "other"),
]
for exc, want in cases:
    got = M._error_kind(exc)
    check(f"error type -> {want:9} ({type(exc).__name__}: {str(exc)[:30]})", got == want, got)
check("unavailable titles", all(M.UNAVAILABLE_TITLE.match(t) for t in ("[Private video]", "[Deleted video]"))
      and not M.UNAVAILABLE_TITLE.match("[MV] Song"))

# ---------- incremental saves
mgr.save_state()
written = {}
real_write = mgr.store.write
mgr.store.write = lambda j, i, d: (written.update(jobs=len(j), items=len(i)), real_write(j, i, d))[1]
mgr._set(job, items["B"], title="Renamed B")
mgr.save_state()
mgr.store.write = real_write
check("save: only the changed video is written", written == {"jobs": 1, "items": 1}, written)
reloaded = M.Store(M.DB_FILE, M.DB_BACKUP).load()
check("save: change is in the database", any(it["title"] == "Renamed B" for j in reloaded for it in j["items"]))

# ---------- per-video log
mgr._item_log(job, items["C"], "error", "\x1b[0;31mERROR:\x1b[0m test failure")
lg = mgr.item_log(job["id"], "C")
check("log: line kept for the Details view (ANSI removed)", lg["lines"] and "test failure" in lg["lines"][-1]
      and "\x1b" not in lg["lines"][-1], lg)
rev_before = items["C"]["rev"]
mgr._item_log(job, items["C"], "warning", "another line")
check("log: a new line marks the video as changed (so it is saved)", items["C"]["rev"] > rev_before)
for h in M.log.handlers: h.flush()
check("log: written to app.log", "test failure" in M.LOG_FILE.read_text())

# ---------- migration from the old state.json
legacy = TMP / "legacy"; legacy.mkdir()
old_job = {k: v for k, v in job.items() if k != "items"} | {"id": "oldjob", "items": [dict(i) for i in job["items"]]}
(legacy / "state.json").write_text(json.dumps([old_job]))
M.DATA_DIR, M.DB_FILE, M.DB_BACKUP = legacy, legacy / "library.db", legacy / "library.backup.db"
M.LEGACY_STATE, M.LEGACY_BACKUP, M.SETTINGS_FILE = legacy / "state.json", legacy / "state.backup.json", legacy / "settings.json"
m2 = M.Manager()
check("migrate: old library loaded", "oldjob" in m2.jobs and len(m2.jobs["oldjob"]["items"]) == len(job["items"]))
check("migrate: written to library.db", any(j["id"] == "oldjob" for j in M.Store(M.DB_FILE, M.DB_BACKUP).load()))
check("migrate: state.json kept as .migrated", (legacy / "state.json.migrated").exists() and not (legacy / "state.json").exists())

# ---------- API protection (real server)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
env = os.environ | {"YTDL_DATA_DIR": str(TMP / "srv")}
srv = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(port), "--no-browser"],
                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
def get(path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
try:
    for _ in range(50):
        try:
            status, page = get("/"); break
        except OSError:
            time.sleep(0.2)
    token = re.search(r'name="api-token" content="([^"]+)"', page)
    check("api: page carries a session token", status == 200 and token)
    check("api: request without token refused", get("/api/overview")[0] == 403)
    check("api: request with wrong token refused", get("/api/overview", {"X-Token": "nope"})[0] == 403)
    check("api: request with token works", get("/api/overview", {"X-Token": token.group(1)})[0] == 200)
    check("api: foreign Host refused (DNS rebinding)", get("/", {"Host": "evil.example:%d" % port})[0] == 403)
    check("api: foreign Host refused even with token",
          get("/api/overview", {"Host": "evil.example", "X-Token": token.group(1)})[0] == 403)
    check("api: localhost name allowed", get("/", {"Host": f"localhost:{port}"})[0] == 200)
    tok = token.group(1)
    H = {"X-Token": tok}
    # live stream: token only in the query string, only on this path
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/stream?token={tok}")
    with urllib.request.urlopen(req, timeout=10) as r:
        first = r.readline().decode()
        ctype = r.headers.get("content-type", "")
    check("stream: sends the overview and the dashboard", first.startswith("data: ") and "text/event-stream" in ctype
          and {"overview", "activity"} <= set(json.loads(first[6:])), first[:120])
    check("stream: refused without its token", get("/api/stream")[0] == 403)
    check("stream: query token not accepted on other paths", get(f"/api/overview?token={tok}")[0] == 403)
    status, body = get("/api/history", H)
    check("history: answers", status == 200 and "rows" in json.loads(body), body[:200])
    status, body = get("/api/stats?days=7", H)
    check("stats: answers", status == 200 and json.loads(body)["period"] == 7, body[:200])
    status, body = get("/api/activity?q=nothing", H)
    check("activity: has queue ETA, disks and speed history", status == 200 and
          {"eta", "disks", "speed_history", "left_bytes"} <= set(json.loads(body)["stats"]), body[:300])
    def post(path, data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                     data=json.dumps(data or {}).encode(), headers=H | {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    check("file actions: unknown video refused", post("/api/jobs/nope/items/x/reveal")[0] == 404)
    check("queue move: unknown video refused", post("/api/queue/move", {"job": "nope", "id": "x", "to": "top"})[0] == 404)
    check("queue: unknown action refused", post("/api/queue/explode")[0] == 404)
    check("queue: pause all works", post("/api/queue/pause-all")[0] == 200 and post("/api/queue/resume-all")[0] == 200)
    check("open-many: no links refused", post("/api/open-many", {"urls": ["nothing here"]})[0] == 400)
    status, body = get("/api/ytdlp", H)
    check("ytdlp: version and restart flag", status == 200 and json.loads(body)["can_restart"] is True, body[:200])
    # a second copy on the same port exits before starting any downloads (no library created)
    env2 = os.environ | {"YTDL_DATA_DIR": str(TMP / "srv2")}
    r = subprocess.run([sys.executable, str(ROOT / "server.py"), "--port", str(port), "--no-browser"],
                       cwd=ROOT, env=env2, capture_output=True, text=True, timeout=30)
    check("startup: port in use -> exits without starting downloads",
          r.returncode != 0 and "already in use" in r.stderr and not (TMP / "srv2" / "library.db").exists(), r.stderr[-200:])
finally:
    srv.terminate(); srv.wait(5)

# ---------- password (meant for network mode; applies whenever one is set)
s = socket.socket(); s.bind(("127.0.0.1", 0)); pport = s.getsockname()[1]; s.close()
psrv = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(pport), "--no-browser"],
                        cwd=ROOT, env=os.environ | {"YTDL_DATA_DIR": str(TMP / "psrv"), "YTDL_PASSWORD": "s3cret"},
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
opener = urllib.request.build_opener(NoRedirect)
def req_(path, data=None, headers=None):
    r = urllib.request.Request(f"http://127.0.0.1:{pport}{path}", data=data, headers=headers or {})
    try:
        with opener.open(r, timeout=5) as resp:
            return resp.status, resp.read().decode(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers
try:
    for _ in range(50):
        try:
            status, body, hdrs = req_("/"); break
        except OSError:
            time.sleep(0.2)
    check("password: page redirects to the login", status == 303 and hdrs.get("location") == "/login", status)
    check("password: no token handed out before login", "api-token" not in body)
    check("password: API refused without login", req_("/api/overview")[0] == 401)
    status, body, hdrs = req_("/login", b"password=s3cret")
    cookie = (hdrs.get("set-cookie") or "").split(";")[0]
    check("password: right password logs in", status == 303 and cookie.startswith("ytdl_session=")
          and "httponly" in hdrs.get("set-cookie", "").lower(), (status, hdrs.get("set-cookie")))
    status, body, _ = req_("/", headers={"Cookie": cookie})
    check("password: logged-in browser gets the page", status == 200 and 'name="api-token" content="' in body)
    codes = [req_("/login", b"password=wrong")[0] for _ in range(6)]
    check("password: wrong ones refused, then rate-limited", codes[:5] == [401] * 5 and codes[5] == 429, codes)
finally:
    psrv.terminate(); psrv.wait(5)

print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
