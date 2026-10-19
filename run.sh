#!/usr/bin/env bash
# Launch YT Downloader: sets up the virtualenv on first run, then opens http://127.0.0.1:8765
#   ./run.sh [--port 8765] [--host 127.0.0.1] [--no-browser] [--password …]
#   ./run.sh --dev   front-end development: the backend plus `next dev` (http://localhost:3000); needs Node.js
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

if [ "${1:-}" = "--dev" ]; then
  shift
  command -v npm >/dev/null || { echo "Dev mode needs Node.js (npm)."; exit 1; }
  [ -d node_modules ] || npm ci
  port=8765
  for ((i = 1; i <= $#; i++)); do [ "${!i}" = "--port" ] && { j=$((i + 1)); port=${!j}; }; done
  .venv/bin/python server.py --dev --no-browser "$@" &
  backend=$!
  NEXT_BASE_PATH="" YTDL_BACKEND="http://127.0.0.1:$port" npx next dev frontend --port 3000 &
  front=$!
  trap 'kill $backend $front 2>/dev/null; wait 2>/dev/null' EXIT INT TERM   # Ctrl+C stops both
  echo "Dev mode: page at http://localhost:3000 (edits reload it), API at http://127.0.0.1:$port"
  wait $backend
  exit
fi

echo "Starting YT Downloader at http://127.0.0.1:8765  (press Ctrl+C to stop)"
exec .venv/bin/python server.py "$@"
