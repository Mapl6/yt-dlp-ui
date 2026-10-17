"""Run: .venv/bin/python tests/test_features.py   (needs ffmpeg; no internet required)

Feature tests: scheduler index and priorities, per-video pause, the event log (history and stats),
processing sub-steps and format notes.
"""
import http.server, json, os, shutil, socketserver, sqlite3, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ytdl-features-"))
os.environ["YTDL_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT))
import manager as M
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, "" if cond else extra)

def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False

# ---------- media and a local server for it (Range support, optional throttling)
media = TMP / "media"; media.mkdir()
def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "30",
   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart", str(media / "full.mp4"))
ff("-i", str(media / "full.mp4"), "-an", "-c", "copy", str(media / "v.mp4"))
ff("-i", str(media / "full.mp4"), "-vn", "-c", "copy", str(media / "a.m4a"))
GOOD = (media / "full.mp4").read_bytes()

throttle = {"on": False}
class Media(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        data = (media / self.path.lstrip("/")).read_bytes()
        start, end = 0, len(data) - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start, end = int(a), int(b) if b else len(data) - 1
        body = data[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body))); self.send_header("Accept-Ranges", "bytes")
        if rng: self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        try:
            for i in range(0, len(body), 16384):
                self.wfile.write(body[i:i + 16384])
                if throttle["on"]:
                    time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            pass
class TS(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
srv = TS(("127.0.0.1", 0), Media); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

FAIL_IDS = set()                         # ids whose extraction fails like a private video
def fake_info(vid, url):
    return {"id": vid, "title": f"Video {vid}", "extractor": "test", "extractor_key": "Test", "webpage_url": url,
            "duration": 30, "formats": [
        {"format_id": "137", "url": f"http://127.0.0.1:{port}/v.mp4", "ext": "mp4", "vcodec": "avc1.4d401e",
         "acodec": "none", "width": 640, "height": 360, "filesize": (media / "v.mp4").stat().st_size, "protocol": "http"},
        {"format_id": "140", "url": f"http://127.0.0.1:{port}/a.m4a", "ext": "m4a", "vcodec": "none",
         "acodec": "mp4a.40.2", "filesize": (media / "a.m4a").stat().st_size, "protocol": "http"}]}
real_extract = YoutubeDL.extract_info
def extract(self, url, download=True, *a, **k):
    if str(url).startswith("test://"):
        vid = str(url)[7:]
        if vid in FAIL_IDS:
            raise DownloadError("ERROR: [test] %s: Private video. Sign in if you've been granted access" % vid)
        return self.process_ie_result(fake_info(vid, url), download=download)
    return real_extract(self, url, download, *a, **k)
YoutubeDL.extract_info = extract

mgr = M.Manager()
out = TMP / "out"; out.mkdir()
mgr.update_settings({"output_dir": str(out), "concurrency": 2})
level = {"v": "offline"}                 # keep workers idle until a test wants them
mgr.net_level = lambda: level["v"]
OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "numbering": False,
        "output_dir": str(out)}

def make_job(name, ids, created=None):
    with mgr.cond:
        job = mgr._create(f"test://list-{name}", {
            "kind": "playlist", "source_id": name, "title": f"List {name}", "thumbnail": None, "heights": [360],
            "items": [{"index": i + 1, "id": v, "title": f"Video {v}", "url": f"test://{v}", "duration": 30,
                       "extractor": "Test"} for i, v in enumerate(ids)]})
        job["outdir"] = str(out)
        if created:
            job["created"] = created
        mgr._touch(job)
    return job

def items_of(job):
    return {it["id"]: it for it in job["items"]}

def full_scan():
    return {(j["id"], it["id"]) for j in mgr.jobs.values() for it in j["items"] if it["status"] in M.LIVE}

# ---------- scheduler index and priorities
j1 = make_job("one", ["a1", "a2", "a3"], created=1000)
j2 = make_job("two", ["b1", "b2"], created=2000)
mgr.queue(j1["id"], None, dict(OPTS))
mgr.queue(j2["id"], None, dict(OPTS))
check("index: queued items indexed", set(mgr._live) == full_scan() and len(mgr._live) == 5, len(mgr._live))
with mgr.cond:
    order = [it["id"] for _, it in mgr._queue_order()]
check("order: oldest playlist first, then playlist order", order == ["a1", "a2", "a3", "b1", "b2"], order)
with mgr.cond:
    mgr._set(j2, items_of(j2)["b2"], priority=5)
    order = [it["id"] for _, it in mgr._queue_order()]
check("order: higher priority goes first", order[0] == "b2", order)
with mgr.cond:
    mgr._set(j2, items_of(j2)["b2"], held=True)
level["v"] = "good"
with mgr.cond:
    pick = mgr._next_item()
level["v"] = "offline"
check("held: a paused video is skipped by the scheduler", pick and pick[1]["id"] == "a1", pick and pick[1]["id"])
mgr.item_action(j1["id"], "a3", "cancel")
check("index: cancelled video leaves the index", ("%s" % j1["id"], "a3") not in mgr._live and set(mgr._live) == full_scan())
mgr.action(j2["id"], "stop")
check("index: stopping a playlist empties its part", not any(k[0] == j2["id"] for k in mgr._live) and set(mgr._live) == full_scan())
mgr.action(j2["id"], "remove")
mgr.action(j1["id"], "stop")
check("index: empty after stop/remove", not mgr._live and not full_scan())

# ---------- a real download: event log, format note, sub-step cleared
j3 = make_job("three", ["ok1", "bad1"])
FAIL_IDS.add("bad1")
mgr.queue(j3["id"], None, dict(OPTS))
level["v"] = "good"
it3 = items_of(j3)
check("download: finishes", wait(lambda: it3["ok1"]["status"] == "done" and it3["bad1"]["status"] == "failed", 90),
      (it3["ok1"]["status"], it3["ok1"]["error"], it3["bad1"]["status"]))
level["v"] = "offline"
check("format note: chosen format recorded", "640x360" in (it3["ok1"].get("fmt") or "") and "avc1" in it3["ok1"]["fmt"],
      it3["ok1"].get("fmt"))
check("step: cleared once finished", it3["ok1"].get("step") is None)
check("history: failed attempt recorded with its kind", it3["bad1"]["history"] and it3["bad1"]["history"][-1]["kind"] == "permanent",
      it3["bad1"].get("history"))
rows = mgr.history()["rows"]
done = [r for r in rows if r["id"] == "ok1"]
failed = [r for r in rows if r["id"] == "bad1"]
check("events: done download logged with size and time", done and done[0]["bytes"] > 0 and done[0]["seconds"] > 0
      and done[0]["status"] == "done", done)
check("events: failure logged with kind", failed and failed[0]["kind"] == "permanent" and failed[0]["current"] == "failed", failed)
act = mgr.activity()
check("activity: recently finished comes from the event log", {r["id"] for r in act["recent"]} >= {"ok1", "bad1"})
check("activity: 24-hour counts", act["stats"]["done_today"] >= 1 and act["stats"]["failed_today"] >= 1, act["stats"])
# retrying keeps the history row
mgr.item_action(j3["id"], "bad1", "retry")
check("events: a retried video stays in Recently finished", any(r["id"] == "bad1" for r in mgr.history()["rows"]))
check("events: current status follows the video", next(r for r in mgr.history()["rows"] if r["id"] == "bad1")["current"] == "queued")
mgr.item_action(j3["id"], "bad1", "cancel")
stats = mgr.stats(7)
check("stats: totals, success rate, kinds", stats["done"] >= 1 and stats["failed"] >= 1 and stats["success"] is not None
      and stats["kinds"][0]["kind"] == "permanent" and stats["days"], stats)
n_before = mgr.store.count_events()
mgr.clear_history()
check("clear: Recently finished emptied", not mgr.history()["rows"] and mgr.store.count_events() == 0)
check("clear: stats keep the rows", mgr.stats(7)["done"] == stats["done"])

# ---------- activity filters
j4 = make_job("four", ["c1", "c2"])
mgr.queue(j4["id"], None, dict(OPTS))
check("activity: filter by playlist", {r["id"] for r in mgr.activity(job_id=j4["id"])["queue"]} == {"c1", "c2"})
check("activity: title search", [r["id"] for r in mgr.activity(q="video c2")["queue"]] == ["c2"])
mgr.action(j4["id"], "stop")

# ---------- event log thread safety: many writers at once with the saver
errors = []
def writer(n):
    try:
        for i in range(20):
            mgr.store.add_events([{"time": time.time(), "job_id": "x", "item_id": f"{n}-{i}", "status": "done"}])
            mgr._set(j3, it3["ok1"], title=f"T{n}{i}")
            mgr.save_state()
    except Exception as exc:
        errors.append(exc)
threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
[t.start() for t in threads]; [t.join() for t in threads]
check("store: concurrent writers never fail", not errors, errors[:2])
check("store: all rows written", mgr.store.count_events(hidden=True) >= 120)

# ---------- backfill: a library from before the event log gets its downloads added once
lib = TMP / "lib2"; lib.mkdir()
f = out / "Old [old1].mp4"; f.write_bytes(GOOD)
os.utime(f, (1_700_000_000, 1_700_000_000))
old_job = {k: v for k, v in j3.items() if k != "items"} | {"id": "oldjob", "rev": 3, "items": [
    dict(it3["ok1"], id="old1", title="Old", status="done", filepath=str(f), finished=None, rev=3)]}
con = sqlite3.connect(lib / "library.db")
con.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
                  "CREATE TABLE items (job_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (job_id, id));")
con.execute("INSERT INTO jobs VALUES (?, ?)", ("oldjob", json.dumps({k: v for k, v in old_job.items() if k != "items"})))
con.execute("INSERT INTO items VALUES (?, ?, ?)", ("oldjob", "old1", json.dumps(old_job["items"][0])))
con.commit(); con.close()
M.DATA_DIR, M.DB_FILE, M.DB_BACKUP = lib, lib / "library.db", lib / "library.backup.db"
M.LEGACY_STATE, M.LEGACY_BACKUP, M.SETTINGS_FILE = lib / "state.json", lib / "state.backup.json", lib / "settings.json"
m2 = M.Manager()
m2.net_level = lambda: "offline"
rows = m2.history()["rows"]
check("backfill: earlier download added, dated by its file", len(rows) == 1 and rows[0]["id"] == "old1"
      and int(rows[0]["finished"]) == 1_700_000_000 and rows[0]["bytes"] == len(GOOD), rows)
check("backfill: done once (version recorded)", m2.store.version == 1)

srv.shutdown()
print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
