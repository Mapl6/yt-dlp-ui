"""`python server.py`: options, the port check, then the engine and the web server."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import auth, engine
from .app import app


def main():
    """`python server.py`: check the port is free, start the engine, serve the page."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--password", default=os.environ.get("YTDL_PASSWORD", ""),
                        help="require this password to open the app (recommended with --host 0.0.0.0)")
    args = parser.parse_args()
    auth.security["password"] = args.password
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        auth.security["any_host"] = True
        if not args.password:
            print(f"WARNING: serving on {args.host} without a password — anyone who can reach this address can "
                  "use the downloader. Start with --password (or set YTDL_PASSWORD) to protect it.", file=sys.stderr)
    # After a restart the old socket can take a moment to go away: retry for a few seconds then.
    tries = 40 if os.environ.pop("YTDL_RESTARTED", None) else 1
    for attempt in range(tries):
        with socket.socket(socket.AF_INET6 if ":" in args.host else socket.AF_INET) as probe:
            if tries > 1:
                # only right after our own restart: on macOS this flag would also let the check pass while
                # another copy listens on 0.0.0.0, so a normal start never sets it
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((args.host, args.port))
                break
            except OSError:
                if attempt == tries - 1:
                    sys.exit(f"Port {args.port} is already in use — is YT Downloader already running? "
                             f"Open http://127.0.0.1:{args.port} or stop the other copy first.")
                time.sleep(0.25)
    engine.runtime["can_restart"] = True
    engine.start_engine(args.port)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    # The live stream never ends by itself: don't let it hold up Ctrl+C for more than a moment.
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                                           timeout_graceful_shutdown=2))
    engine.runtime["server"] = server
    server.run()
