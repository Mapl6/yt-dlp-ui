"""One playlist and its videos. Specific routes come before the catch-all POST /api/jobs/{job_id}/{action}
(a test checks the order)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...errors import _clean_error
from .. import engine

router = APIRouter()



class DownloadRequest(BaseModel):
    ids: list[str] | None = None     # None = every video not downloaded yet
    options: dict = {}


class RemoveRequest(BaseModel):
    delete_files: bool = False


class SyncRequest(BaseModel):
    every_hours: float = 0
    auto_download: bool = False


@router.get("/api/jobs/{job_id}/items")
def job_items(job_id: str, since: int = 0):
    try:
        return engine.manager.items_since(job_id, since)
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@router.post("/api/jobs/{job_id}/download")
def download(job_id: str, req: DownloadRequest):
    try:
        return {"queued": engine.manager.queue(job_id, req.ids, dict(req.options))}
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(400, f"Can't use that folder: {exc.strerror or exc}")


@router.put("/api/jobs/{job_id}/options")
def job_options(job_id: str, req: DownloadRequest):
    """The playlist's Save button: store its options without queueing anything."""
    try:
        return engine.manager.set_options(job_id, dict(req.options))
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except OSError as exc:
        raise HTTPException(400, f"Can't use that folder: {exc.strerror or exc}")


@router.post("/api/jobs/{job_id}/estimate")
def estimate(job_id: str, req: DownloadRequest):
    try:
        return engine.manager.estimate(job_id, req.ids, dict(req.options))
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/api/jobs/{job_id}/sync")
def job_sync(job_id: str, req: SyncRequest):
    try:
        return engine.manager.set_sync(job_id, req.every_hours, req.auto_download)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/jobs/{job_id}/refresh")
def refresh_job(job_id: str):
    try:
        return {"added": engine.manager.refresh(job_id)}
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except Exception as exc:
        raise HTTPException(400, _clean_error(exc))


@router.get("/api/jobs/{job_id}/items/{item_id}/log")
def item_log(job_id: str, item_id: str):
    try:
        return engine.manager.item_log(job_id, item_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))


@router.post("/api/jobs/{job_id}/items/{item_id}/{action}")
def item_action(job_id: str, item_id: str, action: str):
    try:
        engine.manager.item_action(job_id, item_id, action)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/api/jobs/{job_id}/check")
def check_files(job_id: str):
    try:
        return {"started": engine.manager.start_check(job_id)}
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@router.post("/api/jobs/{job_id}/check/stop")
def stop_check(job_id: str):
    try:
        engine.manager.stop_check(job_id)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    return {"ok": True}


@router.get("/api/jobs/{job_id}/cleanup")
def cleanup_preview(job_id: str):
    try:
        return engine.manager.cleanup(job_id, dry_run=True)
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@router.post("/api/jobs/{job_id}/cleanup")
def cleanup(job_id: str):
    try:
        return engine.manager.cleanup(job_id, dry_run=False)
    except KeyError:
        raise HTTPException(404, "Playlist not found")


@router.post("/api/jobs/{job_id}/remove")
def remove_job(job_id: str, req: RemoveRequest | None = None):
    try:
        engine.manager.remove(job_id, bool(req and req.delete_files))
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    return {"ok": True}


@router.post("/api/jobs/{job_id}/m3u")
def export_m3u(job_id: str):
    try:
        return engine.manager.export_m3u(job_id)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str):
    try:
        engine.manager.action(job_id, action)
    except KeyError:
        raise HTTPException(404, "Playlist not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}
