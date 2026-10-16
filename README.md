# YT Downloader

A local web app for downloading YouTube videos, playlists, and channels with yt-dlp.

## Start

```bash
./run.sh            # opens http://127.0.0.1:8765
```

Requires Python 3.10+, `ffmpeg` (`brew install ffmpeg`), and a JS runtime — deno is recommended (`brew install deno`); node or bun also work.

## How it works

1. **Paste a playlist link and press Add.** The whole playlist (every video) is saved to your library. Nothing downloads yet.
2. **Open the playlist and choose videos:** tick them, use *Select not downloaded*, type a range like `1-50, 80, 100-`, or filter by title.
3. **Pick a quality and press Download.** Each video shows its state: Not downloaded → Queued → Downloading → Downloaded.
4. **Come back any time.** The library and every video's progress are saved in `data/library.db` (an older `data/state.json` is moved in automatically on first start). Close the app, restart the Mac, add more videos later; the playlist keeps track.
5. **Add the same link again** (or press *Check for new videos*) to pull in videos added to the playlist since. Everything you already have stays marked as downloaded. The list follows the playlist's new order, but files keep their numbers, and new videos get the next free number. Videos that left the playlist or became private/deleted are marked **Unavailable** and are skipped by *Select not downloaded*.

## Features

- **Quality:** Best, 4K, 2K, 1080p, 720p, 480p, 360p, 240p, 144p, or audio only (MP3 / M4A / Opus)
- **Formats:** MP4 (H.264/AAC, plays everywhere), MKV, or WebM
- **Parallel downloads** (1–8), with pause, resume and stop per playlist, plus remove/retry per video
- Half-finished downloads continue where they stopped, even after a restart
- Videos already in the folder (from its `.downloaded.txt` archive) are marked as downloaded automatically
- **Automatic retries** with backoff for network errors; private or deleted videos fail right away with a clear reason
- Embeds the thumbnail (as the cover inside the video file, with no separate image left next to it), metadata (title, channel, date, description, link, and for playlists the playlist name as album and the video number as track), chapters, and (optionally) English subtitles
- **Check files** (inside an opened playlist) verifies every downloaded video, including ones downloaded long ago. Damaged files are renamed to `….damaged` and missing ones are flagged; both go to the *Failed* tab, where *Retry* downloads them again. Good files already in the folder that the app didn't know about are marked as downloaded.
- **Clean up leftovers** deletes partial downloads, unmerged streams, `….damaged` files and thumbnail images whose picture is already inside the video, of a playlist, after showing you the list. Videos in the queue are never touched.
- **Details** button on any video with a problem shows what happened; the full log is in `data/app.log`

## Connection panel

Every 10 seconds the app checks the internet (Google) and YouTube (through your proxy, if one is set), three requests each, and shows:

| Status | Meaning |
|---|---|
| Excellent / Good | YouTube answers quickly and steadily |
| Fair | Works, but responses are slow or some are lost |
| Weak | Very slow or unstable: downloads may stall; use fewer parallel downloads |
| YouTube unreachable | Internet works but YouTube is blocked: turn on a VPN or set a proxy |
| Offline | No internet (or the proxy isn't running) |

It also shows response times, stability, live download speed, and a chart of recent YouTube response times. Press **Test now** to check right away; saving Settings re-checks automatically.

## Activity dashboard

The **Activity** panel shows every playlist at once:

- **Totals:** how many videos are downloading (and the current limit, which drops on a weak connection), how many are queued (ready vs. waiting for a retry or a paused playlist), total speed with the data left in the current videos, and how many finished or failed in the last 24 hours.
- **Downloading now:** each video's playlist, progress, size, speed, time left, when it started and which attempt this is. ✕ stops it.
- **Up next:** the queue in the order videos will start, with why each one is waiting ("Waiting for a free slot", "Retry in 0:40", "Playlist paused", offline…) and its last error. ✕ removes it from the queue.
- **Recently finished:** the last 30 videos that were downloaded or failed, with the file name or error. Failed ones can be retried here.

Video titles link to YouTube. Playlist names jump to that playlist below.

## Settings (⚙)

| Setting | Use |
|---|---|
| Parallel downloads | 3 is a safe default; going higher risks YouTube rate limits |
| Cookies from browser | For age-restricted or members-only videos, or a "confirm you're not a bot" error |
| Proxy | `socks5://127.0.0.1:1080` etc., if YouTube is blocked on your network |
| Pause between videos | 2–5 s helps with playlists of hundreds of videos |
| Wait while offline | On by default: the queue pauses while offline and uses fewer parallel downloads on a weak connection |

## Weak or unstable internet

Downloads are built so that nothing is lost when the connection drops:

- **Resume, never restart.** A dropped download keeps its `.part` file and continues from the same byte, even after the app or Mac restarts.
- **Waits instead of failing.** Connection problems are retried with growing pauses (10 s → 5 min, up to 40 times) without using up the normal retry limit. While you're offline or YouTube is blocked, the queue simply waits, and continues the moment the connection is back.
- **Adapts to a weak connection.** On a "Weak" connection only one video downloads at a time, on "Fair" at most two, so each one can finish.
- **Every file is checked.** Each downloaded stream must be exactly the size YouTube reports, and the separate video and audio streams are kept until the merged file passes the check — a bad merge is redone from them instead of downloading again. A video only counts as downloaded after the file's length is compared with the video's and its last seconds are decoded. A cut-off file is deleted and downloaded again. Missing pieces of a stream fail the download instead of leaving gaps.
- **Queue-wide pauses.** If the disk is full, an external drive is unplugged, or YouTube starts rate-limiting, all downloads pause with a banner explaining why, instead of failing one by one.
- **Safe library.** The library is a SQLite database that survives crashes and power loss, and each save writes only what changed. A backup is made every 10 minutes; a damaged database is kept aside and the backup is loaded.
- **No double downloads.** Only one copy of the app can use a library, and a video locked by another copy (for example, a second library saving to the same folder) waits instead of writing the same file twice.

Run the tests (no internet needed): `.venv/bin/python tests/test_core.py` and `.venv/bin/python tests/test_reliability.py`

## Security

The app only answers requests addressed to `127.0.0.1` or `localhost`, and every API call needs a token that is built into the page at startup. Other websites you visit can't control it. If you start it with `--host 0.0.0.0` to use it from another device, anyone on your network can use it.

Only download content you have the right to download.
