"""Run: .venv/bin/python tests/test_reliability.py   (needs ffmpeg; no internet required)

Reliability tests for manager.py: flaky network, offline gating, verification, state recovery."""
import hashlib, http.server, json, os, socketserver, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ytdl-test-"))
os.environ["YTDL_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT))
import manager as M

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, extra)

# ---------- unit: classification
cases = {
    "Unable to download video data: <urlopen error [Errno 60] Operation timed out>": "network",
    "HTTP Error 503: Service Unavailable": "network",
    "Connection reset by peer": "network",
    "[Errno 28] No space left on device": "disk",
    "Sign in to confirm you’re not a bot. Use --cookies-from-browser": "ratelimit",
    "HTTP Error 429: Too Many Requests": "ratelimit",
    "Private video. Sign in if you've been granted access": "permanent",
    "Video unavailable": "permanent",
    "Something odd happened": "other",
}
for msg, want in cases.items():
    check(f"classify {want:9} <- {msg[:45]}", M._error_kind(msg) == want, M._error_kind(msg))

# ---------- unit: settings coercion
s = M._coerce_settings({"concurrency": "abc", "rate_limit_mb": "x", "sleep_between": "2.5", "wait_for_network": "false"})
check("settings: bad values fall back", s["concurrency"] == 3 and s["rate_limit_mb"] == 0.0 and s["sleep_between"] == 2.5 and s["wait_for_network"] is False, s)
check("settings: concurrency clamped", M._coerce_settings({"concurrency": 99})["concurrency"] == 8)

# ---------- unit: file verification
media = TMP / "media"; media.mkdir()
src = media / "clip.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi",
                "-i", "sine=frequency=440", "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-movflags", "+faststart", str(src)], check=True)
check("verify: good file passes", M._verify_file(str(src), 30) is None)
check("verify: short file detected", "incomplete" in (M._verify_file(str(src), 120) or ""), M._verify_file(str(src), 120))
trunc = TMP / "trunc.mp4"; trunc.write_bytes(src.read_bytes()[: src.stat().st_size // 3])
check("verify: truncated file detected", M._verify_file(str(trunc), 30) is not None, M._verify_file(str(trunc), 30))
check("verify: missing file detected", M._verify_file(str(TMP / "nope.mp4"), 30) is not None)

# ---------- flaky HTTP server: drops the connection mid-transfer for the first N GETs, supports Range resume
DATA = src.read_bytes()
state = {"gets": 0, "drops_left": 3, "ranges": 0}

class Flaky(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _range(self):
        r = self.headers.get("Range")
        if r and r.startswith("bytes="):
            a, _, b = r[6:].partition("-")
            return int(a), (int(b) if b else len(DATA) - 1)
        return None
    def do_HEAD(self):
        self.send_response(200); self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(DATA))); self.send_header("Accept-Ranges", "bytes"); self.end_headers()
    def do_GET(self):
        state["gets"] += 1
        rng = self._range()
        start, end = rng if rng else (0, len(DATA) - 1)
        if rng: state["ranges"] += 1
        body = DATA[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4"); self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if rng: self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.end_headers()
        if state["drops_left"] > 0 and len(body) > 50_000:
            state["drops_left"] -= 1
            self.wfile.write(body[: len(body) // 3]); self.wfile.flush()
            self.connection.shutdown(2)              # simulate the connection dying mid-download
            return
        self.wfile.write(body)

class TS(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
srv = TS(("127.0.0.1", 0), Flaky); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{port}/clip.mp4"

out = TMP / "out"
mgr = M.Manager()
mgr.update_settings({"output_dir": str(out), "concurrency": 2})
level = {"v": "good"}
mgr.net_level = lambda: level["v"]
OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "numbering": False}

def wait_status(job, want, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        st = [it["status"] for it in mgr.jobs[job["id"]]["items"]]
        if all(s in want for s in st):
            return st
        time.sleep(0.3)
    return [ (it["status"], it["error"]) for it in mgr.jobs[job["id"]]["items"]]

# ---------- e2e 1: connection drops 3 times mid-download -> resumes, file is byte-identical
job = mgr.open_url(URL)
mgr.queue(job["id"], None, dict(OPTS))
st = wait_status(job, {"done", "failed"})
it = mgr.jobs[job["id"]]["items"][0]
same = it["filepath"] and hashlib.sha256(Path(it["filepath"]).read_bytes()).hexdigest() == hashlib.sha256(DATA).hexdigest()
check("flaky: download completes after 3 dropped connections", st == ["done"], f"{st} gets={state['gets']} range-resumes={state['ranges']}")
check("flaky: resumed with Range requests (no restart from zero)", state["ranges"] >= 1)
check("flaky: file is byte-identical to source", same)
check("flaky: archive written after verification", f"{M.Manager._archive_key(it)}" in M.Manager._archive_ids(str(out)))
check("flaky: no leftover .part files", not list(out.rglob("*.part")), list(out.rglob("*.part")))

# ---------- e2e 2: offline -> nothing starts; back online -> starts and finishes
for f in (p for p in out.iterdir() if p.is_file()):
    f.unlink()
it["status"] = "new"
level["v"] = "offline"
mgr.queue(job["id"], None, dict(OPTS))
time.sleep(4)
check("offline: queue holds (item still queued)", it["status"] == "queued", it["status"])
check("offline: overview explains why", "internet" in (mgr.overview()["waiting"] or "").lower(), mgr.overview()["waiting"])
level["v"] = "good"; mgr.network_changed("offline", "good")
check("online: resumes automatically", wait_status(job, {"done"}) == ["done"])

# ---------- e2e 3: network failure counts as net attempt, not a normal attempt
it.update(status="downloading", attempts=0, net_attempts=0)
mgr._handle_failure(mgr.jobs[job["id"]], it, Exception("Unable to download video data: <urlopen error timed out>"))
check("net failure: re-queued with backoff, normal attempts untouched",
      it["status"] == "queued" and it["attempts"] == 0 and it["net_attempts"] == 1 and it["next_try"] > time.time(), it["error"])
mgr.network_changed("offline", "good")
check("net restored: backoff cleared", it["next_try"] == 0)
it.update(status="downloading")
mgr._handle_failure(mgr.jobs[job["id"]], it, Exception("HTTP Error 429: Too Many Requests"))
check("rate limit: whole queue held", mgr.hold and mgr.hold["until"] > time.time(), mgr.waiting_reason())
check("rate limit: doesn't use up the connection-problem allowance", it["net_attempts"] == 1 and it["rate_attempts"] == 1,
      (it["net_attempts"], it["rate_attempts"]))
mgr._clear_hold(probe=False)
it.update(status="done")

# ---------- rate limiting: waves, restarts, one video first, cookie and proxy changes (2026-10-17 incident)
level["v"] = "offline"                               # keep the workers idle while failures are simulated
BOT = Exception("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies")
fj = {"id": "fake-job", "title": "Fake", "rev": 0, "items": []}   # not in the library: never scheduled

def fake_item(name, started):
    return {"id": name, "title": name, "status": "downloading", "started": started, "attempts": 0, "net_attempts": 0,
            "rate_attempts": 0, "history": [], "log": [], "rev": 0, "next_try": 0}

t0 = time.time()
wave = [fake_item(f"w{i}", t0) for i in range(3)]
for x in wave:
    mgr._handle_failure(fj, x, BOT)
left = mgr.hold["until"] - time.time() if mgr.hold else 0
check("rate wave: three downloads blocked together count as one strike", mgr.rate_strikes == 1 and 50 < left <= 60,
      (mgr.rate_strikes, round(left)))
check("rate wave: the blocked videos wait in the queue", all(x["status"] == "queued" for x in wave), [x["status"] for x in wave])
check("rate wave: the pause says it's rate limiting", mgr.hold.get("kind") == "ratelimit", mgr.hold)
check("rate probe: afterwards one download at a time", mgr._limit() == 1, mgr._limit())
time.sleep(0.02)
mgr._handle_failure(fj, fake_item("probe", time.time()), BOT)   # started after the last strike: a new wave
check("rate wave: a download started later is a new wave (pause doubles)",
      mgr.rate_strikes == 2 and mgr.hold["until"] - time.time() > 100, (mgr.rate_strikes, mgr.hold))

saved = object.__new__(M.Manager)                    # what a restarted app reads back
saved.hold = None
M.Manager._load_queue_state(saved)
check("restart: the pause and 'one video first' are restored",
      saved.hold and saved.hold["kind"] == "ratelimit" and abs(saved.hold["until"] - mgr.hold["until"]) < 0.01
      and saved.rate_probe and saved.rate_strikes == 2, (saved.hold, saved.rate_probe, saved.rate_strikes))

# r0vBkQgd5h0 failed after 2 network errors and 6 bot checks: now neither counts against the other, and a block
# that hits everything keeps the video queued.
v = fake_item("r0vBkQgd5h0", t0)
for _ in range(2):
    v["status"] = "downloading"
    mgr._handle_failure(fj, v, Exception("Unable to download video data: <urlopen error timed out>"))
for _ in range(M.MAX_RATE_STRIKES + 2):
    v["status"] = "downloading"
    mgr._handle_failure(fj, v, BOT)
check("rate limit: network and rate-limit errors are counted apart",
      v["net_attempts"] == 2 and v["rate_attempts"] == M.MAX_RATE_STRIKES + 2, (v["net_attempts"], v["rate_attempts"]))
check("rate limit: a block on every video never fails the video", v["status"] == "queued", (v["status"], v["error"]))
for _ in range(M.RATE_BLOCK_STRIKES):
    time.sleep(0.01)
    mgr._handle_failure(fj, fake_item("again", time.time()), BOT)
check("rate limit: repeated waves ask for cookies or a VPN", "cookies" in mgr.hold["reason"] and "VPN" in mgr.hold["reason"],
      mgr.hold["reason"])
mgr.last_success = time.time()                       # other videos download fine now; only this one is blocked
v["status"] = "downloading"
mgr._handle_failure(fj, v, BOT)
check("rate limit: fails a video only when others download meanwhile", v["status"] == "failed", (v["status"], v["error"]))

mgr.update_settings({"proxy": "http://127.0.0.1:9"})
check("identity change: a new proxy clears the rate-limit pause and 'one video first'",
      mgr.hold is None and not mgr.rate_probe and mgr.rate_strikes == 0, (mgr.hold, mgr.rate_probe, mgr.rate_strikes))
mgr._hold_queue("Disk is full", 60, "disk")
mgr.update_settings({"proxy": ""})
check("identity change: a disk-full pause stays", (mgr.hold or {}).get("kind") == "disk", mgr.hold)
mgr._clear_hold(probe=False)

# cookie errors: one pause with an explanation, no attempts used, cleared by changing the cookie settings
CHROME = 'ERROR: could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"'
check("classify cookies  <- could not find chrome cookies database", M._error_kind(CHROME) == "cookies", M._error_kind(CHROME))
check("classify cookies  <- CookieLoadError", M._error_kind(M.CookieLoadError("failed to load cookies")) == "cookies")
check("classify ratelimit <- bot check that mentions --cookies", M._error_kind(BOT) == "ratelimit", M._error_kind(BOT))
mgr.update_settings({"cookies_browser": "chrome"})
c = fake_item("c1", time.time())
mgr._handle_failure(fj, c, Exception(CHROME))
check("cookie error: whole queue paused with an explanation",
      (mgr.hold or {}).get("kind") == "cookies" and "chrome" in mgr.hold["reason"].lower(), mgr.hold)
check("cookie error: the video waits, no attempt used",
      c["status"] == "queued" and c["attempts"] == 0 and c["net_attempts"] == 0, (c["status"], c["attempts"]))
mgr.update_settings({"cookies_browser": "", "cookies_profile": ""})
check("cookie error: changing the browser clears the pause", mgr.hold is None, mgr.hold)

# yt-dlp's messages start with ERROR:/WARNING: and the item log adds its own level
got = []
lg = M._ItemLogger(lambda level, msg: got.append(msg))
lg.error("ERROR: [youtube] abc: boom")
lg.warning("\x1b[0;33mWARNING:\x1b[0m slow")
check("item log: no doubled ERROR: prefix", got == ["[youtube] abc: boom", "slow"], got)

# browsers and profiles found on this computer (a fake home folder)
if sys.platform == "darwin":
    real_home = os.environ["HOME"]
    home = TMP / "home"
    sup = home / "Library" / "Application Support"
    (sup / "Arc" / "User Data" / "Profile 1" / "Network").mkdir(parents=True)
    (sup / "Arc" / "User Data" / "Profile 1" / "Network" / "Cookies").write_bytes(b"")
    (sup / "Arc" / "User Data" / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Work"}}}}))
    (sup / "Google" / "Chrome" / "Crashpad").mkdir(parents=True)          # installed, but no profile with cookies
    os.environ["HOME"] = str(home)
    try:
        found = {b["browser"]: b for b in M.cookie_sources()}
    finally:
        os.environ["HOME"] = real_home
    check("cookie sources: Arc profile found with its name",
          found.get("arc", {}).get("profiles") == [{"id": "Profile 1", "name": "Work"}], found.get("arc"))
    check("cookie sources: Chrome without profiles is reported, not offered as working",
          found.get("chrome", {}).get("status") == "no-profiles", found.get("chrome"))
    check("cookie sources: browsers that aren't installed are left out", "brave" not in found, list(found))

# the probe: after the pause, one download runs; when it works, the queue is back to normal
mgr.update_settings({"cookies_browser": ""})
mgr.rate_probe, mgr.rate_strikes = True, 2
for f in (p for p in out.iterdir() if p.is_file()):
    f.unlink()
it["status"] = "new"
level["v"] = "good"
mgr.queue(job["id"], None, dict(OPTS))
check("rate probe: the probe download finishes", wait_status(job, {"done"}) == ["done"])
check("rate probe: a working download ends 'one video first'", not mgr.rate_probe and mgr.rate_strikes == 0,
      (mgr.rate_probe, mgr.rate_strikes))

# ---------- e2e 4: same video in two jobs/same folder is never downloaded twice at once
k = mgr._flight_key(mgr.jobs[job["id"]], it)
mgr.inflight.add(k); it["status"] = "queued"; it["next_try"] = 0
with mgr.cond:
    check("inflight: duplicate is skipped by the scheduler", mgr._next_item() is None)
    mgr.inflight.discard(k); it["status"] = "done"

# ---------- cross-process lock: a video locked by another copy of the app is not touched
other = M._FileLock(M._lock_path(str(out), M.Manager._archive_key(it)))
script = f"import fcntl,os,time; fd=os.open({str(other.path)!r}, os.O_RDWR|os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(8)"
other.path.parent.mkdir(parents=True, exist_ok=True)
holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
holder.stdout.readline()
for f in out.glob("*.mp4"): f.unlink()
(out / M.ARCHIVE_NAME).unlink()
it["status"] = "new"
mgr.queue(job["id"], None, dict(OPTS))
time.sleep(3)
check("lock: video held by another process waits instead of downloading",
      it["status"] == "queued" and "Another copy" in (it["error"] or ""), (it["status"], it["error"]))
holder.wait()
it["next_try"] = 0
check("lock: downloads once the other process lets go", wait_status(job, {"done"}) == ["done"])
check("lock: lock file cleaned up", not list((out / M.LOCK_DIR_NAME).glob("*.lock")))
try:
    M.Manager(); check("instance lock: second manager on same library refused", False)
except M.AlreadyRunning:
    check("instance lock: second manager on same library refused", True)

# ---------- state: a damaged database is kept aside and the backup is loaded
mgr.save_state(); mgr.store.backup()
mgr.store.conn.close()
for suffix in ("-wal", "-shm"):
    Path(str(M.DB_FILE) + suffix).unlink(missing_ok=True)
M.DB_FILE.write_bytes(b"this is not a database" * 100)  # simulate a damaged file
jobs = M.Store(M.DB_FILE, M.DB_BACKUP).load()
check("state: damaged database -> loaded backup", len(jobs) == 1 and jobs[0]["id"] == job["id"])
check("state: damaged database kept aside, not overwritten", list(M.DATA_DIR.glob("library.damaged-*.db")))

print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
