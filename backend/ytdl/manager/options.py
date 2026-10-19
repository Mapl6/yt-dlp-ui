"""Settings and cookies changes, and sharing the total speed limit between downloads."""

from __future__ import annotations

import json
import os

from .. import config
from ..config import log
from ..cookies import MAX_COOKIE_FILE, _cookie_file, _cookie_summary, _parse_cookie_text, cookie_file_status
from ..settings import DEFAULT_SETTINGS, _coerce_settings
from ..util import _atomic_write


class OptionsMixin:
    def update_settings(self, patch: dict) -> dict:
        with self.cond:
            old = self.settings
            self.settings = _coerce_settings(self.settings | {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            _atomic_write(config.SETTINGS_FILE, json.dumps(self.settings, indent=2))
            self.version += 1
            self.cond.notify_all()
            self._rebalance()
            log.info("Settings changed: %s", {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            if any(old.get(k) != self.settings.get(k) for k in ("cookies_browser", "cookies_profile", "proxy")):
                self._identity_changed("Cookie or proxy settings changed")
            return dict(self.settings)

    def import_cookies(self, text: str) -> dict:
        """Save a cookies.txt (Netscape format) for yt-dlp; it is used instead of the browser's cookies."""
        if len(text.encode("utf-8", "replace")) > MAX_COOKIE_FILE:
            raise ValueError("This file is too big for a cookies.txt")
        summary = _cookie_summary(_parse_cookie_text(text))
        if not summary["youtube"]:
            raise ValueError("There are no YouTube cookies in this file. Open youtube.com, sign in, and export the "
                             "cookies again from that tab")
        path = _cookie_file()
        _atomic_write(path, text)
        os.chmod(path, 0o600)                         # it can sign in to the user's account
        log.info("Imported a cookies file: %d cookies, %d for YouTube", summary["count"], summary["youtube"])
        self._identity_changed("Cookies imported")
        with self.cond:
            self.version += 1
        return cookie_file_status() or {}

    def remove_cookies(self) -> bool:
        try:
            _cookie_file().unlink()
        except FileNotFoundError:
            return False
        log.info("Removed the imported cookies file")
        self._identity_changed("Cookies file removed")
        with self.cond:
            self.version += 1
        return True

    def _identity_changed(self, why: str):
        """New cookies or a new proxy are what fix YouTube's block: try again at once instead of waiting."""
        with self.cond:
            hold = self._active_hold()
            if (hold and hold.get("kind") in ("ratelimit", "cookies")) or self.rate_probe or self.rate_strikes:
                self._clear_hold(probe=False, kinds=("ratelimit", "cookies"))
                log.info("%s: rate-limit pause cleared", why)

    def _rate_for(self, running: int) -> float | None:
        """Speed limit for each download (bytes/s): the per-download limit, or an equal share of the total."""
        limits = []
        if self.settings.get("rate_limit_mb"):
            limits.append(float(self.settings["rate_limit_mb"]) * 1024 * 1024)
        if self.settings.get("total_rate_mb"):
            limits.append(float(self.settings["total_rate_mb"]) * 1024 * 1024 / max(1, running))
        return min(limits) if limits else None

    def _rebalance(self):
        """Share the total speed limit between the downloads running now. yt-dlp reads params["ratelimit"]
        on every chunk, so this applies to downloads already in progress."""
        with self.cond:
            rate = self._rate_for(len(self._ydls))
            for ydl in self._ydls:
                ydl.params["ratelimit"] = rate
