# YT Downloader — Improvement Plan

Last updated 2026-10-18. Open work first; finished work is summarized at the end.

## Next up

In this order, on a branch from `main`. The full test suite (done 2026-10-18, see below) is the safety net: run `scripts/test.sh` after every move.

### 1. Restructure the backend; separate front and back end

- Folders `backend/` and `frontend/`. The library stays in `<repo>/data/`.
- Split `manager.py` (about 3,000 lines) into modules: config and paths, error kinds, settings, store (SQLite and backups), yt-dlp options and loggers, verification, files (archive, locks), cookies, and the `Manager` split by area (scheduler and queue, download pipeline, file health, sync, event log, file actions, export/import).
- Split `server.py` into FastAPI routers. Keep the rule that specific job routes come before `POST /api/jobs/{job_id}/{action}`, with a test for it.
- Move one piece at a time, with every test suite passing after each move; no behaviour changes in the same step.
- One script still starts everything: `run.sh` / `run.bat` / `run.ps1` / `run-mac.sh` start the backend and, in dev mode, the front-end dev server, and stop both on Ctrl+C. `python server.py` keeps working (restart and the Mac app use it).
- Update `CLAUDE.md`, the `.claude/rules/` paths and the skills (`safe-test-server`, `run-tests`).

### 2. Move the front end to Next.js

- Port `static/js/` (about 3,500 lines of ES modules) to Next.js with React, one module at a time: shared logic (API client and live updates, formatters, filters, stats), dialogs and menus, Settings, network panel, dashboard and charts, library cards with the virtual list, notifications and shortcuts.
- Keep what the page does now: live updates over `/api/stream` with polling as a fallback, `items?since=rev`, updates that never lose a click, keyboard shortcuts, phone layout, dark mode.
- Build to static files (`output: "export"`) and **commit the build**, so users still need only Python, it works offline, and the Mac app needs no change. CI checks the committed build is current.
- The backend keeps injecting the `X-Token` into the exported `index.html`. For development, `next dev` proxies `/api` to the backend, which then offers the token on a dev-only route.
- Serve the new page beside the old one until the end-to-end tests (`tests/e2e/`) pass on both, then switch and delete `static/`.

### Later

- **Docker image** for a home server or NAS (the network-mode password exists: `--password` / `YTDL_PASSWORD`).
- **Browser extension or bookmarklet:** "Send to YT Downloader" from a YouTube page (it can post to `/api/open-many`).

## Open issues

| Issue | Fix |
| --- | --- |
| **Windows is untested.** `run.bat`, `run.ps1` and the `msvcrt` file locks were only checked for syntax on macOS. | CI has a Windows job for the Python tests (`.github/workflows/test.yml`, not blocking yet); it runs once the repo is on GitHub. Make it pass, then make it required. |
| **Coverage gaps** (89% overall, floor 88% in `pyproject.toml`). Not covered: reading real browser cookies (`check_cookies`), channel tabs in `inspect_url`, a failing automatic playlist check, `server.py` startup and restart (only run as a subprocess, which coverage doesn't measure). | Tests with a fake cookie database and a fake channel; measure subprocesses (`coverage` `patch = subprocess`). Raise the floor. |
| **Retrying a video that isn't failed or queued says "Unknown action retry".** It is refused, as it should be, but the message is misleading. | A clear message, with a test. |
| **Arc and Chrome Canary cookies rely on a private yt-dlp function.** `_register_extra_browsers` wraps `yt_dlp.cookies._get_chromium_based_browser_settings`; if yt-dlp renames it, those browsers silently disappear from Settings. | A check in the test suite after each yt-dlp update; `cookies.txt` import still works. |
| **The Mac app's menu-bar icon is only checked by hand.** `mac_app.py --smoke` covers the window, the page and creating the icon, not clicking its menu. | A UI test with the Accessibility API, or keep checking by hand after changes. |
| **The total speed limit is per connection.** A fragmented stream (HLS/DASH) downloads 4 fragments at once, each allowed the full share, so it can briefly go over the total. | Divide the share by `concurrent_fragment_downloads` for fragmented formats. |
| **Logins don't survive a restart.** Password sessions are kept in memory, so after a restart (or a yt-dlp update) every browser logs in again. | Signed session cookies with a key stored in `data/`. |
| **Long errors are cut to one line in the playlist list** (rows have a fixed height for virtual scrolling). The full text is in the tooltip and in Details. | An expandable row, like the dashboard's. |

## Ideas

- Per-playlist schedule and speed limit (today both are global).
- Notifications for queue-wide pauses (rate limiting, disk full), not only finished playlists and failed videos.
- A "Downloaded" badge on the Mac app's Dock icon.

## Done

### 2026-10-18: complete tests

- **pytest:**
  - `tests/conftest.py` and `tests/helpers.py`: a scratch library per module (refuses `data/`), ffmpeg clips, local media and flaky servers, a fake extractor, an in-process API client;
  - all 306 old checks kept; any test runs alone (`pytest -k`), in parallel (`-n auto`) in about 20 s instead of 4–6 minutes.
- **New tests:**
  - every API route (token, Host guard, password and its limit, errors, route order);
  - the status flow and every re-queue path;
  - the download invariants (archive only after verification, never twice at once, crash and damaged-database recovery, deletes by `[id]`);
  - settings across a restart;
  - the connection monitor.

  299 Python tests, coverage 89% with a floor of 88%.
- **Page logic:** Vitest (`tests/ui/`, the 31 checks of `test_ui.mjs`).
- **End-to-end:** Playwright (`tests/e2e/`), driving the real page and server on a fake YouTube. It covers add, range download, pause and resume, pause all, settings after a reload, Save options, the bot-check banner, and export and import. Elements are found by role and label.
- **One command:** `scripts/test.sh` (`--fast` for a quick run), the `run-tests` skill, and GitHub Actions on macOS and Ubuntu plus a Windows Python job.
- **Found on the way:** `Manager()` starts its workers before the server sets the network level, and the default ("checking") lets them start downloading. That is harmless in the app, but the tests now hold the workers until it is set (`new_manager`).

### 2026-10-18: YouTube's bot check, cookies, settings

From the 2026-10-17 incident in `data/app.log` (the "confirm you're not a bot" block from 20:12 and the Chrome cookie error at 23:38):

- **Rate limiting:**
  - downloads blocked together count as one strike, so the pause no longer jumps from 4 to 30 minutes in a second;
  - the pause and strike count survive restarts (`data/queue_state.json`);
  - rate-limit errors have their own counter, and a block on every video keeps the queue paused instead of failing videos one by one;
  - after a pause one video goes first;
  - new cookies or a new proxy end the pause at once;
  - after three waves the banner says what to change.
- **Cookies:**
  - Settings lists the browsers and profiles found on this computer (adds Arc and Chrome Canary; flags Full Disk Access);
  - **Test cookies**;
  - **Import cookies…** for a `cookies.txt` (used instead of the browser, a private copy per download);
  - a cookie error pauses the queue once with the reason;
  - the bot-check banner offers **Import cookies…** and **Use cookies from browser…**.
- **Desktop notifications** are a saved setting (they were kept in browser storage, lost in the Mac app and refused by Safari without a click). The Mac app keeps its page storage. A test saves and reloads every setting.
- **Save options** button for a playlist; auto-download of new videos uses the saved options.
- No more "ERROR: ERROR:" in the Details log.

### 2026-10-17: roadmap phase 5

Mac app (window and menu-bar icon); dashboard with history and stats, speed chart, time left and free disk space; queue control (reorder, per-video pause, try now, pause all, retry all failed); subscriptions; SponsorBlock; subtitle languages; parts of videos; adding many links; download schedule and shared speed limit; per-video file actions; remove with files; `.m3u` export; yt-dlp update and restart; export and import of all data; live updates over SSE; virtual scrolling; ES-module front end; optional password for network mode; Windows launchers.

### 2026-10-16: reliability and security

Token and Host checks on the API; validated settings; stable file numbers and Unavailable videos on refresh; error kinds from exception types; resume and backoff on weak internet with queue-wide pauses; every file checked (stream sizes, length, decoding the end) before it counts as downloaded, with re-merge from kept streams; Check files and Clean up leftovers; the first Activity dashboard; SQLite library with backups and recovery; app log and Details; single instance and per-video locks.
