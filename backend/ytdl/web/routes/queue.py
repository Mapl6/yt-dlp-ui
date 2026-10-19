"""Queue-wide controls: reorder, pause or resume everything, clear a pause, retry all failed, test the connection."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import engine

router = APIRouter()



class VideoRef(BaseModel):
    job: str
    id: str


class MoveRequest(VideoRef):
    to: str | None = None            # "top" | "bottom"; None = drop before `before` (or at the end)
    before: VideoRef | None = None


@router.post("/api/queue/move")
def queue_move(req: MoveRequest):
    try:
        engine.manager.move(req.job, req.id, req.to, (req.before.job, req.before.id) if req.before else None)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/api/queue/{action}")
def queue_action(action: str):
    if action == "clear-hold":
        engine.manager.clear_hold()
    elif action == "retry-failed":
        return {"queued": engine.manager.retry_failed()}
    elif action in ("pause-all", "resume-all"):
        engine.manager.pause_all(action == "pause-all")
    else:
        raise HTTPException(404, "Unknown action")
    return {"ok": True}


@router.post("/api/net/check")
def net_check():
    engine.net.check_now()
    return {"ok": True}
