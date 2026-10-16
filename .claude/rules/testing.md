---
paths:
  - "tests/**"
---

# Test conventions

- **Tests are plain scripts, not pytest.** Each file defines `check(name, cond, extra)`, prints `PASS`/`FAIL` lines, ends with `ALL PASSED`/`SOME FAILED` and exits non-zero on failure. Keep that style; don't add pytest or other dependencies.
- **Set up isolation at the very top, before `import manager as M`:**
  - point `YTDL_DATA_DIR` at a fresh `tempfile.mkdtemp(...)`, because `manager.py` reads `DATA_DIR` at import time;
  - insert the repo root into `sys.path`.
- **One `Manager` per data directory.** The instance lock refuses a second one, and that is tested. For another library in the same process, reassign the `M.DATA_DIR`/`M.DB_FILE`/`M.DB_BACKUP`/`M.LEGACY_*`/`M.SETTINGS_FILE` globals first, as the migration test in `test_core.py` does.
- **Keep workers idle with `mgr.net_level = lambda: "offline"`** unless the test is about downloading. Set it back to `"good"` only for the part that downloads.
- **No internet.**
  - Use a local `http.server` (see `Flaky` in `test_reliability.py`) for flaky or resumable transfers.
  - Use the patched `YoutubeDL.extract_info` + `process_ie_result` in `test_health.py` for merged video+audio downloads.
  - Generate media with `ffmpeg -f lavfi` (testsrc/sine).
- **Wait with polling helpers** (`wait`, `wait_status`) and generous timeouts, never fixed sleeps for completion.
- **Run all three suites before claiming something works** (the `run-tests` skill). Some checks depend on timing; run them twice when touching the scheduler or locks.
