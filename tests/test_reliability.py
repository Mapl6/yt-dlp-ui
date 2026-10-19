"""Reliability tests: error classification, flaky network and resume, offline gating, verification, rate limiting
and cookie errors, cross-process locks, and recovery from a damaged database."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import manager as M
from helpers import wait_status

OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "numbering": False}
BOT = Exception("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies")
CHROME = 'ERROR: could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"'
FJ = {"id": "fake-job", "title": "Fake", "rev": 0, "items": []}   # not in the library: never scheduled


@pytest.fixture(scope="module")
def mgr_settings():
    return {"concurrency": 2}


@pytest.fixture(scope="module")
def job(mgr, flaky_server):
    """One video from the flaky local server."""
    return mgr.open_url(flaky_server.url)


@pytest.fixture
def it(mgr, job):
    return mgr.jobs[job["id"]]["items"][0]


def clear_out(out):
    for f in (p for p in out.iterdir() if p.is_file()):
        f.unlink()


def fake_item(name, started):
    return {"id": name, "title": name, "status": "downloading", "started": started, "attempts": 0, "net_attempts": 0,
            "rate_attempts": 0, "history": [], "log": [], "rev": 0, "next_try": 0}


# ---------- unit: classification
CASES = {
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


@pytest.mark.parametrize("msg,want", CASES.items(), ids=[f"classify {w:9} <- {m[:45]}" for m, w in CASES.items()])
def test_classify(msg, want):
    assert M._error_kind(msg) == want


def test_settings_coercion():
    s = M._coerce_settings({"concurrency": "abc", "rate_limit_mb": "x", "sleep_between": "2.5", "wait_for_network": "false"})
    assert s["concurrency"] == 3 and s["rate_limit_mb"] == 0.0 and s["sleep_between"] == 2.5 \
        and s["wait_for_network"] is False, f"settings: bad values fall back: {s}"
    assert M._coerce_settings({"concurrency": 99})["concurrency"] == 8, "settings: concurrency clamped"


def test_verify_file(media, tmp_path):
    src = media / "full.mp4"
    assert M._verify_file(str(src), 30) is None, "verify: good file passes"
    assert "incomplete" in (M._verify_file(str(src), 120) or ""), "verify: short file detected"
    trunc = tmp_path / "trunc.mp4"
    trunc.write_bytes(src.read_bytes()[: src.stat().st_size // 3])
    assert M._verify_file(str(trunc), 30) is not None, "verify: truncated file detected"
    assert M._verify_file(str(tmp_path / "nope.mp4"), 30) is not None, "verify: missing file detected"


def test_flaky_download_resumes(mgr, net, job, it, out, flaky_server, good):
    # connection drops 3 times mid-download -> resumes, file is byte-identical
    net.level = "good"
    try:
        mgr.queue(job["id"], None, dict(OPTS))
        st = wait_status(mgr, job, {"done", "failed"})
    finally:
        net.level = "offline"
    same = it["filepath"] and hashlib.sha256(Path(it["filepath"]).read_bytes()).digest() == hashlib.sha256(good).digest()
    assert st == ["done"], \
        f"flaky: download completes after 3 dropped connections: {st} gets={flaky_server.gets} ranges={flaky_server.ranges}"
    assert flaky_server.ranges >= 1, "flaky: resumed with Range requests (no restart from zero)"
    assert same, "flaky: file is byte-identical to source"
    assert M.Manager._archive_key(it) in M.Manager._archive_ids(str(out)), "flaky: archive written after verification"
    assert not list(out.rglob("*.part")), f"flaky: no leftover .part files: {list(out.rglob('*.part'))}"


def test_offline_holds_queue(mgr, net, job, it, out):
    # offline -> nothing starts; back online -> starts and finishes
    clear_out(out)
    it["status"] = "new"
    net.level = "offline"
    mgr.queue(job["id"], None, dict(OPTS))
    time.sleep(4)
    assert it["status"] == "queued", f"offline: queue holds (item still queued): {it['status']}"
    assert "internet" in (mgr.overview()["waiting"] or "").lower(), \
        f"offline: overview explains why: {mgr.overview()['waiting']}"
    net.level = "good"
    try:
        mgr.network_changed("offline", "good")
        assert wait_status(mgr, job, {"done"}) == ["done"], "online: resumes automatically"
    finally:
        net.level = "offline"


def test_network_failure_counters(mgr, job, it):
    # network failure counts as net attempt, not a normal attempt
    j = mgr.jobs[job["id"]]
    it.update(status="downloading", attempts=0, net_attempts=0, rate_attempts=0)
    mgr._handle_failure(j, it, Exception("Unable to download video data: <urlopen error timed out>"))
    assert it["status"] == "queued" and it["attempts"] == 0 and it["net_attempts"] == 1 and it["next_try"] > time.time(), \
        f"net failure: re-queued with backoff, normal attempts untouched: {it['error']}"
    mgr.network_changed("offline", "good")
    assert it["next_try"] == 0, "net restored: backoff cleared"
    it.update(status="downloading")
    try:
        mgr._handle_failure(j, it, Exception("HTTP Error 429: Too Many Requests"))
        assert mgr.hold and mgr.hold["until"] > time.time(), f"rate limit: whole queue held: {mgr.waiting_reason()}"
        assert it["net_attempts"] == 1 and it["rate_attempts"] == 1, \
            f"rate limit: doesn't use up the connection-problem allowance: {(it['net_attempts'], it['rate_attempts'])}"
    finally:
        mgr._clear_hold(probe=False)
        it.update(status="done")


def test_rate_limit_waves(mgr, monkeypatch):
    # waves, restarts, one video first (2026-10-17 incident)
    mgr._clear_hold(probe=False)
    t0 = time.time()
    wave = [fake_item(f"w{i}", t0) for i in range(3)]
    for x in wave:
        mgr._handle_failure(FJ, x, BOT)
    left = mgr.hold["until"] - time.time() if mgr.hold else 0
    assert mgr.rate_strikes == 1 and 50 < left <= 60, \
        f"rate wave: three downloads blocked together count as one strike: {(mgr.rate_strikes, round(left))}"
    assert all(x["status"] == "queued" for x in wave), "rate wave: the blocked videos wait in the queue"
    assert mgr.hold.get("kind") == "ratelimit", f"rate wave: the pause says it's rate limiting: {mgr.hold}"
    assert mgr._limit() == 1, f"rate probe: afterwards one download at a time: {mgr._limit()}"
    time.sleep(0.02)
    mgr._handle_failure(FJ, fake_item("probe", time.time()), BOT)   # started after the last strike: a new wave
    assert mgr.rate_strikes == 2 and mgr.hold["until"] - time.time() > 100, \
        f"rate wave: a download started later is a new wave (pause doubles): {(mgr.rate_strikes, mgr.hold)}"

    saved = object.__new__(M.Manager)                    # what a restarted app reads back
    saved.hold = None
    M.Manager._load_queue_state(saved)
    assert saved.hold and saved.hold["kind"] == "ratelimit" and abs(saved.hold["until"] - mgr.hold["until"]) < 0.01 \
        and saved.rate_probe and saved.rate_strikes == 2, \
        f"restart: the pause and 'one video first' are restored: {(saved.hold, saved.rate_probe, saved.rate_strikes)}"

    # r0vBkQgd5h0 failed after 2 network errors and 6 bot checks: now neither counts against the other, and a block
    # that hits everything keeps the video queued.
    v = fake_item("r0vBkQgd5h0", t0)
    for _ in range(2):
        v["status"] = "downloading"
        mgr._handle_failure(FJ, v, Exception("Unable to download video data: <urlopen error timed out>"))
    for _ in range(M.MAX_RATE_STRIKES + 2):
        v["status"] = "downloading"
        mgr._handle_failure(FJ, v, BOT)
    assert v["net_attempts"] == 2 and v["rate_attempts"] == M.MAX_RATE_STRIKES + 2, \
        f"rate limit: network and rate-limit errors are counted apart: {(v['net_attempts'], v['rate_attempts'])}"
    assert v["status"] == "queued", f"rate limit: a block on every video never fails the video: {(v['status'], v['error'])}"
    for _ in range(M.RATE_BLOCK_STRIKES):
        time.sleep(0.01)
        mgr._handle_failure(FJ, fake_item("again", time.time()), BOT)
    assert "cookies" in mgr.hold["reason"] and "VPN" in mgr.hold["reason"], \
        f"rate limit: repeated waves ask for cookies or a VPN: {mgr.hold['reason']}"
    mgr.last_success = time.time()                       # other videos download fine now; only this one is blocked
    v["status"] = "downloading"
    mgr._handle_failure(FJ, v, BOT)
    assert v["status"] == "failed", \
        f"rate limit: fails a video only when others download meanwhile: {(v['status'], v['error'])}"

    mgr.update_settings({"proxy": "http://127.0.0.1:9"})
    assert mgr.hold is None and not mgr.rate_probe and mgr.rate_strikes == 0, \
        "identity change: a new proxy clears the rate-limit pause and 'one video first'"
    mgr._hold_queue("Disk is full", 60, "disk")
    mgr.update_settings({"proxy": ""})
    assert (mgr.hold or {}).get("kind") == "disk", f"identity change: a disk-full pause stays: {mgr.hold}"
    mgr._clear_hold(probe=False)


def test_cookie_errors(mgr):
    # one pause with an explanation, no attempts used, cleared by changing the cookie settings
    assert M._error_kind(CHROME) == "cookies", "classify cookies  <- could not find chrome cookies database"
    assert M._error_kind(M.CookieLoadError("failed to load cookies")) == "cookies", "classify cookies  <- CookieLoadError"
    assert M._error_kind(BOT) == "ratelimit", "classify ratelimit <- bot check that mentions --cookies"
    mgr.update_settings({"cookies_browser": "chrome"})
    try:
        c = fake_item("c1", time.time())
        mgr._handle_failure(FJ, c, Exception(CHROME))
        assert (mgr.hold or {}).get("kind") == "cookies" and "chrome" in mgr.hold["reason"].lower(), \
            f"cookie error: whole queue paused with an explanation: {mgr.hold}"
        assert c["status"] == "queued" and c["attempts"] == 0 and c["net_attempts"] == 0, \
            f"cookie error: the video waits, no attempt used: {(c['status'], c['attempts'])}"
    finally:
        mgr.update_settings({"cookies_browser": "", "cookies_profile": ""})
    assert mgr.hold is None, f"cookie error: changing the browser clears the pause: {mgr.hold}"


def test_item_logger_prefix():
    # yt-dlp's messages start with ERROR:/WARNING: and the item log adds its own level
    got = []
    lg = M._ItemLogger(lambda level, msg: got.append(msg))
    lg.error("ERROR: [youtube] abc: boom")
    lg.warning("\x1b[0;33mWARNING:\x1b[0m slow")
    assert got == ["[youtube] abc: boom", "slow"], f"item log: no doubled ERROR: prefix: {got}"


@pytest.mark.skipif(sys.platform != "darwin", reason="browser profile folders are macOS paths")
def test_cookie_sources(tmp_path, monkeypatch):
    # browsers and profiles found on this computer (a fake home folder)
    sup = tmp_path / "Library" / "Application Support"
    (sup / "Arc" / "User Data" / "Profile 1" / "Network").mkdir(parents=True)
    (sup / "Arc" / "User Data" / "Profile 1" / "Network" / "Cookies").write_bytes(b"")
    (sup / "Arc" / "User Data" / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Work"}}}}))
    (sup / "Google" / "Chrome" / "Crashpad").mkdir(parents=True)          # installed, but no profile with cookies
    monkeypatch.setenv("HOME", str(tmp_path))
    found = {b["browser"]: b for b in M.cookie_sources()}
    assert found.get("arc", {}).get("profiles") == [{"id": "Profile 1", "name": "Work"}], \
        f"cookie sources: Arc profile found with its name: {found.get('arc')}"
    assert found.get("chrome", {}).get("status") == "no-profiles", \
        f"cookie sources: Chrome without profiles is reported, not offered as working: {found.get('chrome')}"
    assert "brave" not in found, f"cookie sources: browsers that aren't installed are left out: {list(found)}"


def test_rate_probe_ends_after_success(mgr, net, job, it, out):
    # the probe: after the pause, one download runs; when it works, the queue is back to normal
    mgr.update_settings({"cookies_browser": ""})
    mgr.rate_probe, mgr.rate_strikes = True, 2
    clear_out(out)
    it["status"] = "new"
    net.level = "good"
    try:
        mgr.queue(job["id"], None, dict(OPTS))
        assert wait_status(mgr, job, {"done"}) == ["done"], "rate probe: the probe download finishes"
    finally:
        net.level = "offline"
    assert not mgr.rate_probe and mgr.rate_strikes == 0, \
        f"rate probe: a working download ends 'one video first': {(mgr.rate_probe, mgr.rate_strikes)}"


def test_inflight_never_twice(mgr, net, job, it):
    # same video in two jobs/same folder is never downloaded twice at once
    k = mgr._flight_key(mgr.jobs[job["id"]], it)
    prev = it["status"]
    net.level = "good"
    with mgr.cond:
        mgr.inflight.add(k)
        it["status"] = "queued"
        it["next_try"] = 0
        try:
            assert mgr._next_item() is None, "inflight: duplicate is skipped by the scheduler"
        finally:
            net.level = "offline"
            mgr.inflight.discard(k)
            it["status"] = prev if prev != "queued" else "done"


@pytest.mark.skipif(sys.platform == "win32", reason="the holder process uses fcntl")
def test_cross_process_lock(mgr, net, job, it, out):
    # a video locked by another copy of the app is not touched
    other = M._FileLock(M._lock_path(str(out), M.Manager._archive_key(it)))
    script = (f"import fcntl,os,time; fd=os.open({str(other.path)!r}, os.O_RDWR|os.O_CREAT); "
              f"fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(8)")
    other.path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        for f in out.glob("*.mp4"):
            f.unlink()
        (out / M.ARCHIVE_NAME).unlink(missing_ok=True)
        it["status"] = "new"
        net.level = "good"
        mgr.queue(job["id"], None, dict(OPTS))
        time.sleep(3)
        assert it["status"] == "queued" and "Another copy" in (it["error"] or ""), \
            f"lock: video held by another process waits instead of downloading: {(it['status'], it['error'])}"
        holder.wait()
        it["next_try"] = 0
        assert wait_status(mgr, job, {"done"}) == ["done"], "lock: downloads once the other process lets go"
    finally:
        net.level = "offline"
        holder.kill()
    assert not list((out / M.LOCK_DIR_NAME).glob("*.lock")), "lock: lock file cleaned up"


def test_instance_lock(mgr):
    with pytest.raises(M.AlreadyRunning):
        M.Manager()   # instance lock: second manager on same library refused


def test_damaged_database_recovered(mgr, job):
    # a damaged database is kept aside and the backup is loaded. Leaves this module's manager without a
    # database, so it runs last.
    mgr.save_state()
    mgr.store.backup()
    mgr.store.conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(M.DB_FILE) + suffix).unlink(missing_ok=True)
    M.DB_FILE.write_bytes(b"this is not a database" * 100)  # simulate a damaged file
    jobs = M.Store(M.DB_FILE, M.DB_BACKUP).load()
    assert len(jobs) == 1 and jobs[0]["id"] == job["id"], "state: damaged database -> loaded backup"
    assert list(M.DATA_DIR.glob("library.damaged-*.db")), "state: damaged database kept aside, not overwritten"
