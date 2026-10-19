"""Small helpers: text, time and file-name formatting, atomic writes, parts of videos."""

from __future__ import annotations

import os
import re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _escape_tmpl(text: str) -> str:
    """Make literal text safe inside a yt-dlp output template."""
    return text.replace("%", "%%")


def _safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" .")
    return text[:120] or "Untitled"


def _fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _atomic_write(path: Path, text: str):
    """Write so that a crash or power loss leaves either the old file or the new one, never half of each."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _parse_time(text) -> float | None:
    """ "90", "1:30", "1:02:03" or "1:30.5" -> seconds (None if it can't be read)."""
    text = str(text).strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", text):
        return None
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return secs


def _parse_section(section) -> dict | None:
    """Validate "download only part": {"start": "1:00", "end": "2:30"} -> {"start": 60.0, "end": 150.0}.
    An empty end means "to the end of the video". None when no part is asked for."""
    if not section:
        return None
    start_txt, end_txt = str(section.get("start") or "").strip(), str(section.get("end") or "").strip()
    if not start_txt and not end_txt:
        return None
    start = _parse_time(start_txt) if start_txt else 0.0
    end = _parse_time(end_txt) if end_txt else None
    if start is None or (end_txt and end is None):
        raise ValueError("Use times like 1:30, 1:02:03 or 90 for the part to download")
    if end is not None and end <= start:
        raise ValueError("The end of the part must be after its start")
    return {"start": start, "end": end}


def _section_label(section: dict) -> str:
    """For the file name: "1-00–2-30" (no colons, which some systems don't allow)."""
    end = _fmt_secs(section["end"]) if section.get("end") is not None else "end"
    return f"{_fmt_secs(section['start'])}–{end}".replace(":", "-")


def _expected_duration(item: dict) -> float | None:
    """Length the finished file should have: as verified at download (parts and cut sponsor segments make
    it shorter than the video), else the part asked for, else the video's length."""
    if item.get("media_duration"):
        return item["media_duration"]
    section = (item.get("options") or {}).get("section")
    if section:
        end = section.get("end") if section.get("end") is not None else item.get("duration")
        return end - section["start"] if end else None
    if (item.get("options") or {}).get("sponsorblock") == "remove":
        return None                                  # length after cutting isn't known
    return item.get("duration")


def _retry_sleep(n: int) -> float:
    return min(2 ** n, 30)          # 1, 2, 4, … capped at 30 s between yt-dlp's own retries
