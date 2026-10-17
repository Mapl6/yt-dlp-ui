"""Run: .venv/bin/python tests/test_features.py   (needs ffmpeg; no internet required)

Feature tests: scheduler index and priorities, per-video pause, the event log (history and stats),
processing sub-steps and format notes, queue controls, schedule and speed limits, file actions,
playlist removal with files, .m3u export, and the second opinion before calling a file damaged.
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
        for it in job["items"]:
            it["outdir"] = str(out)
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

# ---------- reorder the queue
j5 = make_job("five", ["d1", "d2", "d3", "d4"])
mgr.queue(j5["id"], None, dict(OPTS))
def qorder():
    return [r["id"] for r in mgr.activity(job_id=j5["id"])["queue"]]
mgr.move(j5["id"], "d3", "top")
check("move: to top", qorder()[0] == "d3", qorder())
mgr.move(j5["id"], "d1", "bottom")
check("move: to bottom", qorder()[-1] == "d1", qorder())
mgr.move(j5["id"], "d4", before=(j5["id"], "d2"))
check("move: drag before another video", qorder() == ["d3", "d4", "d2", "d1"], qorder())
mgr.move(j5["id"], "d3")
check("move: drag to the end", qorder() == ["d4", "d2", "d1", "d3"], qorder())
try:
    mgr.move(j3["id"], "ok1", "top"); moved = True
except ValueError:
    moved = False
check("move: a downloaded video can't be moved", not moved)

# ---------- try now, clear hold, pause all
it5 = items_of(j5)
with mgr.cond:
    mgr._set(j5, it5["d2"], next_try=time.time() + 300)
mgr.item_action(j5["id"], "d2", "now")
check("try now: retry wait cleared", it5["d2"]["next_try"] == 0)
mgr._hold_queue("YouTube is limiting requests", 300)
check("hold: queue-wide pause set", mgr.waiting_reason())
mgr.clear_hold()
check("try now: queue-wide pause cleared", mgr.hold is None and "limiting" not in (mgr.waiting_reason() or ""))
mgr.pause_all(True)
check("pause all: every playlist paused", all(j["paused"] for j in mgr.jobs.values()))
check("pause all: dashboard says why", all(r["wait"] == "Playlist paused" for r in mgr.activity(job_id=j5["id"])["queue"]))
mgr.pause_all(False)
check("resume all", not any(j["paused"] for j in mgr.jobs.values()))
mgr.action(j5["id"], "stop")

# ---------- retry all failed (drops the archive line of a missing file)
j6 = make_job("six", ["e1", "e2"])
it6 = items_of(j6)
with mgr.cond:
    it6["e1"].update(status="failed", file_problem="missing", error="gone")
    it6["e2"].update(status="failed", removed=True)
    mgr._touch(j6)
mgr._archive_add(str(out), M.Manager._archive_key(it6["e1"]))
n = mgr.retry_failed()
check("retry all failed: requeued (not the unavailable one)", it6["e1"]["status"] == "queued" and it6["e2"]["status"] == "failed", n)
check("retry all failed: missing file dropped from the archive",
      M.Manager._archive_key(it6["e1"]) not in M.Manager._archive_ids(str(out)))
mgr.action(j6["id"], "stop")

# ---------- download schedule
def at(hour, minute=0):
    t = list(time.localtime()); t[3], t[4], t[5] = hour, minute, 0
    return time.mktime(time.struct_time(t))
mgr.update_settings({"schedule_on": True, "schedule_start": "1:00", "schedule_end": "07:00"})
check("schedule: times normalised", mgr.settings["schedule_start"] == "01:00")
mgr.clock = lambda: at(3)
check("schedule: inside the window", mgr._in_schedule())
mgr.clock = lambda: at(12)
check("schedule: outside the window", not mgr._in_schedule() and "schedule" in (mgr.waiting_reason() or ""), mgr.waiting_reason())
mgr.queue(j6["id"], ["e1"], dict(OPTS))
level["v"] = "good"
with mgr.cond:
    pick = mgr._next_item()
level["v"] = "offline"
check("schedule: nothing starts outside the window", pick is None)
mgr.update_settings({"schedule_start": "23:00", "schedule_end": "06:00"})
mgr.clock = lambda: at(2)
check("schedule: window across midnight", mgr._in_schedule())
mgr.update_settings({"schedule_on": False, "schedule_start": "bad"})
check("schedule: bad time falls back", mgr.settings["schedule_start"] == "01:00")
mgr.clock = time.time
mgr.action(j6["id"], "stop")

# ---------- total speed limit
MB = 1024 * 1024
mgr.update_settings({"rate_limit_mb": 0, "total_rate_mb": 4})
check("speed: total shared between downloads", abs(mgr._rate_for(3) - 4 * MB / 3) < 1)
mgr.update_settings({"rate_limit_mb": 1})
check("speed: per-download limit when lower", mgr._rate_for(2) == 1 * MB)
class FakeYdl: pass
fakes = [FakeYdl(), FakeYdl()]
for f_ in fakes: f_.params = {}
with mgr.cond:
    mgr._ydls.update(fakes)
mgr.update_settings({"rate_limit_mb": 0, "total_rate_mb": 2})
check("speed: running downloads rebalanced", all(f_.params["ratelimit"] == 1 * MB for f_ in fakes), [f_.params for f_ in fakes])
with mgr.cond:
    mgr._ydls.difference_update(fakes)
mgr.update_settings({"total_rate_mb": 0})

# ---------- pause one running video: it stops, keeps its partial download and continues later
j7 = make_job("seven", ["slow1"])
it7 = items_of(j7)["slow1"]
throttle["on"] = True
mgr.queue(j7["id"], None, dict(OPTS))
level["v"] = "good"
check("hold: download running", wait(lambda: it7["status"] == "downloading" and (it7.get("downloaded") or 0) > 0, 60),
      (it7["status"], it7.get("error")))
mgr.item_action(j7["id"], "slow1", "hold")
check("hold: running video goes back to the queue, paused", wait(lambda: it7["status"] == "queued" and it7["held"], 30),
      it7["status"])
time.sleep(2.5)
check("hold: not restarted while paused", it7["status"] == "queued")
check("hold: partial download kept", any(p.name.endswith(".part") for p in out.iterdir() if "[slow1]" in p.name),
      [p.name for p in out.iterdir()])
throttle["on"] = False
mgr.item_action(j7["id"], "slow1", "unhold")
check("unhold: finishes", wait(lambda: it7["status"] == "done", 90), (it7["status"], it7.get("error")))
level["v"] = "offline"

# ---------- open, delete and mark files
opened = []
M._open_path = lambda path, reveal=False: opened.append((path, reveal))
mgr.item_action(j7["id"], "slow1", "reveal")
mgr.item_action(j7["id"], "slow1", "play")
check("reveal/play: the video's own file", opened == [(it7["filepath"], True), (it7["filepath"], False)], opened)
try:
    mgr.item_action(j6["id"], "e2", "play"); refused = False
except ValueError:
    refused = True
check("play: refused without a file", refused)
keep = out / "Unrelated [slow1x].mp4"; keep.write_bytes(b"x")
(out / f"Video slow1 [slow1].en.srt").write_text("1")
mgr.item_action(j7["id"], "slow1", "delete-file")
check("delete file: video's files gone, status not downloaded",
      it7["status"] == "new" and not any("[slow1]" in p.name for p in out.iterdir()), [p.name for p in out.iterdir()])
check("delete file: other files kept", keep.exists())
check("delete file: archive line removed", M.Manager._archive_key(it7) not in M.Manager._archive_ids(str(out)))
mgr.item_action(j7["id"], "slow1", "mark-done")
check("mark as downloaded: skipped and archived", it7["status"] == "skipped"
      and M.Manager._archive_key(it7) in M.Manager._archive_ids(str(out)))

# ---------- export .m3u
m3u = mgr.export_m3u(j3["id"])
text = Path(m3u["path"]).read_text()
check("m3u: written in the playlist folder", m3u["count"] == 1 and Path(m3u["path"]).parent == out, m3u)
check("m3u: relative path and duration", "#EXTINF:30," in text and os.path.basename(it3["ok1"]["filepath"]) in text, text)

# ---------- no false "damaged": a file ffprobe can't open but that decodes fully is kept
check("full decode: good file plays", M._decodes_fully(str(media / "full.mp4"), 30))
junk = TMP / "junk.mp4"; junk.write_bytes(os.urandom(50000))
check("full decode: garbage fails", not M._decodes_fully(str(junk), 30))
cut = TMP / "cut.mp4"; cut.write_bytes(GOOD[: len(GOOD) // 2])
check("full decode: cut-off file fails", not M._decodes_fully(str(cut), 30))
real_verify = M._verify_file
M._verify_file = lambda path, duration: M.DAMAGED
mgr.start_check(j3["id"])
wait(lambda: not j3["check"]["running"])
M._verify_file = real_verify
check("check: file that plays is not renamed", it3["ok1"]["status"] == "done" and os.path.exists(it3["ok1"]["filepath"])
      and j3["check"]["damaged"] == 0, (it3["ok1"]["status"], j3["check"]))
check("check: second opinion logged", any("plays to the end" in l for l in it3["ok1"]["log"]))

# ---------- remove a playlist with its files (only its own, tagged files)
rm = TMP / "rm"; rm.mkdir()
j8 = make_job("eight", ["r1", "r2"])
j9 = make_job("nine", ["r2"])                 # another playlist in the same folder shares r2
with mgr.cond:
    for j in (j8, j9):
        j["outdir"] = str(rm)
        for it in j["items"]:
            it.update(outdir=str(rm), status="done")
for name in ("01 - Video r1 [r1].mp4", "Video r1 [r1].jpg", "02 - Video r2 [r2].mp4", "notes [other].txt"):
    (rm / name).write_bytes(b"x")
mgr._archive_add(str(rm), "test r1"); mgr._archive_add(str(rm), "test r2")
mgr.remove(j8["id"], delete_files=True)
check("remove: playlist gone at once", j8["id"] not in mgr.jobs)
check("remove + delete: own files deleted", wait(lambda: not (rm / "01 - Video r1 [r1].mp4").exists(), 10)
      and not (rm / "Video r1 [r1].jpg").exists(), [p.name for p in rm.iterdir()])
check("remove + delete: file another playlist still has is kept", (rm / "02 - Video r2 [r2].mp4").exists())
check("remove + delete: unrelated file kept", (rm / "notes [other].txt").exists())
check("remove + delete: archive lines of deleted files dropped", M.Manager._archive_ids(str(rm)) == {"test r2"},
      M.Manager._archive_ids(str(rm)))

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
