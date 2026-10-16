---
name: run-tests
description: Run all YT Downloader test suites (core, reliability, health) plus syntax checks and report a pass/fail summary. Use after any change to manager.py, server.py, netcheck.py, static/ or tests/, and before saying a change works.
---

# Run tests

All suites run offline and use their own temp directories, so they never touch `data/` or real downloads. They need `ffmpeg`/`ffprobe`. A full run takes about 2–4 minutes.

## Steps

1. Syntax checks:
   ```bash
   .venv/bin/python -m py_compile manager.py server.py netcheck.py && node --check static/app.js
   ```
2. Run each suite, saving output to the scratchpad and counting results:
   ```bash
   for t in core reliability health; do
     .venv/bin/python tests/test_$t.py > "<scratchpad>/test_$t.log" 2>&1; rc=$?
     echo "$t: $(grep -c '^PASS' "<scratchpad>/test_$t.log") pass, $(grep -c '^FAIL' "<scratchpad>/test_$t.log") fail (exit $rc)"
   done
   ```
3. For any failure, show the `FAIL` lines and the traceback:
   `grep -E '^FAIL|^Traceback' -A4 <log>`.
   Ignore tracebacks containing `BrokenPipeError` / `ConnectionResetError` from `socketserver`/`http.server`: the flaky test server causes them on purpose.
4. If you changed the scheduler, locks, retries or timing-related code, run the three suites a second time. Some checks depend on timing.

## Report

Give one line per suite (`core: 46 pass, 0 fail`) and the total. For each failure, say whether the app is wrong or the test is outdated, with evidence. Never say "all tests pass" without the counts from this run.
