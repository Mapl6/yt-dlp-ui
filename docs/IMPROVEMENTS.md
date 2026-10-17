# YT Downloader — Improvement Plan

Last updated 2026-10-16. Open work first; finished work is summarized at the end.

## Next up

1. **Subscriptions / auto-sync:** check saved playlists every N hours, with optional auto-download of new videos.
2. **SponsorBlock:** remove sponsor segments.
3. **Subtitle languages:** choose languages, including auto-generated subtitles (English is hard-coded today).
4. **Desktop notification** when a playlist finishes.

## Activity dashboard: next steps

The dashboard (Activity panel) shows what's downloading, the queue and recent results. Planned additions:

### Queue control

- **Reorder the queue:** "Move to top" / "Move to bottom" per video, and drag to reorder. Today `_next_item` picks by playlist creation date and then playlist order. This needs a `priority` field that the scheduler sorts by first.
- **Pause / resume one video** without removing it from the queue (today only the whole playlist can be paused).
- **Pause all / Resume all** buttons in the dashboard header.
- **"Try now"** for a video waiting on a retry: clears its `next_try`. Also a "Try now" on the queue-wide hold banner (rate limit, disk full) for when the user has fixed the cause.
- **Retry all failed** across every playlist, from the "Recently finished" tab.

### More detail

- **Expandable row** per video, showing:
  - chosen format and resolution;
  - output folder and file name;
  - each attempt with its error kind (network, rate limit, other);
  - the log lines inline, instead of in the Details dialog.
- **Processing sub-step:** "Merging", "Verifying", "Embedding thumbnail", "Re-merging from kept streams" instead of only "Processing".
- **Time left for the whole queue:** estimated from known sizes (or duration × bitrate for the chosen quality) and the recent average speed.
- **Disk tile:** free space on each target drive next to the estimated size of the queue, warning before the 1 GB low-disk hold kicks in.
- **Speed chart:** total download speed over the last few minutes, like the connection panel's response-time chart.
- **Per-playlist filter** and title search in the dashboard lists.

### History

- **Real event log:** a SQLite table of finished and failed downloads, with time, size and duration.
  - "Recently finished" is built from each video's current status, so a video re-queued or retried disappears from it.
  - Videos downloaded before the dashboard existed have no `finished` time, so they never appear in it.
- **Stats from the log:**
  - GB downloaded per day or week;
  - success rate;
  - average speed;
  - most common error kinds.

  This would cover the "History and stats page" idea below.
- **Clear** button and "show more" paging for "Recently finished" (it shows the last 30 today).
- **Actions on finished rows:** Reveal in Finder, Play.

### Polish

- **Browser tab title shows progress,** e.g. "↓3 · 42% — YT Downloader", so it's visible from another tab.
- **Desktop notification when a video fails permanently,** not only when a playlist finishes.
- **Collapse the dashboard** when nothing is queued, remembered per browser.
- **Faster `/api/activity`:** it scans every item of every playlist each second. Keep running counters, or push changes with SSE (see Performance below).

## Open issues

| Issue | Fix |
| --- | --- |
| **Network mode is unprotected.** With `--host 0.0.0.0`, anyone on the network can use the app (it prints a warning). | Optional password for network mode. |
| **The page's code has no tests.** That includes the range selector (`1-50, 80`), tabs and selection. | Small JS test file, run with Node. |
| **Possible false "damaged" result.** "Check files" marks a file as damaged if `ffprobe` can't open it. The file is only renamed to `.damaged`, never deleted. | Before renaming, re-check with a full decode. |

## Performance and code quality

- Replace the 1-second polling with Server-Sent Events (push only when `rev` changes).
- `_next_item` scans every item while holding the lock; use a ready-queue.
- Draw only the visible rows (virtual scrolling) for playlists with 2,000+ videos.
- Pin `fastapi`/`uvicorn` versions; keep only `yt-dlp` unpinned.
- Add `run.bat`/`run.ps1` for Windows. File locks are skipped on Windows today.
- Split `app.js` into modules: api, library, netpanel, settings.

## Feature ideas

### Low effort (yt-dlp already supports these)

- Download only part of a video (`download_ranges`).
- Paste many links at once, or import a `.txt` file of URLs.
- Show the yt-dlp version with an "Update now" button.

### Medium effort

- Download schedule (e.g. only 1–7 am) and a total speed limit across all downloads.
- Estimated total size before queueing (the queue already pauses below 1 GB free).
- Actions on downloaded videos: Reveal in Finder, Play, Delete file, Mark as downloaded.
- Export a playlist as `.m3u`.
- Sort and filter by duration, date or status; keyboard shortcuts.
- History and stats page.
- Option to delete files when removing a playlist.

### Bigger

- Browser extension or bookmarklet: "Send to YT Downloader".
- Standalone Mac app (pywebview + menu bar).
- Docker image for a home server or NAS (needs the network-mode password first).

## Done (2026-10-16)

### Security and correctness

- The API only answers `127.0.0.1`/`localhost`, and every call needs a per-session token (blocks DNS rebinding and requests from other sites).
- Settings are validated; bad values fall back to defaults.
- Playlist refresh: the list follows the playlist's order, file numbers are permanent, and new videos get the next free number with no extra digits. Videos that leave the playlist or become private are marked **Unavailable**; a cut-short fetch doesn't mark anything.
- Errors are classified by yt-dlp exception type and HTTP status, with message text only as a fallback.
- Every change to a video goes through the lock.

### Download reliability (weak internet)

- Interrupted downloads resume from their `.part` file; missing stream pieces fail the download instead of leaving gaps.
- Connection problems are retried with growing pauses (up to 40 times) without using up the normal retries. The queue waits while offline and runs fewer downloads on a weak connection.
- Disk full, an unplugged drive or YouTube rate limiting pause the whole queue, with a banner explaining why.
- The pause between videos no longer takes a download slot; `run.sh` starts even when offline.

### File integrity

- Each stream must match the exact size the site reports, and every finished file is checked (length, plus decoding its last seconds) before it counts as downloaded.
- Separate video/audio streams are kept until the merged file passes the check; a bad merge is redone from them without downloading again.
- **Check files** button: verifies existing downloads, renames damaged files to `.damaged`, flags missing ones and adopts good files already in the folder. "Check for new videos" also flags files that have disappeared.
- **Clean up leftovers** button: deletes partial downloads, unmerged streams and `.damaged` files after showing the list.

### Activity dashboard

- **Activity** panel across all playlists, backed by `GET /api/activity`.
  - Totals: downloading out of the current limit, queued (ready or waiting), total speed and data left, done and failed in the last 24 hours.
  - **Downloading now** tab: progress, size, speed, time left, start time and attempt number for each video.
  - **Up next** tab: the queue in scheduler order, with why each video is waiting.
  - **Recently finished** tab: with Retry and Details.
- Videos now record `started` and `finished` times.

### Storage, logging, safety

- The library is in SQLite (`data/library.db`); each save writes only what changed. It's backed up at startup and every 10 minutes, and a damaged database is kept aside while the backup is loaded. `state.json` is migrated automatically.
- `data/app.log` (rotating) plus a **Details** button per video.
- Only one copy of the app can use a library; it refuses to start if its port is taken; a per-video lock stops two copies from writing the same file.

**Tests:** `tests/test_core.py`, `tests/test_reliability.py`, `tests/test_health.py` — 109 checks, no internet needed.
