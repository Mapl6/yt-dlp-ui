"""Item fields and statuses, export markers and postprocessor steps shared by the Manager mixins."""

from __future__ import annotations

from ..config import ACTIVE

# ---------------------------------------------------------------- library manager
#
# Every fetched link is saved in a library ("jobs") with ALL of its videos.
# Item status:  new (not downloaded) -> queued -> downloading -> processing -> done | failed
# "skipped" means the file was already in the folder's archive (counts as downloaded).
# Item fields:  index   = position in the playlist right now (updated on every refresh)
#               number  = file-name prefix, given once and never changed, so files on disk keep their names
#               removed = no longer in the playlist, or private/deleted (can't be downloaded)
#
# Reliability rules:
#  * a video is only "done" (and written to the folder's archive) after the file on disk is verified;
#  * connection problems never use up the normal retry budget, and the queue waits while offline;
#  * disk-full and YouTube rate limiting pause the whole queue instead of failing videos one by one;
#  * interrupted downloads keep their .part files and resume where they stopped;
#  * every change to an item goes through the lock (self.cond).

ITEM_KEYS = ("index", "id", "title", "url", "duration", "number", "status", "progress", "speed",
             "eta", "downloaded", "total", "error", "attempts", "filepath", "removed", "health",
             "started", "finished", "priority", "held", "step", "fmt", "section")
STATUSES = {"new", "queued", "downloading", "processing", "done", "failed", "skipped"}
TRANSIENT = ("speed", "eta", "next_try", "cancel", "step")   # runtime-only item fields, left out of exports
EXPORT_APP = "yt-downloader"       # marks an "Export all data" file
EXPORT_FORMAT = 1                  # bump when the export layout changes incompatibly
LIVE_LEVELS = {"excellent", "good", "fair", "weak"}
LIVE = {"queued", *ACTIVE}         # statuses kept in the scheduler's index
HISTORY_LEN = 20                   # attempts kept per video for the dashboard's detail view
SPEED_SAMPLE = 2                   # seconds between total-speed samples (speed chart, queue ETA)

# Postprocessor (pp_key) -> sub-step shown while a video is processing. Keys starting with "_" are ours.
PP_STEPS = {"Merger": "Merging", "ExtractAudio": "Converting audio", "EmbedThumbnail": "Embedding thumbnail",
            "Metadata": "Writing metadata", "EmbedSubtitle": "Embedding subtitles",
            "ModifyChapters": "Removing sponsor segments", "VideoConvertor": "Converting", "VideoRemuxer": "Remuxing"}
PP_QUIET = {"ThumbnailsConvertor", "SponsorBlock", "MoveFilesAfterDownload"}   # run before/around the download
