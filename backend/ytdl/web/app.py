"""The FastAPI app: the page (the Next.js build, with the token filled in), its assets, the API routers and the guard."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..config import ROOT
from . import auth, engine
from .routes import jobs, library, live, queue, settings

OUT = ROOT / "frontend" / "out"               # the page: the Next.js build (npm run build), committed
HEAD = re.compile(r"<head[^>]*>")


@asynccontextmanager
async def lifespan(_app):
    if engine.manager is None:          # started with "uvicorn ytdl.web.app:app" instead of ./run.sh
        engine.start_engine()
    yield
    engine.manager.save_state()


app = FastAPI(title="YT Downloader", lifespan=lifespan)


@app.get("/")
def index():
    """The page, with this process's token added to <head> (React doesn't render that tag, or it would add its
    own empty copy when it hydrates). Never cached: an update must never run an old page."""
    if not (OUT / "index.html").exists():
        return HTMLResponse("The page isn't built: run npm run build (see README.md).", status_code=500)
    html = (OUT / "index.html").read_text(encoding="utf-8")
    html = HEAD.sub(lambda m: f'{m.group(0)}<meta name="api-token" content="{auth.TOKEN}"/>', html, count=1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.middleware("http")(auth.guard)
app.include_router(auth.router)
for routes in (live, queue, settings, library, jobs):
    app.include_router(routes.router)

if (OUT / "_next").is_dir():
    app.mount("/_next", StaticFiles(directory=OUT / "_next"), name="next")   # scripts and styles (content-hashed names)
