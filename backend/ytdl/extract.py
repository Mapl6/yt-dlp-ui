"""Reading a link: a video, a playlist or a channel, with all of its videos."""

from __future__ import annotations

import re

from .formats import VIDEO_HEIGHTS
from .ytdlp import _base_opts, _ytdl

# Placeholder titles YouTube uses for entries that can't be watched any more.
UNAVAILABLE_TITLE = re.compile(r"^\[(?:private|deleted|unavailable)[^\]]*\]$", re.I)


# ---------------------------------------------------------------- inspection

def inspect_url(url: str, settings: dict) -> dict:
    """Return metadata for a video or (flattened) playlist without downloading anything."""
    opts = _base_opts(settings) | {"skip_download": True, "extract_flat": "in_playlist"}
    with _ytdl(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)

        if info.get("_type") in ("playlist", "multi_video"):
            entries = list(info.get("entries") or [])
            # Channel pages return their tabs (Videos, Shorts, Live) as nested playlists: flatten one level.
            if entries and all(e.get("_type") == "playlist" or e.get("ie_key") == "YoutubeTab" for e in entries):
                nested = []
                for tab in entries:
                    sub = ydl.sanitize_info(ydl.extract_info(tab["url"], download=False))
                    nested.extend(sub.get("entries") or [])
                entries = nested

            seen, items = set(), []
            for e in entries:
                if not e or not e.get("id") or e["id"] in seen:
                    continue
                seen.add(e["id"])
                items.append({
                    "index": len(items) + 1,
                    "id": e["id"],
                    "title": e.get("title") or e["id"],
                    "url": e.get("url") if str(e.get("url", "")).startswith("http")
                           else f"https://www.youtube.com/watch?v={e['id']}",
                    "duration": e.get("duration"),
                    "extractor": e.get("ie_key") or info.get("extractor_key") or "youtube",
                })
            return {
                "kind": "playlist",
                "source_id": info.get("id"),
                "title": info.get("title") or "Playlist",
                "uploader": info.get("uploader") or info.get("channel"),
                "thumbnail": _pick_thumb(info) or (_pick_thumb(entries[0]) if entries else None),
                "items": items,
                "heights": VIDEO_HEIGHTS,
            }

        heights = sorted({f["height"] for f in info.get("formats") or [] if f.get("height")}, reverse=True)
        return {
            "kind": "video",
            "source_id": info.get("id"),
            "title": info.get("title") or info.get("id"),
            "uploader": info.get("uploader") or info.get("channel"),
            "thumbnail": info.get("thumbnail") or _pick_thumb(info),
            "items": [{
                "index": 1,
                "id": info["id"],
                "title": info.get("title") or info["id"],
                "url": info.get("webpage_url") or url,
                "duration": info.get("duration"),
                "extractor": info.get("extractor_key") or "youtube",
            }],
            "heights": [h for h in VIDEO_HEIGHTS if any(x >= h for x in heights)] or heights,
        }


def _pick_thumb(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return best.get("url")
    return info.get("thumbnail")
