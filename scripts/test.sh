#!/usr/bin/env bash
# Run every test: syntax checks, pytest (with coverage), Vitest (page logic), Playwright (the page in a browser).
#   scripts/test.sh          everything
#   scripts/test.sh --fast   skip real downloads, real server processes and the browser tests
# Offline once the dependencies are installed; needs ffmpeg. Never touches data/ or your download folders.
set -u
cd "$(dirname "$0")/.."
fast=0
[ "${1:-}" = "--fast" ] && fast=1
if ! command -v ffmpeg >/dev/null; then echo "ffmpeg is required: brew install ffmpeg"; exit 1; fi
py=.venv/bin/python

# ---- dependencies
if [ ! -x "$py" ]; then
  if command -v uv >/dev/null; then uv venv -q .venv; else python3 -m venv .venv; fi
fi
if ! "$py" -c "import pytest, pytest_cov, xdist, httpx2" 2>/dev/null; then
  echo "Installing test dependencies…"
  if command -v uv >/dev/null; then uv pip install -q -p "$py" -r requirements-dev.txt
  else "$py" -m ensurepip -q >/dev/null 2>&1; "$py" -m pip install -q -r requirements-dev.txt; fi || exit 1
fi
if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
  echo "Installing Vitest and Playwright…"
  npm ci --silent || exit 1
fi

summary=()
failed=0
step() {   # step <name> <command…>
  local name=$1; shift
  echo; echo "==== $name"
  if "$@"; then summary+=("PASS  $name"); else summary+=("FAIL  $name"); failed=1; fi
}

syntax() {
  "$py" -m compileall -q backend server.py mac_app.py tests || return 1
  local f ok=0
  for f in static/js/*.js tests/ui/*.mjs tests/e2e/*.mjs; do node --check "$f" || ok=1; done
  return $ok
}

browser() {   # Playwright's own Chromium, or the installed Google Chrome when it can't be downloaded
  [ -n "${E2E_CHANNEL:-}" ] && return 0
  npx playwright install chromium >/dev/null 2>&1 && return 0
  if [ -d "/Applications/Google Chrome.app" ] || command -v google-chrome >/dev/null; then
    echo "Couldn't download Playwright's Chromium: using the installed Google Chrome."
    export E2E_CHANNEL=chrome
    return 0
  fi
  echo "No browser for Playwright: run 'npx playwright install chromium'"; return 1
}

step "syntax" syntax
if [ $fast = 1 ]; then
  step "pytest (fast)" "$py" -m pytest -q -n auto --dist loadfile -m "not slow and not server"
else
  step "pytest" "$py" -m pytest -q -n auto --dist loadfile --cov --cov-report=term
fi
step "vitest" npx vitest run
if [ $fast = 0 ]; then
  if browser; then
    lsof -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 && echo "(Your own app is running on 8765; the tests use their own port and library.)"
    step "playwright" npx playwright test
  else
    summary+=("FAIL  playwright (no browser)"); failed=1
  fi
fi

echo; echo "==== summary"
printf '%s\n' "${summary[@]}"
exit $failed
