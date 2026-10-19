"""App settings: defaults and validation."""

from __future__ import annotations

import re
from pathlib import Path

from .config import MAX_WORKERS

DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "Downloads" / "YT Downloader"),
    "concurrency": 3,
    "cookies_browser": "",       # "", chrome, safari, firefox, brave, edge, arc, chrome-canary ...
    "cookies_profile": "",       # browser profile folder ("Default", "Profile 1", a Firefox profile path); "" = newest
    "rate_limit_mb": 0.0,        # per download, 0 = unlimited
    "sleep_between": 0.0,        # seconds between starting downloads (helps avoid 429s)
    "proxy": "",                 # e.g. socks5://127.0.0.1:1080 or http://127.0.0.1:8080
    "wait_for_network": True,    # hold the queue while offline, slow down on a weak connection
    "total_rate_mb": 0.0,        # shared by all downloads, 0 = unlimited
    "schedule_on": False,        # only start downloads between schedule_start and schedule_end (local time)
    "schedule_start": "01:00",
    "schedule_end": "07:00",
    "sub_langs": "en",           # default subtitle languages, e.g. "en, fa"
    "notify": False,             # desktop notifications (finished playlist, failed video)
}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


SUB_LANG = re.compile(r"^(?:all|[a-z]{2,3}(?:-[a-z0-9]+)*)$", re.I)


def _coerce_settings(raw: dict) -> dict:
    """Return complete, well-typed settings; bad values fall back to the defaults."""
    out = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = raw.get(key, default)
        try:
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(float(value))
            elif isinstance(default, float):
                value = max(0.0, float(value))
            else:
                value = str(value or "").strip()
        except (TypeError, ValueError):
            value = default
        out[key] = value
    out["concurrency"] = max(1, min(MAX_WORKERS, out["concurrency"]))
    out["output_dir"] = out["output_dir"] or DEFAULT_SETTINGS["output_dir"]
    for key in ("schedule_start", "schedule_end"):
        m = HHMM.match(out[key])
        out[key] = f"{int(m.group(1)):02d}:{m.group(2)}" if m else DEFAULT_SETTINGS[key]
    return out


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _sub_langs(text: str) -> list[str]:
    """ "en, fa" -> yt-dlp patterns that also match regional variants (en-US, en-GB…)."""
    out = []
    for code in re.split(r"[\s,;]+", text or ""):
        if SUB_LANG.match(code):
            out += ["all"] if code.lower() == "all" else [code, f"{code}-.*"]
    return out or ["en", "en-.*"]
