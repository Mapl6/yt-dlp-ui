@echo off
rem Launch YT Downloader on Windows: double-click, or run "run.bat [--port 8765] [--no-browser]".
rem Sets up the virtualenv on first run, then opens http://127.0.0.1:8765
setlocal
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg is required: winget install Gyan.FFmpeg  ^(then open a new terminal^)
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  where uv >nul 2>nul && (uv venv -q .venv) || (py -3 -m venv .venv 2>nul || python -m venv .venv)
)
rem Keep yt-dlp current. A failed update (e.g. offline) must not stop the app from starting.
echo Checking dependencies...
where uv >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m ensurepip -q >nul 2>nul
  ".venv\Scripts\python.exe" -m pip install -q --upgrade -r requirements.txt || echo Couldn't update dependencies ^(offline?^) - starting anyway.
) else (
  uv pip install -q -p ".venv\Scripts\python.exe" --upgrade -r requirements.txt || echo Couldn't update dependencies ^(offline?^) - starting anyway.
)
echo Starting YT Downloader at http://127.0.0.1:8765  (press Ctrl+C to stop)
".venv\Scripts\python.exe" server.py %*
