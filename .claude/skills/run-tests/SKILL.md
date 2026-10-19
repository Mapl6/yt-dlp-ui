---
name: run-tests
description: Run all YT Downloader tests (syntax, pytest with coverage, Vitest page logic, Playwright end-to-end) and report a pass/fail summary. Use after any change to manager.py, server.py, netcheck.py, static/ or tests/, and before saying a change works.
---

# Run tests

Everything runs offline once the test tools are installed and uses its own scratch directories and port 8798, so nothing touches `data/`, real downloads or the user's app on 8765. Needs `ffmpeg`/`ffprobe` and Node.js. A full run takes about a minute (plus a failed browser download when offline).

## Steps

1. Run everything, saving the output to the scratchpad:
   ```bash
   scripts/test.sh > "<scratchpad>/tests.log" 2>&1; echo "exit $?"
   ```
   Use `scripts/test.sh --fast` only for a quick check while working: it skips real downloads, server processes and the browser tests.
2. Read the counts from the log:
   ```bash
   grep -E '^(PASS|FAIL)  |passed|failed|TOTAL|Tests  ' "<scratchpad>/tests.log"
   ```
3. For a failure, show it:
   - pytest: `grep -E '^(FAILED|ERROR)|^E  ' -B2 "<scratchpad>/tests.log"`. Rerun one test with `.venv/bin/python -m pytest "tests/test_x.py::test_name" -x`.
   - Playwright: the error and `test-results/<test>/error-context.md` (a snapshot of the page).

   Ignore tracebacks with `BrokenPipeError` / `ConnectionResetError` from `socketserver`/`http.server`: the flaky test server causes them on purpose.
4. If you changed the scheduler, locks, retries or timing, run the Python tests a second time:
   `.venv/bin/python -m pytest -q -n auto --dist loadfile`.
5. If Playwright can't download its browser (offline), the script uses the installed Google Chrome (`E2E_CHANNEL=chrome`). Say so in the report.

## Report

Give one line per part (`pytest: 299 passed, coverage 89%`, `vitest: 31 passed`, `playwright: 9 passed`) and the total. For each failure, say whether the app is wrong or the test is outdated, with evidence. Never say "all tests pass" without the counts from this run.
