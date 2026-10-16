"""Local web server for the YouTube downloader. Run: ./run.sh  →  http://127.0.0.1:8765"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from manager import Manager, _clean_error
from netcheck import NetMonitor

STATIC = Path(__file__).parent / "static"

manager = Manager()
net = NetMonitor(lambda: manager.settings,
                 lambda: sum(manager.summary(j)["speed"] for j in list(manager.jobs.values())))


@asynccontextmanager
async def lifespan(_app):
    yield
    manager.save_state()


app = FastAPI(title="YT Downloader", lifespan=lifespan)


class OpenRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    ids: list[str] | None = None     # None = every video not downloaded yet
    options: dict = {}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview():
    return manager.overview() | {"net": net.snapshot()}


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
    except OSError as exc:
        raise HTTPException(400, f"Can't use that folder: {exc}")


@app.post("/api/jobs/{job_id}/refresh")
def refresh_job(job_id: str):
    try:
        return {"added": manager.refresh(job_id)}
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except Exception as exc:
        raise HTTPException(400, _clean_error(exc))


@app.post("/api/jobs/{job_id}/items/{item_id}/{action}")
def item_action(job_id: str, item_id: str, action: str):
    try:
        manager.item_action(job_id, item_id, action)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))
    return {"ok": True}


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
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
