"""What to download and how: yt-dlp format and postprocessor options, size estimates, our postprocessors."""

from __future__ import annotations

from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP

from .errors import _clean_error
from .settings import _sub_langs

VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
AUDIO_FORMATS = {"mp3", "m4a", "opus"}
# Typical total bitrate (bits/s) per quality, to estimate sizes before they are known.
BITRATES = {2160: 20e6, 1440: 10e6, 1080: 5e6, 720: 2.5e6, 480: 1.2e6, 360: 0.7e6, 240: 0.4e6, 144: 0.2e6}
BEST_BITRATE, AUDIO_BITRATE = 8e6, 160e3
SPONSOR_CATEGORIES = ("sponsor", "selfpromo", "interaction")


def _estimate_bytes(item: dict, options: dict) -> tuple[int, bool]:
    """-> (bytes still to download, exact?). Uses the size yt-dlp reported, else duration × typical bitrate."""
    if item.get("total") and item["status"] in ("queued", "downloading"):
        return max(0, int(item["total"]) - int(item.get("downloaded") or 0)), True
    quality = str(options.get("quality", "best"))
    bps = AUDIO_BITRATE if quality in AUDIO_FORMATS else BITRATES.get(int(quality), BEST_BITRATE) \
        if quality.isdigit() else BEST_BITRATE
    duration = item.get("duration")
    section = options.get("section")
    if duration and section:
        duration = (section["end"] if section.get("end") is not None else duration) - section["start"]
    return (int(max(0, duration) * bps / 8) if duration else 0), False


class _PlaylistTags(PostProcessor):
    """Adds the playlist name (album) and the video's number (track) to the file's metadata."""

    def __init__(self, tags: dict):
        super().__init__()
        self.tags = {f"meta_{k}": v for k, v in tags.items() if v}

    def run(self, info):
        info.update(self.tags)
        return [], info


def _describe_format(info: dict) -> str:
    """ "1920x1080 · avc1 + mp4a · mp4" for the formats yt-dlp picked."""
    fmts = info.get("requested_formats") or [info]
    res = next((f.get("resolution") for f in fmts if f.get("height")), None) or info.get("resolution")
    codecs = [next((f.get(k) for f in fmts if f.get(k) not in (None, "none")), None) for k in ("vcodec", "acodec")]
    bits = [res if res and res != "audio only" else None,
            " + ".join(c.split(".")[0] for c in codecs if c), info.get("ext")]
    return " · ".join(b for b in bits if b)


class _FormatNote(PostProcessor):
    """Runs after format selection, before the download: reports which format was chosen."""

    def __init__(self, report):
        super().__init__()
        self.report = report

    def run(self, info):
        try:
            self.report(_describe_format(info))
        except Exception:                          # a display detail must never break a download
            pass
        return [], info


class _SafeSponsorBlock(SponsorBlockPP):
    """SponsorBlock that never fails a download: if the segment list can't be fetched, download without it."""

    def run(self, info):
        try:
            return super().run(info)
        except Exception as exc:
            self.report_warning(f"SponsorBlock segments unavailable ({_clean_error(exc)}); continuing without them")
            return [], info


# ---------------------------------------------------------------- options

def build_format_opts(options: dict) -> tuple[dict, list]:
    """Translate UI options into yt-dlp format options and postprocessors."""
    quality = str(options.get("quality", "best"))
    container = options.get("container", "mp4")
    pps: list[dict] = []
    opts: dict = {}

    if quality in AUDIO_FORMATS:
        opts["format"] = {"m4a": "ba[ext=m4a]/ba/b", "opus": "ba[acodec=opus]/ba/b"}.get(quality, "ba/b")
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": quality,
                    "preferredquality": "0" if quality == "mp3" else None})
        pps[-1] = {k: v for k, v in pps[-1].items() if v is not None}
    else:
        opts["format"] = "bv*+ba/b"
        sort = [] if quality == "best" else [f"res:{int(quality)}"]
        if container == "mp4":
            # Prefer H.264/AAC so files play everywhere (QuickTime, phones, TVs).
            sort += ["vcodec:h264", "acodec:aac"]
        opts["format_sort"] = sort
        opts["merge_output_format"] = container
        if options.get("subtitles"):
            opts |= {"writesubtitles": True, "subtitleslangs": _sub_langs(options.get("sub_langs") or "en"),
                     "subtitlesformat": "srt/best"}
            if options.get("auto_subs"):
                opts["writeautomaticsub"] = True       # YouTube's auto-generated subtitles
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    sponsor = options.get("sponsorblock")
    if sponsor in ("mark", "remove"):
        # The segments are fetched by _SafeSponsorBlock (added in _download_locked). ModifyChapters must run
        # before FFmpegMetadata: it cuts the segments ("remove") or turns them into chapters ("mark").
        cats = set(options.get("sponsor_categories") or SPONSOR_CATEGORIES) & set(SponsorBlockPP.CATEGORIES)
        pps.append({"key": "ModifyChapters", "remove_sponsor_segments": cats if sponsor == "remove" else set(),
                    "force_keyframes": False})
    if options.get("metadata", True):
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    elif sponsor == "mark":
        pps.append({"key": "FFmpegMetadata", "add_metadata": False, "add_chapters": True})
    if options.get("thumbnail", True):
        opts["writethumbnail"] = True
        pps.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return opts, pps
