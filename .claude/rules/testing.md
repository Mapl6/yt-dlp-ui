---
paths:
  - "tests/**"
  - "pyproject.toml"
  - "package.json"
  - "playwright.config.mjs"
  - "vitest.config.mjs"
  - "scripts/test.sh"
---

# Test conventions

- **Python tests are pytest modules** (`tests/test_*.py`). Write plain `assert`s with a message that says what should hold (`assert cond, "check: damaged file -> failed"`). Dev dependencies are pinned in `requirements-dev.txt`; `scripts/test.sh` installs them. The app itself must keep needing only `requirements.txt`.
- **Isolation comes from `tests/conftest.py`, which must stay the first import:**
  - it points `YTDL_DATA_DIR` at a scratch directory before `manager` is imported (`DATA_DIR` is read at import);
  - it refuses to run if that would be `<repo>/data`.

  Never import `manager` from a test file before pytest has loaded `conftest.py`, and never set paths to `data/`.
- **One `Manager` per module:** the `mgr` fixture (module scope), on its own scratch library, with an `out` folder and a `net` switch.
  - Managers can't be stopped (their threads run forever), so don't create one per test.
  - For a restart or a second library use `make_manager(dir)`. It switches the path globals with `use_data_dir` and restores them afterwards.
  - Build managers through `new_manager()` (in `helpers.py`), never `M.Manager()` directly. It holds the workers until `net_level` is set; otherwise they start downloading at once.
- **Keep workers idle with `net.level = "offline"`** (the default). Set `"good"` only around the part that downloads, and set it back in `finally`.
- **Each test sets up what it needs** (its own playlist via `make_job`, or a module fixture such as `downloaded` or `lib2` that builds it), so `pytest -k <name>` runs any test alone. Clean up queued items with `mgr.action(job_id, "stop")` in `finally`.
- **Use monkeypatch for every patch** (`M._verify_file`, `M.inspect_url`, `M._open_path`, class attributes), so nothing leaks into the next test.
- **No internet:**
  - `media` (ffmpeg clips) and `media_server` (Range, `throttle`, `gets`);
  - `flaky_server` (drops connections, counts Range resumes);
  - `fake` (a `FakeExtractor` for `test://<id>` videos, with `fail_ids`, `bot_ids`, `audio_extra`, `titles`, `extra`);
  - `api` (a FastAPI `TestClient` on `127.0.0.1` with the token and a NetMonitor stub).

  Real `server.py` processes (startup and port checks) are marked `server`. Real downloads are marked `slow`; `scripts/test.sh --fast` skips both.
- **Wait with `wait()`/`wait_status()`** and generous timeouts, never fixed sleeps for completion.
- **Coverage has a floor** (`fail_under` in `pyproject.toml`). Don't lower it; raise it when you close gaps.
- **Page logic** (`frontend/static/js/util.js`, `filters.js`, the `buckets` export of `stats.js`) is tested with Vitest in `tests/ui/*.test.mjs`. Keep those modules free of DOM access at import time.
- **End-to-end tests** (`tests/e2e/*.spec.mjs`, Playwright):
  - they run the real page and `server.py` through `tests/e2e/fake_app.py`, on port 8798 (never 8765) with a scratch library and a fake YouTube (`https://e2e.test/list/<name>/<n>`, `https://e2e.test/bot/<n>`, `GET /__throttle/on|off` on the media port);
  - the tests run in order on one app;
  - find elements by role, label and text, not CSS classes or ids, so the same tests can check the Next.js port. The playlist card container (`.job`) is the exception.
  - `E2E_CHANNEL=chrome` uses the installed Google Chrome when Playwright's own browser can't be downloaded.
- **Run everything before claiming something works:** `scripts/test.sh` (the `run-tests` skill). Run the Python suite twice when touching the scheduler, locks or timing.
