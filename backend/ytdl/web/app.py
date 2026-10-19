"""The FastAPI app: the page (with the token filled in), static files, the API routers and the guard."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..config import ROOT
from . import auth, engine
from .routes import jobs, library, live, queue, settings

STATIC = ROOT / "frontend" / "static"         # the page (until the Next.js build replaces it)
OUT = ROOT / "frontend" / "out"               # the Next.js page, built by `npm run build` (committed)
NEXT_BASE = "/next"                           # where the Next.js page is served while both pages exist
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
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('<meta name="api-token" content="">', f'<meta name="api-token" content="{auth.TOKEN}">')
    for name in ("js/main.js", "style.css"):          # a new file name after every change: no stale cached copy
        if (STATIC / name).exists():
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={int((STATIC / name).stat().st_mtime)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def built_page(path):
    """The Next.js page with this process's token added to <head> (the page doesn't render that tag itself)."""
    html = HEAD.sub(lambda m: f'{m.group(0)}<meta name="api-token" content="{auth.TOKEN}"/>', path.read_text(encoding="utf-8"), count=1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get(NEXT_BASE)
def next_redirect():
    return RedirectResponse(f"{NEXT_BASE}/", status_code=307)


@app.get(f"{NEXT_BASE}/")
def next_index():
    if not (OUT / "index.html").exists():
        return HTMLResponse("The new page isn't built yet: run npm run build.", status_code=404)
    return built_page(OUT / "index.html")


app.middleware("http")(auth.guard)
app.include_router(auth.router)
for routes in (live, queue, settings, library, jobs):
    app.include_router(routes.router)

app.mount("/static", StaticFiles(directory=STATIC), name="static")
if OUT.is_dir():
    app.mount(NEXT_BASE, StaticFiles(directory=OUT), name="next")
