"""What the page polls or streams: overview, dashboard, live stream, history and stats."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from .. import engine

router = APIRouter()



@router.get("/api/overview")
def overview():
    return engine.manager.overview() | {"net": engine.net.snapshot()}


@router.get("/api/activity")
def activity(job: str | None = None, q: str | None = None):
    return engine.manager.activity(job, q)


@router.get("/api/stream")
async def stream(request: Request, job: str | None = None, q: str | None = None):
    """Server-Sent Events: the overview and the dashboard, pushed when something changes (at most twice a
    second), and at least every 2 s so countdowns and the connection panel stay current."""
    def snapshot():
        return {"overview": overview(), "activity": engine.manager.activity(job, q)}

    async def events():
        last_version, last_sent = None, 0.0
        server = engine.runtime["server"]
        while not (server and server.should_exit) and not await request.is_disconnected():
            now = time.monotonic()
            if engine.manager.version != last_version or now - last_sent >= 2:
                last_version = engine.manager.version
                data = await run_in_threadpool(snapshot)
                yield f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
                last_sent = now
            await asyncio.sleep(0.5)
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/api/history")
def history(offset: int = 0, limit: int = 30, since_id: int = 0):
    return engine.manager.history(max(0, offset), max(1, min(limit, 200)), max(0, since_id))


@router.post("/api/history/clear")
def history_clear():
    return {"cleared": engine.manager.clear_history()}


@router.get("/api/stats")
def stats(days: int = 30):
    return engine.manager.stats(days)
