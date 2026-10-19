"""Every route of server.py, in-process (FastAPI TestClient): the token and Host guard on each one, the password
login and its rate limit, error responses, a working call per route, and the order of the job routes."""
import json
import re
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

from ytdl import extract, files, ytdlp
from ytdl import manager as M
from ytdl.web import app as webapp
from ytdl.web import auth, engine
from helpers import ROOT, items_of, make_job, wait


class NetStub:
    """NetMonitor without probes: reports a fixed state and counts check_now() calls."""

    def __init__(self):
        self.checks = 0
        self.state = {"level": "good"}

    def snapshot(self):
        return {"level": "good", "stub": True}

    def check_now(self):
        self.checks += 1


@pytest.fixture(scope="module")
def netstub():
    return NetStub()


@pytest.fixture(scope="module")
def api(mgr, netstub):
    prev = engine.manager, engine.net, engine.runtime["server"]
    engine.manager, engine.net = mgr, netstub
    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        client.headers["X-Token"] = auth.TOKEN
        yield client
    engine.manager, engine.net, engine.runtime["server"] = prev


@pytest.fixture
def anon(api):
    """The same app without the token header."""
    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture(scope="module")
def job(mgr, out):
    return make_job(mgr, out, "api", ["v1", "v2", "v3"])


@pytest.fixture
def fake_inspect(monkeypatch):
    def inspect(url, settings):
        if "broken" in url:
            raise DownloadError("ERROR: [generic] broken: Unsupported URL: " + url)
        vid = url.rsplit("/", 1)[-1]
        return {"kind": "video", "source_id": vid, "title": f"Single {vid}", "thumbnail": None, "heights": [360],
                "items": [{"index": 1, "id": vid, "title": f"Single {vid}", "url": url, "duration": 30,
                           "extractor": "Test"}]}

    monkeypatch.setattr(extract, "inspect_url", inspect)


def all_routes(routes=None):
    """Every route in matching order; FastAPI keeps an included router as one entry (original_router)."""
    for r in webapp.app.routes if routes is None else routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from all_routes(inner.routes)
        else:
            yield r


def api_routes():
    """(method, path with sample parameters) for every /api route."""
    out = []
    for r in all_routes():
        if isinstance(r, APIRoute) and r.path.startswith("/api/"):
            path = r.path.replace("{job_id}", "nojob").replace("{item_id}", "noitem").replace("{action}", "noaction")
            for m in sorted(r.methods):
                out.append((m, path))
    return out


ROUTES = api_routes()


def test_every_route_is_listed():
    source = "".join(p.read_text() for p in (ROOT / "backend" / "ytdl" / "web").rglob("*.py"))
    declared = re.findall(r'^@(?:app|router)\.(?:get|post|put|delete)\("/api/', source, re.M)
    assert len(ROUTES) == len(declared) > 30, f"{len(ROUTES)} routes registered, {len(declared)} declared"


@pytest.mark.parametrize("method,path", ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
def test_token_required_on_every_route(anon, method, path):
    r = anon.request(method, path)
    assert r.status_code == 403 and "Session expired" in r.json()["detail"]


@pytest.mark.parametrize("method,path", ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
def test_wrong_token_refused_on_every_route(anon, method, path):
    assert anon.request(method, path, headers={"X-Token": "nope"}).status_code == 403


@pytest.mark.parametrize("method,path", ROUTES + [("GET", "/"), ("GET", "/login")],
                         ids=[f"{m} {p}" for m, p in ROUTES] + ["GET /", "GET /login"])
def test_foreign_host_refused_on_every_route(api, method, path):
    r = api.request(method, path, headers={"Host": "evil.example"})
    assert r.status_code == 403 and r.text == "Forbidden: unknown host"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost", "localhost:1234", "[::1]:8765"])
def test_loopback_hosts_allowed(api, host):
    assert api.get("/api/overview", headers={"Host": host}).status_code == 200


def test_any_host_when_serving_on_the_network(api, monkeypatch):
    monkeypatch.setitem(auth.security, "any_host", True)
    assert api.get("/api/overview", headers={"Host": "192.168.1.20:8765"}).status_code == 200


def test_route_order_specific_job_routes_first():
    """The catch-all POST /api/jobs/{job_id}/{action} would swallow any job route declared after it."""
    paths = [(r.path, r.methods) for r in all_routes() if isinstance(r, APIRoute)]
    catch_all = next(i for i, (p, m) in enumerate(paths) if p == "/api/jobs/{job_id}/{action}" and "POST" in m)
    later = [p for p, m in paths[catch_all + 1:] if p.startswith("/api/jobs/")]
    assert not later, f"job routes declared after the catch-all: {later}"
    item_catch = next(i for i, (p, m) in enumerate(paths) if p == "/api/jobs/{job_id}/items/{item_id}/{action}")
    log_route = next(i for i, (p, m) in enumerate(paths) if p == "/api/jobs/{job_id}/items/{item_id}/log")
    assert log_route < item_catch or "GET" not in paths[item_catch][1]


def test_index_injects_token_and_versions(api):
    r = api.get("/")
    assert r.status_code == 200 and f'name="api-token" content="{auth.TOKEN}"' in r.text
    assert r.headers["cache-control"] == "no-store"
    assert re.search(r'/static/js/main\.js\?v=\d+', r.text), "main.js gets a version so no stale copy runs"


def test_static_files_revalidate(api):
    r = api.get("/static/js/main.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


# ---------- password (network mode)

@pytest.fixture
def password(monkeypatch):
    monkeypatch.setitem(auth.security, "password", "s3cret")
    monkeypatch.setattr(auth, "sessions", set())
    auth.login_failures.clear()
    yield "s3cret"
    auth.login_failures.clear()


def test_password_redirects_page_and_refuses_api(api, password):
    r = api.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login" and "api-token" not in r.text
    r = api.get("/api/overview")
    assert r.status_code == 401 and "Password" in r.json()["detail"]
    assert api.get("/login").status_code == 200, "the login page itself needs no session"


def test_password_login_sets_session(api, password):
    r = api.post("/login", content="password=s3cret", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "ytdl_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    session = r.cookies["ytdl_session"]
    with TestClient(webapp.app, base_url="http://127.0.0.1", cookies={"ytdl_session": session}) as c:
        page = c.get("/")
        assert page.status_code == 200 and auth.TOKEN in page.text
        assert c.get("/api/overview", headers={"X-Token": auth.TOKEN}).status_code == 200
        assert c.get("/api/overview").status_code == 403, "a session still needs the token"
    with TestClient(webapp.app, base_url="http://127.0.0.1", cookies={"ytdl_session": "forged"}) as c:
        assert c.get("/", follow_redirects=False).status_code == 303, "an unknown session is refused"


def test_password_rate_limit(api, password, monkeypatch):
    codes = [api.post("/login", content="password=wrong").status_code for _ in range(6)]
    assert codes == [401] * 5 + [429], codes
    assert api.post("/login", content="password=s3cret").status_code == 429, "even the right one waits"
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 61)
    assert api.post("/login", content="password=s3cret", follow_redirects=False).status_code == 303, \
        "allowed again after a minute"


def test_login_without_password_set_refused(api):
    assert api.post("/login", content="password=").status_code == 401


# ---------- live stream

def test_stream_sends_snapshot_and_ends_on_shutdown(api):
    stopping = SimpleNamespace(should_exit=False)
    engine.runtime["server"] = stopping
    try:
        with api.stream("GET", f"/api/stream?token={auth.TOKEN}", headers={"X-Token": ""}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            lines = r.iter_lines()
            first = next(lines)
            data = json.loads(first[len("data: "):])
            assert {"overview", "activity"} <= set(data) and data["overview"]["net"]["stub"]
            stopping.should_exit = True                 # Ctrl+C: the stream ends instead of holding it up
            rest = list(lines)
            assert len([x for x in rest if x.startswith("data: ")]) <= 1
    finally:
        engine.runtime["server"] = None


def test_stream_token_only_in_query_on_stream(api, anon):
    assert anon.get("/api/stream").status_code == 403
    assert anon.get(f"/api/overview?token={auth.TOKEN}").status_code == 403


# ---------- each route works

def test_overview_includes_network(api):
    r = api.get("/api/overview")
    assert r.status_code == 200 and r.json()["net"] == {"level": "good", "stub": True}
    assert {"jobs", "settings", "waiting"} <= set(r.json())


def test_activity_and_history(api, mgr):
    assert {"queue", "recent", "stats"} <= set(api.get("/api/activity").json())
    mgr.store.add_events([{"time": time.time(), "job_id": "x", "item_id": f"h{i}", "status": "done"} for i in range(5)])
    rows = api.get("/api/history?limit=2").json()["rows"]
    assert len(rows) == 2
    assert api.get("/api/history?limit=0&offset=-5").status_code == 200, "bad paging values are clamped"
    assert api.get("/api/history?limit=abc").status_code == 422
    assert api.post("/api/history/clear").json()["cleared"] >= 5
    assert api.get("/api/history").json()["rows"] == []
    assert api.get("/api/stats?days=7").json()["period"] == 7


def test_queue_actions(api, mgr, job):
    assert api.post("/api/queue/pause-all").json() == {"ok": True} and all(j["paused"] for j in mgr.jobs.values())
    assert api.post("/api/queue/resume-all").json() == {"ok": True} and not any(j["paused"] for j in mgr.jobs.values())
    mgr._hold_queue("test pause", 300, "other")
    assert api.post("/api/queue/clear-hold").status_code == 200 and mgr.hold is None
    assert "queued" in api.post("/api/queue/retry-failed").json()
    assert api.post("/api/queue/explode").status_code == 404


def test_queue_move(api, mgr, job):
    api.post(f"/api/jobs/{job['id']}/download", json={"ids": ["v1", "v2", "v3"], "options": {}})
    try:
        r = api.post("/api/queue/move", json={"job": job["id"], "id": "v3", "to": "top"})
        assert r.json() == {"ok": True}
        assert [x["id"] for x in mgr.activity(job_id=job["id"])["queue"]][0] == "v3"
        r = api.post("/api/queue/move", json={"job": job["id"], "id": "v1", "before": {"job": job["id"], "id": "v3"}})
        assert r.status_code == 200
        assert api.post("/api/queue/move", json={"job": "nope", "id": "x", "to": "top"}).status_code == 404
        assert api.post("/api/queue/move", json={"job": job["id"]}).status_code == 422
    finally:
        mgr.action(job["id"], "stop")
    r = api.post("/api/queue/move", json={"job": job["id"], "id": "v1", "to": "top"})
    assert r.status_code == 400 and "queue" in r.json()["detail"]


def test_net_check(api, netstub):
    before = netstub.checks
    assert api.post("/api/net/check").json() == {"ok": True} and netstub.checks == before + 1


def test_settings_route(api, mgr, netstub):
    before = netstub.checks
    r = api.put("/api/settings", json={"concurrency": 99, "sleep_between": "1.5"})
    try:
        assert r.status_code == 200 and r.json()["concurrency"] == 8 and r.json()["sleep_between"] == 1.5
        assert netstub.checks == before + 1, "a settings change re-tests the connection"
        assert api.put("/api/settings", content="[1,2]", headers={"Content-Type": "application/json"}).status_code == 422
    finally:
        mgr.update_settings({"concurrency": 3, "sleep_between": 0})


def test_cookie_routes(api, mgr, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))            # an empty home: no real browser folders are read
    assert api.get("/api/cookies/sources").json() == {"browsers": []}
    r = api.post("/api/cookies/test", json={"browser": " nosuch ", "profile": ""}).json()
    assert r["ok"] is False and "nosuch" in r["error"] and " nosuch " not in r["error"]
    assert api.post("/api/cookies/test", json={}).status_code == 422
    assert api.post("/api/cookies/file", json={"text": "not cookies"}).status_code == 400
    good = ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc\n"
    r = api.post("/api/cookies/file", json={"text": good})
    assert r.status_code == 200 and r.json()["youtube"] == 1
    assert api.delete("/api/cookies/file").json() == {"removed": True}
    assert api.delete("/api/cookies/file").json() == {"removed": False}


def test_export_and_import_routes(api, mgr, job):
    r = api.get("/api/export")
    assert r.status_code == 200 and 'attachment; filename="yt-downloader-export-' in r.headers["content-disposition"]
    data = r.json()
    assert data["app"] == M.EXPORT_APP and any(j["id"] == job["id"] for j in data["jobs"])
    r = api.post("/api/import", json={"data": data})
    assert r.status_code == 200 and r.json()["playlists_added"] == 0
    assert api.post("/api/import", json={"data": {"app": "other"}}).status_code == 400
    assert api.post("/api/import", json={}).status_code == 422


def test_open_routes(api, mgr, fake_inspect):
    assert api.post("/api/open", json={"url": "   "}).status_code == 400
    r = api.post("/api/open", json={"url": "https://x.test/broken"})
    assert r.status_code == 400 and r.json()["detail"].startswith("Unsupported URL"), "the error is cleaned up"
    r = api.post("/api/open", json={"url": "https://x.test/single1"})
    assert r.status_code == 200 and r.json()["title"] == "Single single1"
    assert api.post("/api/open", json={}).status_code == 422
    assert api.post("/api/open-many", json={"urls": ["no links here"]}).status_code == 400
    r = api.post("/api/open-many", json={"urls": ["https://x.test/many1"]})
    assert r.status_code == 200 and r.json()["total"] == 1
    assert wait(lambda: not mgr.importing["running"], 30)


def test_ytdlp_routes(api, mgr, monkeypatch):
    r = api.get("/api/ytdlp").json()
    assert r["version"] == ytdlp.yt_dlp.version.__version__ and r["can_restart"] is False
    monkeypatch.setattr(mgr, "_update_cmd", lambda: [sys.executable, "-c", "print('updated')"], raising=False)
    monkeypatch.setattr(mgr, "_installed_ytdlp", lambda: "2099.01.01", raising=False)
    assert api.post("/api/ytdlp/update").status_code == 200
    assert wait(lambda: not mgr.updating["running"], 30) and mgr.updating["ok"]


def test_restart_refused_when_not_started_as_script(api):
    r = api.post("/api/restart")
    assert r.status_code == 400 and "Quit and open the app again" in r.json()["detail"]


def test_job_routes_unknown_playlist(api):
    for method, path, body in [
        ("GET", "/api/jobs/nope/items", None), ("POST", "/api/jobs/nope/download", {}),
        ("PUT", "/api/jobs/nope/options", {}), ("POST", "/api/jobs/nope/estimate", {}),
        ("PUT", "/api/jobs/nope/sync", {}), ("POST", "/api/jobs/nope/refresh", None),
        ("GET", "/api/jobs/nope/items/x/log", None), ("POST", "/api/jobs/nope/items/x/hold", None),
        ("POST", "/api/jobs/nope/check", None), ("POST", "/api/jobs/nope/check/stop", None),
        ("GET", "/api/jobs/nope/cleanup", None), ("POST", "/api/jobs/nope/cleanup", None),
        ("POST", "/api/jobs/nope/remove", None), ("POST", "/api/jobs/nope/m3u", None),
        ("POST", "/api/jobs/nope/pause", None),
    ]:
        r = api.request(method, path, json=body)
        assert r.status_code == 404, f"{method} {path}: {r.status_code} {r.text}"


def test_job_items_since(api, mgr, job):
    r = api.get(f"/api/jobs/{job['id']}/items").json()
    assert {it["id"] for it in r["items"]} == {"v1", "v2", "v3"}
    rev = r["rev"]
    assert api.get(f"/api/jobs/{job['id']}/items?since={rev}").json()["items"] == [], "nothing changed since"
    mgr._set(job, items_of(job)["v2"], title="Changed")
    assert [it["id"] for it in api.get(f"/api/jobs/{job['id']}/items?since={rev}").json()["items"]] == ["v2"]


def test_download_options_estimate_sync(api, mgr, job, out):
    jid = job["id"]
    r = api.post(f"/api/jobs/{jid}/download", json={"ids": ["v1"], "options": {"quality": "720"}})
    try:
        assert r.json() == {"queued": 1} and items_of(job)["v1"]["status"] == "queued"
    finally:
        mgr.action(jid, "stop")
    if sys.platform == "darwin":
        r = api.post(f"/api/jobs/{jid}/download", json={"options": {"output_dir": "/Volumes/NoSuchDrive_api/x"}})
        assert r.status_code == 400 and "Can't use that folder" in r.json()["detail"]
    r = api.put(f"/api/jobs/{jid}/options", json={"options": {"quality": "480", "output_dir": str(out)}})
    assert r.status_code == 200 and job["options"]["quality"] == "480"
    assert not any(it["status"] == "queued" for it in job["items"]), "Save doesn't queue"
    r = api.post(f"/api/jobs/{jid}/estimate", json={"options": {"quality": "1080"}})
    assert r.status_code == 200 and r.json()["count"] == 3
    r = api.post(f"/api/jobs/{jid}/estimate", json={"options": {"section": {"start": "abc"}}})
    assert r.status_code == 400
    assert api.put(f"/api/jobs/{jid}/sync", json={"every_hours": 5}).status_code == 400
    r = api.put(f"/api/jobs/{jid}/sync", json={"every_hours": 24, "auto_download": True})
    assert r.status_code == 200 and job["sync"]["every"] == 24 and job["sync"]["auto"] is True
    api.put(f"/api/jobs/{jid}/sync", json={"every_hours": 0})


def test_refresh_route(api, mgr, job, monkeypatch):
    def inspect(url, settings):
        return {"kind": "playlist", "source_id": "api", "title": "List api", "thumbnail": None, "heights": [360],
                "items": [{"index": i + 1, "id": v, "title": f"Video {v}", "url": f"test://{v}", "duration": 30,
                           "extractor": "Test"} for i, v in enumerate(["v1", "v2", "v3", "v4"])]}

    monkeypatch.setattr(extract, "inspect_url", inspect)
    assert api.post(f"/api/jobs/{job['id']}/refresh").json() == {"added": 1}
    monkeypatch.setattr(extract, "inspect_url", lambda url, s: (_ for _ in ()).throw(DownloadError("ERROR: offline")))
    r = api.post(f"/api/jobs/{job['id']}/refresh")
    assert r.status_code == 400 and r.json()["detail"] == "offline"


def test_item_routes(api, mgr, job):
    jid = job["id"]
    assert "lines" in api.get(f"/api/jobs/{jid}/items/v1/log").json()
    assert api.get(f"/api/jobs/{jid}/items/nope/log").status_code == 404
    assert api.post(f"/api/jobs/{jid}/items/nope/hold").status_code == 404
    r = api.post(f"/api/jobs/{jid}/items/v1/explode")
    assert r.status_code == 400 and "Unknown action" in r.json()["detail"]
    r = api.post(f"/api/jobs/{jid}/items/v1/play")
    assert r.status_code == 400, "no file to play"
    api.post(f"/api/jobs/{jid}/download", json={"ids": ["v1"], "options": {}})
    try:
        assert api.post(f"/api/jobs/{jid}/items/v1/hold").json() == {"ok": True} and items_of(job)["v1"]["held"]
        assert api.post(f"/api/jobs/{jid}/items/v1/unhold").status_code == 200 and not items_of(job)["v1"]["held"]
    finally:
        mgr.action(jid, "stop")


def test_check_cleanup_m3u_remove(api, mgr, out):
    j = make_job(mgr, out, "api2", ["k1"])
    jid = j["id"]
    assert api.post(f"/api/jobs/{jid}/check").json()["started"] in (True, False)
    assert wait(lambda: not (j.get("check") or {}).get("running"), 30)
    assert api.post(f"/api/jobs/{jid}/check/stop").json() == {"ok": True}
    (out / "Video k1 [k1].f137.mp4.part").write_bytes(b"x" * 10)
    pre = api.get(f"/api/jobs/{jid}/cleanup").json()
    assert pre["names"] == ["Video k1 [k1].f137.mp4.part"] and (out / "Video k1 [k1].f137.mp4.part").exists(), \
        "GET only previews"
    assert api.post(f"/api/jobs/{jid}/cleanup").json()["files"] == 1
    assert not (out / "Video k1 [k1].f137.mp4.part").exists()
    r = api.post(f"/api/jobs/{jid}/m3u")
    assert r.status_code == 400 and "No downloaded videos" in r.json()["detail"]
    assert api.post(f"/api/jobs/{jid}/remove").json() == {"ok": True} and jid not in mgr.jobs


def test_job_actions(api, mgr, out, monkeypatch):
    j = make_job(mgr, out, "api3", ["p1"])
    jid = j["id"]
    assert api.post(f"/api/jobs/{jid}/pause").json() == {"ok": True} and j["paused"]
    assert api.post(f"/api/jobs/{jid}/resume").json() == {"ok": True} and not j["paused"]
    opened = []
    monkeypatch.setattr(files, "_open_path", lambda path, reveal=False: opened.append(path))
    assert api.post(f"/api/jobs/{jid}/open").status_code == 200 and opened == [str(out)]
    r = api.post(f"/api/jobs/{jid}/explode")
    assert r.status_code == 400 and "Unknown action" in r.json()["detail"]
    assert api.post(f"/api/jobs/{jid}/remove", json={"delete_files": False}).status_code == 200
