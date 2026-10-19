# Launch YT Downloader on Windows (PowerShell): sets up the virtualenv on first run, then opens http://127.0.0.1:8765
#   powershell -ExecutionPolicy Bypass -File run.ps1 [--port 8765] [--no-browser]
#   ... run.ps1 --dev   front-end development: the backend plus `next dev` (http://localhost:3000); needs Node.js
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

if ($args.Count -gt 0 -and $args[0] -eq "--dev") {
    $rest = @($args | Select-Object -Skip 1)
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Write-Host "Dev mode needs Node.js (npm)."; exit 1 }
    if (-not (Test-Path node_modules)) { npm ci }
    $port = 8765
    $i = [array]::IndexOf($rest, "--port"); if ($i -ge 0) { $port = $rest[$i + 1] }
    $backend = Start-Process -FilePath $python -ArgumentList (@("server.py", "--dev", "--no-browser") + $rest) -NoNewWindow -PassThru
    $env:YTDL_BACKEND = "http://127.0.0.1:$port"
    $front = Start-Process -FilePath "npx.cmd" -ArgumentList @("next", "dev", "frontend", "--port", "3000") -NoNewWindow -PassThru
    Write-Host "Dev mode: page at http://localhost:3000 (edits reload it), API at http://127.0.0.1:$port"
    try { Wait-Process -Id $backend.Id, $front.Id -ErrorAction SilentlyContinue }
    finally { foreach ($p in $backend, $front) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force } } }   # Ctrl+C stops both
    exit
}

Write-Host "Starting YT Downloader at http://127.0.0.1:8765  (press Ctrl+C to stop)"
& $python server.py @args
exit $LASTEXITCODE
