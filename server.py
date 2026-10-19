"""Start YT Downloader: ./run.sh (or python server.py [--host H] [--port P] [--no-browser] [--password PW]).

The code lives in backend/ytdl/; this launcher stays here so run.sh, run.bat, run.ps1 and "Restart now"
(which runs the same command again) keep working.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from ytdl.server import main  # noqa: E402

if __name__ == "__main__":
    main()
