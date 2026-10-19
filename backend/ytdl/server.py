"""Local web server for the YouTube downloader. Run: ./run.sh (python server.py)  →  http://127.0.0.1:8765"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .manager import ROOT, AlreadyRunning, Manager, _clean_error, check_cookies, cookie_sources
from .netcheck import NetMonitor

STATIC = ROOT / "static"

# Protection for the local API:
#  * Host check: a web page can't reach us through DNS rebinding (its requests carry its own host name).
#  * Session token: every /api call needs the token embedded in our page, which other sites can't read,
#    and the custom header forces a CORS preflight that other origins fail.
TOKEN = secrets.token_urlsafe(24)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
security = {"any_host": False,   # set when the user deliberately serves on the network (--host 0.0.0.0)
            "password": ""}      # --password / YTDL_PASSWORD: required to open the app (meant for network mode)
SESSION_COOKIE = "ytdl_session"
sessions: set[str] = set()        # logged-in browsers (in memory: a restart asks for the password again)
login_failures: dict[str, deque] = defaultdict(deque)   # client ip -> times of recent wrong passwords
LOGIN_LIMIT = 5                   # wrong passwords per minute per client
runtime = {"can_restart": False,  # True when started as "python server.py" (the process can re-exec itself)
           "server": None}        # the uvicorn.Server, so live streams can end when it shuts down

manager: Manager = None   # type: ignore[assignment]  — created by start_engine()
net: NetMonitor = None    # type: ignore[assignment]


def start_engine(port: int = 8765):
    """Start the download manager. Called only once we know the web port is ours, so a second copy of the
    app never starts downloading before it notices the first one."""
    global manager, net
    try:
        manager = Manager()
    except AlreadyRunning as exc:
        sys.exit(f"{exc}\nOpen http://127.0.0.1:{port} in your browser, or stop the other copy first.")
    net = NetMonitor(lambda: manager.settings, manager.total_speed, on_change=manager.network_changed)
    manager.net_level = lambda: net.state.get("level", "checking")
    manager.on_network_error = net.check_now   # re-test the connection as soon as a download hits trouble


@asynccontextmanager
async def lifespan(_app):
    if manager is None:          # started with "uvicorn server:app" instead of ./run.sh
        start_engine()
    yield
    manager.save_state()


app = FastAPI(title="YT Downloader", lifespan=lifespan)


class OpenRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    ids: list[str] | None = None     # None = every video not downloaded yet
    options: dict = {}


class VideoRef(BaseModel):
    job: str
    id: str


class MoveRequest(VideoRef):
    to: str | None = None            # "top" | "bottom"; None = drop before `before` (or at the end)
    before: VideoRef | None = None


class RemoveRequest(BaseModel):
    delete_files: bool = False


class SyncRequest(BaseModel):
    every_hours: float = 0
    auto_download: bool = False


class ImportRequest(BaseModel):
    data: dict
    settings: bool = False      # also replace the settings with the file's


class OpenManyRequest(BaseModel):
    urls: list[str]


class CookieFileRequest(BaseModel):
    text: str


class CookieTestRequest(BaseModel):
    browser: str
    profile: str = ""


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    path = request.url.path
    if not security["any_host"] and host not in LOOPBACK_HOSTS:
        return PlainTextResponse("Forbidden: unknown host", status_code=403)
    if security["password"] and path != "/login" and request.cookies.get(SESSION_COOKIE) not in sessions:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Password needed — reload the page."}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    if path.startswith("/api/"):
        # EventSource can't send headers, so the live stream (only) takes the token from the query string.
        sent = request.query_params.get("token", "") if path == "/api/stream" else request.headers.get("x-token", "")
        if not secrets.compare_digest(sent, TOKEN):
            return JSONResponse({"detail": "Session expired — reload the page."}, status_code=403)
    response = await call_next(request)
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"   # always revalidate: an update must never run old code
    return response


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>YT Downloader</title><style>
:root{--bg:#f6f6f4;--card:#fff;--text:#1d1d1b;--muted:#6b6b66;--border:#deded9;--accent:#d93a32}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--text:#ecece8;--muted:#a3a39c;--border:#363633}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px;width:100%;max-width:340px}
h1{font-size:18px;margin:0 0 4px}p{color:var(--muted);margin:0 0 18px}.err{color:var(--accent)}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border-radius:9px}
input{border:1px solid var(--border);background:var(--bg);color:var(--text);margin-bottom:12px}
button{border:0;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
</style></head><body><form method="post" action="/login"><h1>YT Downloader</h1><p class="{cls}">{msg}</p>
<input type="password" name="password" placeholder="Password" autofocus required autocomplete="current-password">
<button>Open</button></form></body></html>"""


def login_page(msg: str = "Enter the password to open the downloader.", error: bool = False, status: int = 200):
    page = LOGIN_PAGE.replace("{cls}", "err" if error else "").replace("{msg}", html.escape(msg))
    return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/login")
def login_form():
    return login_page()


@app.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    recent = login_failures[ip]
    while recent and time.time() - recent[0] > 60:
        recent.popleft()
    if len(recent) >= LOGIN_LIMIT:
        return login_page("Too many wrong passwords — wait a minute and try again.", True, 429)
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    given = (form.get("password") or [""])[0]
    if not security["password"] or not secrets.compare_digest(given.encode(), security["password"].encode()):
        recent.append(time.time())
        return login_page("Wrong password.", True, 401)
    session = secrets.token_urlsafe(32)
    sessions.add(session)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, session, max_age=30 * 86400, httponly=True, samesite="strict")
    return response


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('<meta name="api-token" content="">', f'<meta name="api-token" content="{TOKEN}">')
    for name in ("js/main.js", "style.css"):          # a new file name after every change: no stale cached copy
        if (STATIC / name).exists():
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={int((STATIC / name).stat().st_mtime)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/overview")
def overview():
    return manager.overview() | {"net": net.snapshot()}


@app.get("/api/activity")
def activity(job: str | None = None, q: str | None = None):
    return manager.activity(job, q)


@app.get("/api/stream")
async def stream(request: Request, job: str | None = None, q: str | None = None):
    """Server-Sent Events: the overview and the dashboard, pushed when something changes (at most twice a
    second), and at least every 2 s so countdowns and the connection panel stay current."""
    def snapshot():
        return {"overview": overview(), "activity": manager.activity(job, q)}

    async def events():
        last_version, last_sent = None, 0.0
        server = runtime["server"]
        while not (server and server.should_exit) and not await request.is_disconnected():
            now = time.monotonic()
            if manager.version != last_version or now - last_sent >= 2:
                last_version = manager.version
                data = await run_in_threadpool(snapshot)
                yield f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
                last_sent = now
            await asyncio.sleep(0.5)
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/history")
def history(offset: int = 0, limit: int = 30, since_id: int = 0):
    return manager.history(max(0, offset), max(1, min(limit, 200)), max(0, since_id))


@app.post("/api/history/clear")
def history_clear():
    return {"cleared": manager.clear_history()}


@app.get("/api/stats")
def stats(days: int = 30):
    return manager.stats(days)


@app.post("/api/queue/move")
def queue_move(req: MoveRequest):
    try:
        manager.move(req.job, req.id, req.to, (req.before.job, req.before.id) if req.before else None)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/queue/{action}")
def queue_action(action: str):
    if action == "clear-hold":
        manager.clear_hold()
    elif action == "retry-failed":
        return {"queued": manager.retry_failed()}
    elif action in ("pause-all", "resume-all"):
        manager.pause_all(action == "pause-all")
    else:
        raise HTTPException(404, "Unknown action")
    return {"ok": True}


@app.post("/api/net/check")
def net_check():
    net.check_now()
    return {"ok": True}


@app.put("/api/settings")
def settings(patch: dict):
    result = manager.update_settings(patch)
    net.check_now()   # a new proxy changes reachability right away
    return result


@app.get("/api/cookies/sources")
def cookies_sources():
    """Browsers on this computer that cookies can be read from, with their profiles."""
    return {"browsers": cookie_sources()}


@app.post("/api/cookies/test")
def cookies_test(req: CookieTestRequest):
    """Read the chosen browser's cookies now (macOS may ask for the keychain password)."""
    return check_cookies(req.browser.strip(), req.profile.strip())


@app.post("/api/cookies/file")
def cookies_import(req: CookieFileRequest):
    """Save a cookies.txt (Netscape format) exported from a browser; downloads use it instead of the browser."""
    try:
        return manager.import_cookies(req.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/cookies/file")
def cookies_remove():
    return {"removed": manager.remove_cookies()}


@app.get("/api/export")
def export_all():
    """Everything in one JSON file: settings, every playlist with its videos, and the download history."""
    name = time.strftime("yt-downloader-export-%Y%m%d-%H%M%S.json")
    return JSONResponse(manager.export_data(), headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/import")
def import_all(req: ImportRequest):
    """Merge an export file into the library (adds what is missing, never deletes)."""
    try:
        return manager.import_data(req.data, req.settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/open")
def open_url(req: OpenRequest):
    """Fetch a link and save it (with all of its videos) in the library."""
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Paste a video or playlist link.")
    try:
        return manager.open_url(url)
    except Exception as exc:
        raise HTTPException(400, _clean_error(exc))


@app.post("/api/open-many")
def open_many(req: OpenManyRequest):
    """Add many links (pasted, or from a .txt file) in the background."""
    try:
        return manager.open_many(req.urls)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/ytdlp")
def ytdlp():
    return manager.ytdlp_info() | {"can_restart": runtime["can_restart"]}


@app.post("/api/ytdlp/update")
def ytdlp_update():
    return manager.update_ytdlp()


@app.post("/api/restart")
def restart():
    """Restart the app (to load an updated yt-dlp). Interrupted downloads resume from their .part files."""
    if not runtime["can_restart"]:
        raise HTTPException(400, "Quit and open the app again to finish the update.")

    def later():
        time.sleep(0.5)                       # let this response reach the page first
        manager.save_state()
        argv = [os.path.abspath(sys.argv[0]), *sys.argv[1:]]
        if "--no-browser" not in argv:
            argv.append("--no-browser")       # the open page reloads itself
        os.environ["YTDL_RESTARTED"] = "1"
        os.execv(sys.executable, [sys.executable, *argv])   # fds are close-on-exec: lock and port are released
    threading.Thread(target=later, daemon=True).start()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/items")
def job_items(job_id: str, since: int = 0):
    try:
        return manager.items_since(job_id, since)
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@app.post("/api/jobs/{job_id}/download")
def download(job_id: str, req: DownloadRequest):
    try:
        return {"queued": manager.queue(job_id, req.ids, dict(req.options))}
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(400, f"Can't use that folder: {exc.strerror or exc}")


@app.put("/api/jobs/{job_id}/options")
def job_options(job_id: str, req: DownloadRequest):
    """The playlist's Save button: store its options without queueing anything."""
    try:
        return manager.set_options(job_id, dict(req.options))
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except OSError as exc:
        raise HTTPException(400, f"Can't use that folder: {exc.strerror or exc}")


@app.post("/api/jobs/{job_id}/estimate")
def estimate(job_id: str, req: DownloadRequest):
    try:
        return manager.estimate(job_id, req.ids, dict(req.options))
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/jobs/{job_id}/sync")
def job_sync(job_id: str, req: SyncRequest):
    try:
        return manager.set_sync(job_id, req.every_hours, req.auto_download)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/jobs/{job_id}/refresh")
def refresh_job(job_id: str):
    try:
        return {"added": manager.refresh(job_id)}
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except Exception as exc:
        raise HTTPException(400, _clean_error(exc))


@app.get("/api/jobs/{job_id}/items/{item_id}/log")
def item_log(job_id: str, item_id: str):
    try:
        return manager.item_log(job_id, item_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))


@app.post("/api/jobs/{job_id}/items/{item_id}/{action}")
def item_action(job_id: str, item_id: str, action: str):
    try:
        manager.item_action(job_id, item_id, action)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/check")
def check_files(job_id: str):
    try:
        return {"started": manager.start_check(job_id)}
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@app.post("/api/jobs/{job_id}/check/stop")
def stop_check(job_id: str):
    try:
        manager.stop_check(job_id)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/cleanup")
def cleanup_preview(job_id: str):
    try:
        return manager.cleanup(job_id, dry_run=True)
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@app.post("/api/jobs/{job_id}/cleanup")
def cleanup(job_id: str):
    try:
        return manager.cleanup(job_id, dry_run=False)
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@app.post("/api/jobs/{job_id}/remove")
def remove_job(job_id: str, req: RemoveRequest | None = None):
    try:
        manager.remove(job_id, bool(req and req.delete_files))
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/m3u")
def export_m3u(job_id: str):
    try:
        return manager.export_m3u(job_id)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str):
    try:
        manager.action(job_id, action)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    """`python server.py`: check the port is free, start the engine, serve the page."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--password", default=os.environ.get("YTDL_PASSWORD", ""),
                        help="require this password to open the app (recommended with --host 0.0.0.0)")
    args = parser.parse_args()
    security["password"] = args.password
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        security["any_host"] = True
        if not args.password:
            print(f"WARNING: serving on {args.host} without a password — anyone who can reach this address can "
                  "use the downloader. Start with --password (or set YTDL_PASSWORD) to protect it.", file=sys.stderr)
    # After a restart the old socket can take a moment to go away: retry for a few seconds then.
    tries = 40 if os.environ.pop("YTDL_RESTARTED", None) else 1
    for attempt in range(tries):
        with socket.socket(socket.AF_INET6 if ":" in args.host else socket.AF_INET) as probe:
            if tries > 1:
                # only right after our own restart: on macOS this flag would also let the check pass while
                # another copy listens on 0.0.0.0, so a normal start never sets it
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((args.host, args.port))
                break
            except OSError:
                if attempt == tries - 1:
                    sys.exit(f"Port {args.port} is already in use — is YT Downloader already running? "
                             f"Open http://127.0.0.1:{args.port} or stop the other copy first.")
                time.sleep(0.25)
    runtime["can_restart"] = True
    start_engine(args.port)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    # The live stream never ends by itself: don't let it hold up Ctrl+C for more than a moment.
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                                           timeout_graceful_shutdown=2))
    runtime["server"] = server
    server.run()
