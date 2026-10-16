---
name: verify-downloads
description: Read-only health report on the user's real downloaded videos — checks whether files are complete and playable without renaming, moving or deleting anything. Use when the user asks whether downloads are OK, after an incident that may have damaged files, or before suggesting re-downloads.
---

# Verify downloads (read-only)

The app's own "Check files" button renames damaged files and changes the library. This skill only reports, so it is safe on real folders.

## Steps

1. Find the folder: ask the user, or read `output_dir` from `data/settings.json` (playlists are in subfolders named after the playlist). Default: `~/Downloads/YT Downloader/`.
2. Check whether the user's app is running (`lsof -iTCP:8765 -sTCP:LISTEN`). Files still being downloaded or merged show up as `.part` / `.fNNN` leftovers. That's expected, not damage.
3. Run the quick check (seconds per file):
   ```bash
   .venv/bin/python .claude/skills/verify-downloads/scripts/verify_folder.py "<folder>"
   ```
   Add `--id <video id>` (repeatable) for specific videos.
4. For a few files that matter, or after an incident like two processes writing the same file, add `--full`. It decodes the whole file to find damage in the middle; expect about 2–3 minutes per hour of video. Run it in the background for many files.
5. For a stream that is still on disk as `….fNNN.ext`, compare its size with the exact size YouTube reports. Get that with a `yt_dlp.YoutubeDL({...}).extract_info(url, download=False)` call, using the user's proxy from `data/settings.json`, and read `formats[*].filesize`. If the file is larger than expected, data was written twice.

## Report

List the bad files with the reason and the leftover count. Don't delete or rename anything. If files are bad, tell the user they can press **Check files** in the app (it renames damaged files and lets them Retry), or offer to do it for them.
