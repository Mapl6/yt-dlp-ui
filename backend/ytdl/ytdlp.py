"""Creating YoutubeDL objects: base options, quiet and per-video loggers. Every YoutubeDL is made by _ytdl()."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import uuid

import yt_dlp

from . import config
from .cookies import _cookie_file
from .util import _retry_sleep


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class _ItemLogger(_QuietLogger):
    """Keeps yt-dlp's warnings and errors for one video (shown under "Details") and writes them to app.log."""

    LEVEL = re.compile(r"^(?:\x1b\[[0-9;]*m)*(?:ERROR|WARNING):(?:\x1b\[[0-9;]*m)*\s*")

    def __init__(self, add):
        self._add = add

    def warning(self, msg):
        self._add("warning", self.LEVEL.sub("", str(msg)))   # the log line has its own level: no "ERROR: ERROR:"

    def error(self, msg):
        self._add("error", self.LEVEL.sub("", str(msg)))


def _base_opts(settings: dict, logger=None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": logger or _QuietLogger(),
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
        "socket_timeout": 30,        # a stalled connection is dropped and retried after 30 s
        "retries": 15,
        "extractor_retries": 5,
        "retry_sleep_functions": {"http": _retry_sleep, "fragment": _retry_sleep,
                                  "extractor": _retry_sleep, "file_access": lambda n: 1},
    }
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"].strip()
    if settings.get("cookies_browser"):
        opts["cookiesfrombrowser"] = (settings["cookies_browser"], settings.get("cookies_profile") or None)
    return opts


@contextlib.contextmanager
def _ytdl(opts: dict):
    """A YoutubeDL that uses the imported cookies.txt, if there is one, instead of the browser's cookies. Each one
    gets its own copy of the file: yt-dlp writes the cookies back when it closes, and parallel downloads would
    otherwise overwrite each other's copy."""
    tmp = None
    src = _cookie_file()
    if src.is_file() and "cookiefile" not in opts:
        tmp = config.DATA_DIR / ".cookies-tmp" / f"{uuid.uuid4().hex}.txt"
        tmp.parent.mkdir(exist_ok=True)
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        opts = {k: v for k, v in opts.items() if k != "cookiesfrombrowser"} | {"cookiefile": str(tmp)}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
