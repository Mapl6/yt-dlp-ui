# YT Downloader — Improvement Plan

Last updated 2026-10-17. Open work first; finished work is summarized at the end.

## Next up

1. **Docker image** for a home server or NAS. The network-mode password it needed now exists (`--password` / `YTDL_PASSWORD`).
2. **Browser extension or bookmarklet:** "Send to YT Downloader" from a YouTube page (it can post to `/api/open-many`).

## Open issues

| Issue | Fix |
| --- | --- |
| **Windows is untested.** `run.bat`, `run.ps1` and the `msvcrt` file locks were only checked for syntax on macOS. | Run the test suites on a Windows machine. |
| **The Mac app's menu-bar icon is only checked by hand.** `mac_app.py --smoke` covers the window, the page and creating the icon, not clicking its menu. | A UI test with the Accessibility API, or keep checking by hand after changes. |
| **The total speed limit is per connection.** A fragmented stream (HLS/DASH) downloads 4 fragments at once, each allowed the full share, so it can briefly go over the total. | Divide the share by `concurrent_fragment_downloads` for fragmented formats. |
| **Logins don't survive a restart.** Password sessions are kept in memory, so after a restart (or a yt-dlp update) every browser logs in again. | Signed session cookies with a key stored in `data/`. |
| **Long errors are cut to one line in the playlist list** (rows have a fixed height for virtual scrolling). The full text is in the tooltip and in Details. | An expandable row, like the dashboard's. |

## Ideas

- Per-playlist schedule and speed limit (today both are global).
- Notifications for queue-wide pauses (rate limiting, disk full), not only finished playlists and failed videos.
- A "Downloaded" badge on the Mac app's Dock icon.

## Done (2026-10-17)

The roadmap from 2026-10-16, apart from Docker and the browser extension. Five commits on `feature/roadmap`: core, queue control, download options, server, front end; plus the Mac app and these docs.

### Queue control

- Move a video to the top or bottom of the queue, or drag it in the dashboard (`priority`, then playlist age and order).
- Pause and resume one video (`held`); a running one stops and keeps its `.part` file.
- **Try now** for a video waiting on a retry, and on the queue-wide pause banner.
- **Pause all / Resume all**, **Retry all failed**.

### Dashboard and history

- Event log (SQLite `events` table) of finished and failed downloads with size and time; earlier downloads are added once from their files. "Recently finished" reads it, with paging and **Clear** (hides rows; stats keep them).
- **History & stats** tab: data per day or week, success rate, average speed, most common error kinds, with a table view.
- Tiles for time left (queue size from known sizes or duration × bitrate, and the last two minutes' speed) and free disk space per drive; a speed chart.
- Expandable rows: chosen and actual format, folder and file, attempts with error kinds, and the log.
- Processing sub-steps (Merging, Verifying, Embedding thumbnail, Re-merging…).
- Filter by playlist and search; totals ignore the filter. The panel folds away when idle.
- Tab title progress, desktop notifications (finished playlist, failed video), keyboard shortcuts.

### Downloads and library

- Subscriptions: check saved playlists every 6/12/24 h or weekly, optionally queueing new videos.
- SponsorBlock: mark or cut segments; a SponsorBlock outage never fails a download.
- Subtitle languages, including auto-generated ones.
- Download only part of a video (own file name, never archived). Cut and partial files are verified against their real length (`media_duration`), also by Check files.
- Paste many links or import a `.txt` file; estimated size and free space before queueing.
- Download schedule and a total speed limit shared by running downloads (applied to downloads in progress).
- Per-video Show in Finder, Play, Delete file, Mark as downloaded; remove a playlist together with its files; export `.m3u8`.
- yt-dlp version check, **Update now** and **Restart now**.

### Platform and code quality

- Live updates over Server-Sent Events (`/api/stream`), polling as a fallback. `/api/activity` and the scheduler read an index of queued and active videos instead of scanning every video.
- Virtual scrolling in playlist cards; `app.js` split into ES modules under `static/js/` (no build step).
- Check files decodes a file fully before calling it damaged.
- Optional password for network mode (login page, session cookie, rate limit).
- `fastapi` and `uvicorn` pinned; `run.bat` / `run.ps1`; file locks on Windows.
- Optional Mac app: `mac_app.py`, `run-mac.sh`, `scripts/make_mac_app.sh`.
- Tests: `tests/test_features.py` (Python) and `tests/test_ui.mjs` (page logic, run with Node), next to the three existing suites.

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

