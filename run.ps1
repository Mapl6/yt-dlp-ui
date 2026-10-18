# Launch YT Downloader on Windows (PowerShell): sets up the virtualenv on first run, then opens http://127.0.0.1:8765
#   powershell -ExecutionPolicy Bypass -File run.ps1 [--port 8765] [--no-browser]
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg is required: winget install Gyan.FFmpeg  (then open a new terminal)"
    exit 1
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv -q .venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv }
    else { python -m venv .venv }
}

# Keep yt-dlp current — YouTube changes often and old versions break.
# A failed update (e.g. offline) must not stop the app from starting.
Write-Host "Checking dependencies..."
$ErrorActionPreference = "Continue"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install -q -p $python --upgrade -r requirements.txt
} else {
    & $python -m ensurepip -q *> $null
    & $python -m pip install -q --upgrade -r requirements.txt
}
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't update dependencies (offline?) - starting anyway." }

Write-Host "Starting YT Downloader at http://127.0.0.1:8765  (press Ctrl+C to stop)"
& $python server.py @args
exit $LASTEXITCODE
