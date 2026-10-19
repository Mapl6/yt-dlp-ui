"""Settings, cookies, export and import of all data, yt-dlp updates and restarting."""

from __future__ import annotations

import os
import sys
import threading
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...cookies import check_cookies, cookie_sources
from .. import engine

router = APIRouter()



class ImportRequest(BaseModel):
    data: dict
    settings: bool = False      # also replace the settings with the file's


class CookieFileRequest(BaseModel):
    text: str


class CookieTestRequest(BaseModel):
    browser: str
    profile: str = ""


@router.put("/api/settings")
def settings(patch: dict):
    result = engine.manager.update_settings(patch)
    engine.net.check_now()   # a new proxy changes reachability right away
    return result


@router.get("/api/cookies/sources")
def cookies_sources():
    """Browsers on this computer that cookies can be read from, with their profiles."""
    return {"browsers": cookie_sources()}


@router.post("/api/cookies/test")
def cookies_test(req: CookieTestRequest):
    """Read the chosen browser's cookies now (macOS may ask for the keychain password)."""
    return check_cookies(req.browser.strip(), req.profile.strip())


@router.post("/api/cookies/file")
def cookies_import(req: CookieFileRequest):
    """Save a cookies.txt (Netscape format) exported from a browser; downloads use it instead of the browser."""
    try:
        return engine.manager.import_cookies(req.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/cookies/file")
def cookies_remove():
    return {"removed": engine.manager.remove_cookies()}


@router.get("/api/export")
def export_all():
    """Everything in one JSON file: settings, every playlist with its videos, and the download history."""
    name = time.strftime("yt-downloader-export-%Y%m%d-%H%M%S.json")
    return JSONResponse(engine.manager.export_data(), headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/api/import")
def import_all(req: ImportRequest):
    """Merge an export file into the library (adds what is missing, never deletes)."""
    try:
        return engine.manager.import_data(req.data, req.settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/ytdlp")
def ytdlp():
    return engine.manager.ytdlp_info() | {"can_restart": engine.runtime["can_restart"]}


@router.post("/api/ytdlp/update")
def ytdlp_update():
    return engine.manager.update_ytdlp()


@router.post("/api/restart")
def restart():
    """Restart the app (to load an updated yt-dlp). Interrupted downloads resume from their .part files."""
    if not engine.runtime["can_restart"]:
        raise HTTPException(400, "Quit and open the app again to finish the update.")

    def later():
        time.sleep(0.5)                       # let this response reach the page first
        engine.manager.save_state()
        argv = [os.path.abspath(sys.argv[0]), *sys.argv[1:]]
        if "--no-browser" not in argv:
            argv.append("--no-browser")       # the open page reloads itself
        os.environ["YTDL_RESTARTED"] = "1"
        os.execv(sys.executable, [sys.executable, *argv])   # fds are close-on-exec: lock and port are released
    threading.Thread(target=later, daemon=True).start()
    return {"ok": True}
