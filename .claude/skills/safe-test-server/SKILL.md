---
name: safe-test-server
description: Start a YT Downloader test server on a sanitized copy of the user's library (or an empty one) that cannot download into or modify the user's real files. Use whenever you need to run the app to check a change, try the UI, or reproduce a bug.
---

# Safe test server

A plain copy of `data/` is **not** safe: its queued videos start downloading immediately into the user's real `~/Downloads/YT Downloader/...` folder, possibly while the user's own app writes the same files. This skill prevents that.

## Steps

1. Choose scratch paths under your scratchpad directory (not `/tmp` unless told otherwise):
   - `DATA` = `<scratchpad>/data.test`
   - `OUT` = `<scratchpad>/out.test`
   - `PORT` = a free port other than 8765, e.g. `8799`.

2. Build the data directory:
   - **With the user's playlists:** run
     `.venv/bin/python .claude/skills/safe-test-server/scripts/make_safe_copy.py "$DATA" "$OUT"`.
     It reads `data/` read-only. In the copy, queued and active videos are reset to "new", every folder points at `$OUT`, and file paths are cleared.
   - **Empty library:** `mkdir -p "$DATA"` and write `{"output_dir": "$OUT"}` to `$DATA/settings.json`.

3. Start the server in the background:
   `YTDL_DATA_DIR="$DATA" .venv/bin/python server.py --port $PORT --no-browser > <scratchpad>/server.log 2>&1 &`.
   Wait until `curl -s -o /dev/null -w "%{http_code}" localhost:$PORT/` prints `200`.

4. API calls need the session token:
   `T=$(curl -s localhost:$PORT/ | sed -n 's/.*api-token" content="\([^"]*\)".*/\1/p')`, then
   `curl -s -H "X-Token: $T" localhost:$PORT/api/overview`.

5. For UI checks, drive headless Chrome over the DevTools protocol:
   - start `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --remote-debugging-port=9333 --user-data-dir=<scratchpad>/chrome-prof about:blank`;
   - connect with Node's built-in `WebSocket`;
   - use `Runtime.evaluate` for clicks and reading the DOM, `Page.captureScreenshot` for screenshots, and collect `Runtime.exceptionThrown` as errors.

   The Playwright MCP browser may be busy in another session; this path always works.

6. **Clean up:**
   - `pkill -f "server.py --port $PORT"`;
   - `pkill -f "remote-debugging-port=9333"`, if you started Chrome.

## Don't

- Point `YTDL_DATA_DIR` at `data/`, or start the server without it.
- Put real files into `$OUT` and press "Check files" or "Clean up": those actions rename and delete files.
- Use port 8765: that is the user's own app.
