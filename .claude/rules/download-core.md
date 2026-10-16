---
paths:
  - "manager.py"
  - "netcheck.py"
  - "server.py"
---

# Download core invariants

These hold the reliability guarantees together. Breaking one silently loses or corrupts files.

- **Every item change goes through `self._set(job, item, ...)`,** or `item.update(...)` plus `self._touch(job, item)` inside `with self.cond:`. Without the rev bump, the change never reaches the UI (`items?since=`) or the database (`save_state` writes only `rev > _saved_rev`).
- **A video becomes `done` and enters `.downloaded.txt` only after `_check_part_sizes` and `_verify_file` pass.** Never pass yt-dlp's `download_archive` option: the app writes the archive itself, after verification.
- **Re-queuing a video with `file_problem` must call `_forget_file`,** which drops its archive line; otherwise it is "skipped" again. All three re-queue paths call it: `queue`, `action("retry")` and `item_action("retry")`.
- **Never delete user files from a check.** Damaged files are renamed with `_move_aside` (`.damaged`). Only `cleanup(dry_run=False)` deletes, after the user saw the preview.
- **Never create folders on an unplugged drive.** Use `_folder_problem(outdir)` before any `mkdir` of a download folder.
- **Classify errors with `_error_kind`:** exception types and HTTP status first, text regex as fallback. Connection problems use `net_attempts` and backoff, never `attempts`; disk-full and rate-limit errors use `_hold_queue`.
- **Keep the order of `_download` → `_download_locked`:** folder check, archive check, disk space, per-video `_FileLock`, re-check archive under the lock, yt-dlp, size check, verify, re-merge once from kept streams, delete streams, archive. The locks prevent two processes writing one `.part` file.
- **`_merge` keeps `number` (file prefix) permanent** and only moves `index`. It doesn't flag removals when a fetch looks cut short.
- **`server.py`:**
  - The engine starts in `start_engine()` only after the port bind check; don't create `Manager()` at import time.
  - Declare specific `/api/jobs/{id}/...` routes before the catch-all `POST /api/jobs/{job_id}/{action}`.
  - Every new `/api/*` route is covered by the `guard` token check automatically. Keep it that way.
- **Changing these needs tests.** Add or adjust checks in `tests/` (see the testing rule) and run all three suites.
