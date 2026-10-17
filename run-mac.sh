#!/usr/bin/env bash
# Launch YT Downloader as a Mac app (its own window + menu-bar icon). Same library as ./run.sh.
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"     # Homebrew ffmpeg when started from Finder
if ! command -v ffmpeg >/dev/null; then
  osascript -e 'display alert "YT Downloader needs ffmpeg" message "Install it with: brew install ffmpeg"' >/dev/null 2>&1 || true
  echo "ffmpeg is required: brew install ffmpeg"; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv -q .venv; else python3 -m venv .venv; fi
fi

# Keep yt-dlp current; a failed update (e.g. offline) must not stop the app from starting.
echo "Checking dependencies…"
if command -v uv >/dev/null; then
  uv pip install -q -p .venv/bin/python --upgrade -r requirements-mac.txt || echo "Couldn't update dependencies (offline?) — starting anyway."
else
  .venv/bin/python -m ensurepip -q >/dev/null 2>&1 || true
  .venv/bin/python -m pip install -q --upgrade -r requirements-mac.txt || echo "Couldn't update dependencies (offline?) — starting anyway."
fi

exec .venv/bin/python mac_app.py "$@"
