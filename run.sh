#!/usr/bin/env bash
# Launch YT Downloader: sets up the virtualenv on first run, then opens http://127.0.0.1:8765
set -e
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null; then echo "ffmpeg is required: brew install ffmpeg"; exit 1; fi

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv -q .venv; else python3 -m venv .venv; fi
fi

# Keep yt-dlp current — YouTube changes often and old versions break.
# A failed update (e.g. offline) must not stop the app from starting.
echo "Checking dependencies…"
if command -v uv >/dev/null; then
  uv pip install -q -p .venv/bin/python --upgrade -r requirements.txt || echo "Couldn't update dependencies (offline?) — starting anyway."
else
  .venv/bin/python -m ensurepip -q >/dev/null 2>&1 || true
  .venv/bin/python -m pip install -q --upgrade -r requirements.txt || echo "Couldn't update dependencies (offline?) — starting anyway."
fi

echo "Starting YT Downloader at http://127.0.0.1:8765  (press Ctrl+C to stop)"
exec .venv/bin/python server.py "$@"
