# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local web app (FastAPI + yt-dlp, vanilla JS front end) for downloading YouTube videos, playlists and channels. Playlists are saved to a library first; the user then picks which videos to download. Its main design goal is reliability on weak or interrupted internet: nothing is lost and no damaged file ever counts as downloaded.

## Commands

```bash
./run.sh                                   # creates .venv, updates deps (non-fatal offline), opens http://127.0.0.1:8765
.venv/bin/python server.py --port 8799 --no-browser   # run a second instance for testing (see "Testing safely")

.venv/bin/python tests/test_core.py        # merge/numbering, format opts, error kinds, SQLite, logs, API security
.venv/bin/python tests/test_reliability.py # flaky-server resume, offline gating, verification, locks, DB recovery
.venv/bin/python tests/test_health.py      # Check files, Clean up, stream-size check, re-merge from kept streams
```

Tests are plain scripts (no pytest): each prints `PASS`/`FAIL` lines and exits non-zero on failure. They need `ffmpeg`/`ffprobe` but no internet: they use local HTTP servers and, in `test_health.py`, a patched `YoutubeDL.extract_info` that serves a fabricated video with separate video and audio formats. A test file takes the repo root as an optional argument. To run one check, comment out the others in the script; there is no test selector. Tracebacks about `BrokenPipeError`/`ConnectionResetError` in the output are expected: the flaky server drops connections on purpose.

Syntax checks: `node --check static/app.js`, `.venv/bin/python -m py_compile manager.py server.py netcheck.py`.

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
  - `file_problem`: set by "Check files". `_forget_file` removes the video's archive line when it is re-queued.
- **Revisions drive everything.** Each job has `rev`; `_touch(job, item)` bumps it and stamps the item. The UI polls `/api/overview` every second and fetches `/api/jobs/{id}/items?since=rev`. `save_state` writes only rows with `rev > _saved_rev[job]`. Any change to an item must go through `_set()`/`_touch()` under `self.cond` (a `Condition` over an `RLock`), or it won't reach the UI or disk.
- **Persistence.** `Store` is SQLite (`data/library.db`, WAL, `synchronous=FULL`), one row per job and per item holding JSON. It's backed up to `library.backup.db` at startup and every 10 minutes; a damaged database is moved aside (never deleted) and the backup loaded. A legacy `state.json` is migrated once, then renamed to `.migrated`. Settings go to `data/settings.json` via `_atomic_write`, after `_coerce_settings` validates them.
- **Scheduler.** 8 worker threads; `_next_item` picks queued items, respecting:
  - `hold`: queue-wide pauses for disk full, rate limiting, low disk;
  - `next_start`: the "pause between videos" setting;
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

  **The archive line is written only after verification.** Keep that invariant.
- **Failure handling.** `_error_kind` returns disk, permanent, ratelimit, network or other. It checks exception types and HTTP status first, walking yt-dlp's `exc_info`/`cause` chain via `_exc_chain`; regex on the message text is only a fallback.
  - network: retried with backoff up to 40 times (`net_attempts`), separate from `attempts`;
  - ratelimit and disk: queue-wide `hold`;
  - permanent: fails immediately.

  `network_changed` clears backoff when the connection returns.
- **File health.** `start_check` → `_run_check` verifies files in a thread pool (`CHECK_WORKERS`). Damaged files are renamed to `.damaged`, never deleted. Missing files go to `failed` with `file_problem`. Good untracked files in the folder are adopted as `skipped`. `cleanup()` only touches files whose name has the `[video id]` of a non-busy item in this job, and always previews first (GET = dry run, POST = delete).
- **Single instance.** `Manager()` takes an `fcntl` lock on `data/.lock` (`AlreadyRunning`). `server.py` binds-tests the port before `start_engine()`, so a second copy exits before any worker starts.

**`server.py`**:

- `guard` middleware rejects any `Host` other than `127.0.0.1`/`localhost`/`[::1]` (DNS rebinding) unless started with a non-loopback `--host`.
- `/api/*` requires `X-Token`, a per-process token injected into `index.html` at `GET /`. The page reloads itself (rate-limited) on 403.
- Specific job routes (`/check`, `/cleanup`) must be declared before the catch-all `POST /api/jobs/{job_id}/{action}`.

**`netcheck.py`**: `NetMonitor` probes Google and YouTube `generate_204` every 10 s through the configured proxy. It classifies the connection as excellent, good, fair, weak, blocked or offline, and calls `on_change(old, new)`.

**`static/app.js`**: no framework. One view per job card; the card body comes from `<template id="tpl-body">` and is built lazily when opened. Rows are updated in place from `items?since=`. Head and button HTML is only replaced when it changes, so clicks aren't lost mid-poll.

## Docs

- `README.md`: user-facing behaviour. Update it when behaviour changes.
- `docs/IMPROVEMENTS.md`: the roadmap. Open work goes at the top; finished work is a short dated summary at the bottom.
