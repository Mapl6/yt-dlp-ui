"""Local web server for the YouTube downloader. Run: ./run.sh  →  http://127.0.0.1:8765"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from manager import AlreadyRunning, Manager, _clean_error
from netcheck import NetMonitor

STATIC = Path(__file__).parent / "static"

# Protection for the local API:
#  * Host check: a web page can't reach us through DNS rebinding (its requests carry its own host name).
#  * Session token: every /api call needs the token embedded in our page, which other sites can't read,
#    and the custom header forces a CORS preflight that other origins fail.
TOKEN = secrets.token_urlsafe(24)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
security = {"any_host": False}    # set when the user deliberately serves on the network (--host 0.0.0.0)
runtime = {"can_restart": False}  # True when started as "python server.py" (the process can re-exec itself)

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


class OpenManyRequest(BaseModel):
    urls: list[str]


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if not security["any_host"] and host not in LOOPBACK_HOSTS:
        return PlainTextResponse("Forbidden: unknown host", status_code=403)
    if request.url.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("x-token", ""), TOKEN):
        return JSONResponse({"detail": "Session expired — reload the page."}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('<meta name="api-token" content="">', f'<meta name="api-token" content="{TOKEN}">')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/overview")
def overview():
    return manager.overview() | {"net": net.snapshot()}


@app.get("/api/activity")
def activity(job: str | None = None, q: str | None = None):
    return manager.activity(job, q)


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        security["any_host"] = True
        print(f"WARNING: serving on {args.host} — anyone who can reach this address can use the downloader.",
              file=sys.stderr)
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
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
