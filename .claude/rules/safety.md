# Safety: the user's real library and downloads

This app manages real files. A test run once started downloading into the user's real folder while their own copy of the app was writing the same files.

- **Real data lives in `data/`** (library, settings, log) and the user's download folders (default `~/Downloads/YT Downloader/`). Never start a `Manager()` or `server.py` against `data/`. Always set `YTDL_DATA_DIR` to a scratch directory.
- **To test on the user's library, use the `safe-test-server` skill.** A plain copy of `data/` still has queued videos and real output folders, so it starts downloading into the real folder at once.
- **Never run "Check files" or "Clean up leftovers" on the user's real folders** unless they ask: they rename and delete files. To inspect real downloads, use the `verify-downloads` skill (read-only).
- **Check whether the user's app is running** (`lsof -iTCP:8765 -sTCP:LISTEN`) before anything that touches their download folder, and tell them if it is.
- **Stop every server you start** (`pkill -f "server.py --port <port>"`) before finishing.
- **Don't commit, push, or delete user files without asking.**
