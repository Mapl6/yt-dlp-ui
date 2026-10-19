"""Adding links to the library."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...errors import _clean_error
from .. import engine

router = APIRouter()



class OpenRequest(BaseModel):
    url: str


class OpenManyRequest(BaseModel):
    urls: list[str]


@router.post("/api/open")
def open_url(req: OpenRequest):
    """Fetch a link and save it (with all of its videos) in the library."""
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Paste a video or playlist link.")
    try:
        return engine.manager.open_url(url)
    except Exception as exc:
        raise HTTPException(400, _clean_error(exc))


@router.post("/api/open-many")
def open_many(req: OpenManyRequest):
    """Add many links (pasted, or from a .txt file) in the background."""
    try:
        return engine.manager.open_many(req.urls)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
