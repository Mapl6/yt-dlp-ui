"""The FastAPI app: the page (with the token filled in), static files, the API routers and the guard."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..config import ROOT
from . import auth, engine
from .routes import jobs, library, live, queue, settings

STATIC = ROOT / "static"


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


app.middleware("http")(auth.guard)
app.include_router(auth.router)
for routes in (live, queue, settings, library, jobs):
    app.include_router(routes.router)

app.mount("/static", StaticFiles(directory=STATIC), name="static")
