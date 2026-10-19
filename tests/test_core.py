"""Core tests: playlist merge (reorder / removed videos / numbering), format options, error classification
by exception type, incremental SQLite saves and migration, per-video logs, and API protection (real server)."""
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError

import manager as M
from helpers import ROOT, entry


def playlist(*entries):
    return {"kind": "playlist", "source_id": "PL1", "title": "Test list", "thumbnail": None,
            "heights": M.VIDEO_HEIGHTS, "items": list(entries)}


@pytest.fixture(scope="module")
def merged(mgr):
    """A 5-video playlist refreshed twice: reordered with a new and a private video, D and E gone; then D back.
    Records what each step returned, for test_merge."""
    r = SimpleNamespace()
    with mgr.cond:
        job = mgr._create("https://youtube.com/playlist?list=PL1",
                          playlist(*(entry(i + 1, v) for i, v in enumerate("ABCDE"))))
        by = lambda: {it["id"]: it for it in job["items"]}
        by()["A"]["status"] = "done"
        r.numbers_before = {k: v["number"] for k, v in by().items()}
        # New order: F (new, at the top like a channel), B, A, C, X (private, new). D and E were removed.
        r.added = mgr._merge(job, playlist(entry(1, "F"), entry(2, "B"), entry(3, "A"), entry(4, "C"),
                                           entry(5, "X", "[Private video]")))
        r.first = {k: dict(v) for k, v in by().items()}
        r.summary = mgr.summary(job)
        r.empty_added = mgr._merge(job, playlist())
        r.empty_a_removed = by()["A"]["removed"]
        mgr._merge(job, playlist(entry(1, "F"), entry(2, "B"), entry(3, "A"), entry(4, "C"), entry(5, "D")))
        mgr._touch(job)                                  # as refresh() does after merging
    r.job, r.items = job, by()
    return r


def test_merge(mgr, merged):
    first, items, job = merged.first, merged.items, merged.job
    assert merged.added == 2, f"merge: two new entries added: {merged.added}"
    assert [first[k]["index"] for k in "FBAC"] == [1, 2, 3, 4], "merge: positions follow the playlist"
    assert all(first[k]["number"] == merged.numbers_before[k] for k in "ABCDE"), \
        "merge: known videos keep their file numbers"
    assert first["F"]["number"] == "06" and first["X"]["number"] == "07", \
        f"merge: new videos get the next free numbers: {(first['F']['number'], first['X']['number'])}"
    assert len({it["number"] for it in first.values()}) == len(first), "merge: file numbers never collide"
    assert first["D"]["removed"] and first["E"]["removed"] and first["D"]["index"] == 6 and first["E"]["index"] == 7, \
        "merge: videos gone from the playlist are flagged and moved to the end"
    assert first["X"]["removed"] and not first["F"]["removed"], "merge: private video is flagged unavailable"
    assert first["A"]["status"] == "done", "merge: downloaded video keeps its status"
    s = merged.summary
    assert s["counts"]["removed"] == 3 and s["counts"]["new"] == 3, \
        f"summary: unavailable not counted as missing: {s['counts']}"
    assert abs(s["percent"] - 25.0) < 0.01, f"summary: percent uses available videos: {s['percent']}"
    assert merged.empty_added == 0 and not merged.empty_a_removed, "merge: empty fetch changes nothing"
    assert not items["D"]["removed"] and items["D"]["index"] == 5, "merge: a video that comes back is available again"
    # file-number width: refreshing a 5-video list must not widen numbers to 2+ digits more than needed
    assert job["width"] == 2 and items["F"]["number"] == "06", f"merge: number width unchanged by refresh: {job['width']}"


def test_merge_big_list(mgr):
    with mgr.cond:
        big = mgr._create("https://youtube.com/playlist?list=BIG",
                          {**playlist(*(entry(i + 1, f"b{i}") for i in range(120))), "source_id": "BIG"})
        mgr._merge(big, {**playlist(*(entry(i + 1, f"b{i}") for i in range(121))), "source_id": "BIG"})
    try:
        assert big["width"] == 3 and big["items"][-1]["number"] == "121", \
            f"merge: 120-video list keeps 3-digit numbers after refresh: {(big['width'], big['items'][-1]['number'])}"
        # a fetch cut short (40 of 121) must not flag the other 81 videos as unavailable
        with mgr.cond:
            mgr._merge(big, {**playlist(*(entry(i + 1, f"b{i}") for i in range(40))), "source_id": "BIG"})
        assert not any(it["removed"] for it in big["items"]), "merge: partial fetch does not flag the rest as unavailable"
    finally:
        del mgr.jobs[big["id"]]


def test_queue_unplugged_drive(mgr, merged):
    # queueing to an unplugged external drive must not create folders on the system disk
    with pytest.raises(OSError) as exc:
        mgr.queue(merged.job["id"], None, {"quality": "720", "output_dir": "/Volumes/NoSuchDrive_ytdl/x"})
    assert "not connected" in str(exc.value) and not Path("/Volumes/NoSuchDrive_ytdl").exists(), \
        f"queue: unplugged drive refused: {exc.value}"


def test_queue_all_skips_unavailable(mgr, merged):
    items = merged.items
    try:
        n = mgr.queue(merged.job["id"], None, {"quality": "720"})
        assert n == 4 and items["E"]["status"] == "new" and items["X"]["status"] == "new", \
            f"queue all: skips unavailable videos: {n}"
    finally:
        mgr.action(merged.job["id"], "stop")


def test_format_options():
    o, pps = M.build_format_opts({"quality": "mp3"})
    ea = next(p for p in pps if p["key"] == "FFmpegExtractAudio")
    assert ea["preferredcodec"] == "mp3" and ea["preferredquality"] == "0", "format: mp3 extracts best-quality audio"
    o, pps = M.build_format_opts({"quality": "720", "container": "mp4", "subtitles": True, "thumbnail": False})
    assert o["format_sort"] == ["res:720", "vcodec:h264", "acodec:aac"], "format: 720p mp4 prefers h264 at 720"
    assert o.get("writesubtitles") and "writethumbnail" not in o \
        and any(p["key"] == "FFmpegEmbedSubtitle" for p in pps), "format: subtitles embedded, no thumbnail"


# ---------- error classification by exception type (not message text)
def http(status):
    err = HTTPError(Response(io.BytesIO(b""), "https://x", {}, status=status, reason="x"))
    return DownloadError("ERROR: something", exc_info=(type(err), err, None))


ERROR_CASES = [
    (http(429), "ratelimit"), (http(503), "network"), (http(403), "network"), (http(404), "permanent"),
    (DownloadError("ERROR: odd wording", exc_info=(OSError, OSError(28, "No space"), None)), "disk"),
    (DownloadError("ERROR: odd wording", exc_info=(TransportError, TransportError("weird"), None)), "network"),
    (DownloadError("ERROR: x", exc_info=(GeoRestrictedError, GeoRestrictedError("geo"), None)), "permanent"),
    (ExtractorError("Some new wording yt-dlp invented", expected=True), "permanent"),
    (ExtractorError("Unable to parse player", expected=False), "other"),
]


@pytest.mark.parametrize("exc,want", ERROR_CASES,
                         ids=[f"error type -> {w:9} ({type(e).__name__}: {str(e)[:30]})" for e, w in ERROR_CASES])
def test_error_kind_by_type(exc, want):
    assert M._error_kind(exc) == want


def test_unavailable_titles():
    assert all(M.UNAVAILABLE_TITLE.match(t) for t in ("[Private video]", "[Deleted video]")) \
        and not M.UNAVAILABLE_TITLE.match("[MV] Song"), "unavailable titles"


def test_incremental_saves(mgr, merged, monkeypatch):
    mgr.save_state()
    written = {}
    real_write = mgr.store.write
    monkeypatch.setattr(mgr.store, "write",
                        lambda j, i, d: (written.update(jobs=len(j), items=len(i)), real_write(j, i, d))[1])
    mgr._set(merged.job, merged.items["B"], title="Renamed B")
    mgr.save_state()
    monkeypatch.undo()
    assert written == {"jobs": 1, "items": 1}, f"save: only the changed video is written: {written}"
    reloaded = M.Store(M.DB_FILE, M.DB_BACKUP).load()
    assert any(it["title"] == "Renamed B" for j in reloaded for it in j["items"]), "save: change is in the database"


def test_item_log(mgr, merged):
    job, items = merged.job, merged.items
    mgr._item_log(job, items["C"], "error", "\x1b[0;31mERROR:\x1b[0m test failure")
    lg = mgr.item_log(job["id"], "C")
    assert lg["lines"] and "test failure" in lg["lines"][-1] and "\x1b" not in lg["lines"][-1], \
        f"log: line kept for the Details view (ANSI removed): {lg}"
    rev_before = items["C"]["rev"]
    mgr._item_log(job, items["C"], "warning", "another line")
    assert items["C"]["rev"] > rev_before, "log: a new line marks the video as changed (so it is saved)"
    for h in M.log.handlers:
        h.flush()
    assert "test failure" in M.LOG_FILE.read_text(), "log: written to app.log"


def test_migrate_legacy_state(merged, tmp, make_manager):
    job = merged.job
    legacy = tmp / "legacy"
    legacy.mkdir()
    old_job = {k: v for k, v in job.items() if k != "items"} | {"id": "oldjob", "items": [dict(i) for i in job["items"]]}
    (legacy / "state.json").write_text(json.dumps([old_job]))
    m2 = make_manager(legacy)
    assert "oldjob" in m2.jobs and len(m2.jobs["oldjob"]["items"]) == len(job["items"]), "migrate: old library loaded"
    assert any(j["id"] == "oldjob" for j in M.Store(M.DB_FILE, M.DB_BACKUP).load()), "migrate: written to library.db"
    assert (legacy / "state.json.migrated").exists() and not (legacy / "state.json").exists(), \
        "migrate: state.json kept as .migrated"


# ---------- API protection (a real server process)

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(data_dir, **env):
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(port), "--no-browser"],
                            cwd=ROOT, env=os.environ | {"YTDL_DATA_DIR": str(data_dir)} | env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc, port


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def request(port, path, data=None, headers=None, method=None, timeout=5):
    """(status, body, headers) without following redirects."""
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def wait_up(port):
    for _ in range(50):
        try:
            return request(port, "/")
        except OSError:
            time.sleep(0.2)
    raise AssertionError("server did not start")


@pytest.fixture(scope="module")
def real_server(tmp):
    proc, port = start_server(tmp / "srv")
    try:
        status, page, _ = wait_up(port)
        token = re.search(r'name="api-token" content="([^"]+)"', page)
        yield SimpleNamespace(port=port, status=status, token=token, proc=proc)
    finally:
        proc.terminate()
        proc.wait(5)


@pytest.mark.server
def test_api_token_and_host(real_server):
    port, token = real_server.port, real_server.token
    get = lambda path, headers=None: request(port, path, headers=headers)[:2]
    assert real_server.status == 200 and token, "api: page carries a session token"
    assert get("/api/overview")[0] == 403, "api: request without token refused"
    assert get("/api/overview", {"X-Token": "nope"})[0] == 403, "api: request with wrong token refused"
    assert get("/api/overview", {"X-Token": token.group(1)})[0] == 200, "api: request with token works"
    assert get("/", {"Host": "evil.example:%d" % port})[0] == 403, "api: foreign Host refused (DNS rebinding)"
    assert get("/api/overview", {"Host": "evil.example", "X-Token": token.group(1)})[0] == 403, \
        "api: foreign Host refused even with token"
    assert get("/", {"Host": f"localhost:{port}"})[0] == 200, "api: localhost name allowed"


@pytest.mark.server
def test_api_stream(real_server):
    port, tok = real_server.port, real_server.token.group(1)
    # live stream: token only in the query string, only on this path
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/stream?token={tok}")
    with urllib.request.urlopen(req, timeout=10) as r:
        first = r.readline().decode()
        ctype = r.headers.get("content-type", "")
    assert first.startswith("data: ") and "text/event-stream" in ctype \
        and {"overview", "activity"} <= set(json.loads(first[6:])), \
        f"stream: sends the overview and the dashboard: {first[:120]}"
    assert request(port, "/api/stream")[0] == 403, "stream: refused without its token"
    assert request(port, f"/api/overview?token={tok}")[0] == 403, "stream: query token not accepted on other paths"


@pytest.mark.server
def test_api_routes(real_server):
    port = real_server.port
    H = {"X-Token": real_server.token.group(1)}

    def get(path):
        status, body, _ = request(port, path, headers=H)
        return status, body

    def post(path, data=None):
        status, body, _ = request(port, path, json.dumps(data or {}).encode(),
                                  H | {"Content-Type": "application/json"}, "POST")
        return status, body

    status, body = get("/api/history")
    assert status == 200 and "rows" in json.loads(body), f"history: answers: {body[:200]}"
    status, body = get("/api/stats?days=7")
    assert status == 200 and json.loads(body)["period"] == 7, f"stats: answers: {body[:200]}"
    status, body = get("/api/activity?q=nothing")
    assert status == 200 and {"eta", "disks", "speed_history", "left_bytes"} <= set(json.loads(body)["stats"]), \
        f"activity: has queue ETA, disks and speed history: {body[:300]}"
    assert post("/api/jobs/nope/items/x/reveal")[0] == 404, "file actions: unknown video refused"
    assert post("/api/queue/move", {"job": "nope", "id": "x", "to": "top"})[0] == 404, "queue move: unknown video refused"
    assert post("/api/queue/explode")[0] == 404, "queue: unknown action refused"
    assert post("/api/queue/pause-all")[0] == 200 and post("/api/queue/resume-all")[0] == 200, "queue: pause all works"
    assert post("/api/open-many", {"urls": ["nothing here"]})[0] == 400, "open-many: no links refused"
    status, body = get("/api/ytdlp")
    assert status == 200 and json.loads(body)["can_restart"] is True, f"ytdlp: version and restart flag: {body[:200]}"


@pytest.mark.server
def test_second_copy_exits(real_server, tmp):
    # a second copy on the same port exits before starting any downloads (no library created)
    r = subprocess.run([sys.executable, str(ROOT / "server.py"), "--port", str(real_server.port), "--no-browser"],
                       cwd=ROOT, env=os.environ | {"YTDL_DATA_DIR": str(tmp / "srv2")},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "already in use" in r.stderr and not (tmp / "srv2" / "library.db").exists(), \
        f"startup: port in use -> exits without starting downloads: {r.stderr[-200:]}"


@pytest.mark.server
def test_password(tmp):
    # meant for network mode; applies whenever one is set
    proc, port = start_server(tmp / "psrv", YTDL_PASSWORD="s3cret")
    try:
        status, body, hdrs = wait_up(port)
        assert status == 303 and hdrs.get("location") == "/login", f"password: page redirects to the login: {status}"
        assert "api-token" not in body, "password: no token handed out before login"
        assert request(port, "/api/overview")[0] == 401, "password: API refused without login"
        status, body, hdrs = request(port, "/login", b"password=s3cret")
        cookie = (hdrs.get("set-cookie") or "").split(";")[0]
        assert status == 303 and cookie.startswith("ytdl_session=") \
            and "httponly" in hdrs.get("set-cookie", "").lower(), \
            f"password: right password logs in: {(status, hdrs.get('set-cookie'))}"
        status, body, _ = request(port, "/", headers={"Cookie": cookie})
        assert status == 200 and 'name="api-token" content="' in body, "password: logged-in browser gets the page"
        codes = [request(port, "/login", b"password=wrong")[0] for _ in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429, f"password: wrong ones refused, then rate-limited: {codes}"
    finally:
        proc.terminate()
        proc.wait(5)
