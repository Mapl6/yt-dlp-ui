"""yt-dlp's version and updating it."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time

import yt_dlp

from ..config import YTDLP_CHECK_EVERY, YTDLP_RETRY_AFTER, log
from ..errors import _clean_error
from ..ytdlp import _base_opts, _ytdl


class UpdatesMixin:
    # ---- yt-dlp version and update

    def ytdlp_info(self) -> dict:
        """The yt-dlp version in use, and the newest one on PyPI (looked up in the background, cached)."""
        with self.cond:
            now, latest = time.time(), self._latest
            # after a failed lookup (offline), wait a while instead of asking PyPI again on every call
            stale = not latest["checking"] and now - latest["checked"] > YTDLP_CHECK_EVERY \
                and now - latest["failed"] > YTDLP_RETRY_AFTER
            if stale:
                latest["checking"] = True
        if stale:
            threading.Thread(target=self._check_latest, name="ytdlp-latest", daemon=True).start()
        with self.cond:
            return {"version": yt_dlp.version.__version__, "latest": self._latest["version"],
                    "checked": self._latest["checked"] or None, "checking": self._latest["checking"],
                    "update": dict(self.updating) if self.updating else None}

    def _check_latest(self):
        version = None
        try:
            with _ytdl(_base_opts(self.settings)) as ydl:
                data = json.loads(ydl.urlopen("https://pypi.org/pypi/yt-dlp/json").read())
            version = data["info"]["version"]
        except Exception as exc:
            log.info("Couldn't check for a newer yt-dlp: %s", _clean_error(exc))
        with self.cond:
            now = time.time()
            self._latest.update(checking=False, checked=now if version else self._latest["checked"],
                                failed=0 if version else now, version=version or self._latest["version"])
            self.version += 1

    def _update_cmd(self) -> list[str]:
        if shutil.which("uv"):
            return ["uv", "pip", "install", "-q", "-p", sys.executable, "--upgrade", "yt-dlp[default]"]
        return [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp[default]"]

    @staticmethod
    def _installed_ytdlp() -> str | None:
        """Version on disk (may be newer than the one loaded until the app restarts)."""
        try:
            res = subprocess.run([sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
                                 capture_output=True, text=True, timeout=60)
            return res.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def update_ytdlp(self) -> dict:
        with self.cond:
            if self.updating and self.updating["running"]:
                return dict(self.updating)
            self.updating = {"running": True, "ok": None, "output": "", "installed": None, "started": time.time()}
            self.version += 1
        threading.Thread(target=self._run_update, name="ytdlp-update", daemon=True).start()
        return dict(self.updating)

    def _run_update(self):
        try:
            res = subprocess.run(self._update_cmd(), capture_output=True, text=True, timeout=600)
            ok, output = res.returncode == 0, (res.stdout + res.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, output = False, str(exc)
        installed = self._installed_ytdlp() if ok else None
        with self.cond:
            self.updating.update(running=False, ok=ok, output=output[-2000:], installed=installed,
                                 restart=bool(installed and installed != yt_dlp.version.__version__))
            if installed:
                self._latest.update(version=installed if installed > (self._latest["version"] or "") else self._latest["version"])
            self.version += 1
        log.info("yt-dlp update: %s (installed %s)", "ok" if ok else "failed", installed)
