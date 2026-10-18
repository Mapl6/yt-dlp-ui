# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local web app (FastAPI + yt-dlp, vanilla JS front end) for downloading YouTube videos, playlists and channels. Playlists are saved to a library first; the user then picks which videos to download. Its main design goal is reliability on weak or interrupted internet: nothing is lost and no damaged file ever counts as downloaded.

## Commands

```bash
./run.sh                                   # creates .venv, updates deps (non-fatal offline), opens http://127.0.0.1:8765
./run-mac.sh                               # optional Mac app (pywebview window + menu-bar icon); run.bat / run.ps1 on Windows
.venv/bin/python server.py --port 8799 --no-browser   # run a second instance for testing (see "Testing safely")

.venv/bin/python tests/test_core.py        # merge/numbering, format opts, error kinds, SQLite, logs, API security
.venv/bin/python tests/test_reliability.py # flaky-server resume, offline gating, verification, locks, DB recovery
.venv/bin/python tests/test_health.py      # Check files, Clean up, stream-size check, re-merge from kept streams
.venv/bin/python tests/test_features.py    # queue control, event log, schedule, speed limits, file actions, SponsorBlock, parts, sync
node tests/test_ui.mjs                     # page logic: range selector, filters/sorting, formatters, stats buckets
```

Tests are plain scripts (no pytest): each prints `PASS`/`FAIL` lines and exits non-zero on failure. They need `ffmpeg`/`ffprobe` but no internet: they use local HTTP servers and, in `test_health.py`, a patched `YoutubeDL.extract_info` that serves a fabricated video with separate video and audio formats. A test file takes the repo root as an optional argument. To run one check, comment out the others in the script; there is no test selector. Tracebacks about `BrokenPipeError`/`ConnectionResetError` in the output are expected: the flaky server drops connections on purpose.

Syntax checks: `node --check` on each `static/js/*.js`, `.venv/bin/python -m py_compile manager.py server.py netcheck.py mac_app.py`. `mac_app.py --smoke` (needs pywebview, e.g. a scratch venv with `requirements-mac.txt`) opens the window, checks the page and quits.

## Project rules and skills (`.claude/`)

- `rules/safety.md` (always loaded): never run against `data/` or the user's download folders. This repo has already had a test server write into the user's real folder.
- Path-scoped rules load with the matching files:
  - `rules/download-core.md`: the invariants in `manager.py`/`server.py`;
  - `rules/testing.md`: test-script conventions;
  - `rules/frontend.md`: `static/` conventions.
- Skills:
  - `safe-test-server`: run the app on a sanitized copy of the library;
  - `run-tests`: all suites plus a summary;
  - `verify-downloads`: a read-only health report on real downloaded files.

## Architecture

**`manager.py`**: everything that matters. `server.py` is a thin FastAPI layer; `netcheck.py` is the connection monitor.

- **Data model.** `Manager.jobs` is an in-memory dict of playlists ("jobs"), each with `items` (videos). Item status flow: `new → queued → downloading → processing → done | failed`; `skipped` means already in the folder's `.downloaded.txt` archive and counts as downloaded. The item fields are easy to mix up:
  - `index`: current position in the playlist. It changes on every refresh, and the UI and range selection use it.
  - `number`: the file-name prefix. It's assigned once and never changed, so files on disk keep their names; new videos get the next free number (`_merge`).
  - `removed`: the video left the playlist or is private/deleted. Such videos are shown as "Unavailable" and excluded from selection and from the "complete" count.
  - `file_problem`: set by "Check files". `_forget_file` removes the video's archive line when it is re-queued; every re-queue goes through `_requeue`, which calls it.
  - `priority` (queue order, set by move/drag), `held` (this one video paused), `step` (processing sub-step), `fmt` (format actually downloaded), `history` (last attempts with their error kind), `media_duration` (verified length; shorter than `duration` for a part or after SponsorBlock cuts — Check files compares with it via `_expected_duration`).
  - Retry counters: `attempts` (other errors), `net_attempts` (connection problems), `rate_attempts` + `rate_first` (rate limiting). They are separate on purpose; `_requeue` and success reset all three.
- **Revisions drive everything.** Each job has `rev`; `_touch(job, item)` bumps it and stamps the item, and bumps `Manager.version`, which the live stream (`/api/stream`) watches. The page fetches `/api/jobs/{id}/items?since=rev`. `save_state` writes only rows with `rev > _saved_rev[job]`. Any change to an item must go through `_set()`/`_touch()` under `self.cond` (a `Condition` over an `RLock`), or it won't reach the UI or disk.
- **Live index.** `_live` holds only queued and active items; `_touch(job, item)` updates that entry, `_touch(job)` (no item) re-indexes the whole job — use it after bulk `it.update(..., rev=...)` loops. `_queue_order()` sorts it by `(-priority, job.created, index)` and is cached until membership or a priority changes (`_set(priority=…)` resets it). `_next_item`, `activity()` and `total_speed()` read only this index.
- **Persistence.** `Store` is SQLite (`data/library.db`, WAL, `synchronous=FULL`), one row per job and per item holding JSON. It's backed up to `library.backup.db` at startup and every 10 minutes; a damaged database is moved aside (never deleted) and the backup loaded. A legacy `state.json` is migrated once, then renamed to `.migrated`. Settings go to `data/settings.json` via `_atomic_write`, after `_coerce_settings` validates them (new settings only need a default in `DEFAULT_SETTINGS`).
- **Event log.** `events` table in the same database: one row per finished or failed download (`_event()`, written outside the lock), with size and time. On the first start with it (`PRAGMA user_version` 0) earlier downloads are added once from their files (`_backfill_events`). It feeds "Recently finished" (`history()`, paging, `clear_history` only hides rows), the 24-hour counts and `stats()`. `Store` serializes all use of its one connection with `Store._lock`.
- **Scheduler.** 8 worker threads; `_next_item` picks queued items, respecting:
  - `hold`: queue-wide pause `{reason, until, kind, since}`, kind `ratelimit | cookies | disk | other`, set by `_hold_queue` (a longer pause replaces a shorter one) and ended by `_clear_hold`. It is saved with `rate_strikes`, `rate_wave`, `rate_probe` and `last_success` in `data/queue_state.json` (`_save_queue_state`) and restored on start, so a restart doesn't hit YouTube again at once;
  - `rate_probe`: after rate limiting `_limit()` is 1 until a download is verified;
  - `next_start`: the "pause between videos" setting;
  - the download schedule (`_in_schedule`, using `self.clock`);
  - `held` items and paused playlists;
  - the connection level from `NetMonitor`: offline means wait, YouTube blocked means only YouTube items wait, weak or fair means fewer parallel downloads (`_limit`);
  - `inflight`: never the same `(folder, video)` twice at once.
- **Download pipeline** (`_download` → `_download_locked`):
  1. Folder check; never recreate `/Volumes/<drive>` for an unplugged drive.
  2. Archive check.
  3. Free-space check.
  4. Per-video `fcntl` file lock in `<outdir>/.ytdl-locks/`, so two app copies can't write the same `.part` file.
  5. yt-dlp with `keepvideo` (video mode).
  6. `_check_part_sizes`: http(s) streams must match the reported `filesize` exactly.
  7. `_verify_file`: ffprobe duration plus decoding the last 5 seconds.
  8. If the merged file fails, re-merge once from the kept streams.
  9. Delete the streams, then append to the archive.

  **The archive line is written only after verification.** Keep that invariant. A part of a video (`options.section`, `download_ranges`) gets its own file name and never touches the archive. SponsorBlock goes through `_SafeSponsorBlock`, so an API failure never fails a download; the expected length comes from `requested_downloads[0]["duration"]` (yt-dlp updates it after cutting). Running `YoutubeDL` objects are registered in `_ydls`; `_rebalance()` shares the total speed limit by setting their `params["ratelimit"]` mid-download. `pp_hook` maps postprocessors to `step` labels (`PP_STEPS`).
- **Failure handling.** `_error_kind` returns disk, permanent, ratelimit, network or other. It checks exception types and HTTP status first, walking yt-dlp's `exc_info`/`cause` chain via `_exc_chain`; regex on the message text is only a fallback.
  - network: retried with backoff up to 40 times (`net_attempts`), separate from `attempts`;
  - disk: queue-wide `hold`;
  - ratelimit: queue-wide `hold` that doubles per *wave* (1 min … 30 min). A failure of a download that started before the last strike (`rate_wave`) doesn't add one. The video stays queued; it fails at `MAX_RATE_STRIKES` only if another download succeeded after its first block (`last_success > rate_first`). From `RATE_BLOCK_STRIKES` waves the reason tells the user to change cookies or VPN;
  - cookies (`CookieLoadError` or `COOKIE_ERRORS`): one `hold` of kind `cookies` with `_cookie_error_text`, no attempt used;
  - permanent: fails immediately.

  `update_settings` calls `_identity_changed` when `cookies_browser`, `cookies_profile` or `proxy` change, as do cookie import and removal: it ends a `ratelimit`/`cookies` hold and the probe.

  `network_changed` clears backoff when the connection returns.
- **Cookies.** `cookie_sources()` lists the browsers and profiles found on this computer (status `ok`, `no-profiles`, `blocked` = needs Full Disk Access); `check_cookies()` reads one like a download would. `_register_extra_browsers()` teaches yt-dlp Arc and Chrome Canary (macOS). An imported `cookies.txt` lives in `data/cookies.txt` (mode 600) and replaces the browser's cookies. Every `YoutubeDL` is made through `_ytdl(opts)`, which gives it its own temporary copy of that file, because yt-dlp writes cookies back when it closes.
- **File health.** `start_check` → `_run_check` verifies files in a thread pool (`CHECK_WORKERS`). A file ffprobe can't open is only called damaged if `_decodes_fully` also fails. Damaged files are renamed to `.damaged`, never deleted. Missing files go to `failed` with `file_problem`. Good untracked files in the folder are adopted as `skipped`. `cleanup()` only touches files whose name has the `[video id]` of a non-busy item in this job, and always previews first (GET = dry run, POST = delete).
- **Other background threads:** `_syncer` (automatic playlist checks, `sync_due`), `_sampler` (speed history for the chart and the queue ETA), `_import` (adding many links), `_run_update` (yt-dlp update). User file actions (`item_action` reveal/play/delete-file/mark-done, `remove(delete_files=True)`, `export_m3u`) only ever take paths from the library, never from the request, and only delete files whose last `[id]` tag belongs to that video.
- **Single instance.** `Manager()` takes an `fcntl` (or, on Windows, `msvcrt`) lock on `data/.lock` (`AlreadyRunning`). `server.py` binds-tests the port before `start_engine()`, so a second copy exits before any worker starts.

**`server.py`**:

- `guard` middleware rejects any `Host` other than `127.0.0.1`/`localhost`/`[::1]` (DNS rebinding) unless started with a non-loopback `--host`.
- `/api/*` requires `X-Token`, a per-process token injected into `index.html` at `GET /`; only `/api/stream` (Server-Sent Events) takes it as `?token=`. The page reloads itself (rate-limited) on 401/403.
- With `--password` / `YTDL_PASSWORD`, every path except `/login` needs the session cookie set by `POST /login` (in-memory sessions, 5 wrong tries per minute per address).
- `/api/stream` pushes `{overview, activity}` when `manager.version` changes (at most every 0.5 s, at least every 2 s) and ends when `runtime["server"].should_exit` is set, so Ctrl+C isn't held up.
- `POST /api/restart` re-execs the process (`runtime["can_restart"]`, only when started as `python server.py`); after a restart the port check retries with `SO_REUSEADDR`.
- Specific job routes (`/check`, `/cleanup`, `/remove`, `/m3u`, `/estimate`, `/sync`, `/options`) must be declared before the catch-all `POST /api/jobs/{job_id}/{action}`.
- Cookies: `GET /api/cookies/sources`, `POST /api/cookies/test`, `POST`/`DELETE /api/cookies/file`. `PUT /api/jobs/{id}/options` is the playlist's Save button (`set_options`; `queue()` stores options through the same `_apply_options`).

**`netcheck.py`**: `NetMonitor` probes Google and YouTube `generate_204` every 10 s through the configured proxy. It classifies the connection as excellent, good, fair, weak, blocked or offline, and calls `on_change(old, new)`.

**`static/js/`**: ES modules, no framework or build step (see `rules/frontend.md`). `main.js` receives updates from the SSE stream (polling as fallback) and hands them to `library.js` (one view per playlist card; the body comes from `<template id="tpl-body">`, built lazily; rows drawn by the `VList` virtual list from `items?since=`), `dashboard.js` (+ `stats.js`, `charts.js`) and `netpanel.js`. HTML is only replaced when it changes, so clicks aren't lost mid-update.

**`mac_app.py`** (optional): runs the same server in a thread and shows it in a pywebview window. It patches pywebview's Cocoa app delegate so ⌘Q quits and the Dock icon reopens the hidden window, and adds an `NSStatusItem` menu. `scripts/make_mac_app.sh` builds a thin `.app` launcher that runs `run-mac.sh`.

## Docs

- `README.md`: user-facing behaviour. Update it when behaviour changes.
- `docs/IMPROVEMENTS.md`: the roadmap. Open work goes at the top; finished work is a short dated summary at the bottom.
