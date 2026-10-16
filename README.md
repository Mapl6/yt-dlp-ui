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
4. **Come back any time.** The library and every video's progress are saved in `data/state.json`. Close the app, restart the Mac, add more videos later; the playlist keeps track.
5. **Add the same link again** (or press *Check for new videos*) to pull in videos added to the playlist since. Everything you already have stays marked as downloaded.

## Features

- **Quality:** Best, 4K, 2K, 1080p, 720p, 480p, 360p, 240p, 144p, or audio only (MP3 / M4A / Opus)
- **Formats:** MP4 (H.264/AAC, plays everywhere), MKV, or WebM
- **Parallel downloads** (1–8), with pause, resume and stop per playlist, plus remove/retry per video
- Half-finished downloads continue where they stopped, even after a restart
- Videos already in the folder (from its `.downloaded.txt` archive) are marked as downloaded automatically
- **Automatic retries** with backoff for network errors; private or deleted videos fail right away with a clear reason
- Embeds the thumbnail, metadata, chapters, and (optionally) English subtitles

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

## Settings (⚙)

| Setting | Use |
|---|---|
| Parallel downloads | 3 is a safe default; going higher risks YouTube rate limits |
| Cookies from browser | For age-restricted or members-only videos, or a "confirm you're not a bot" error |
| Proxy | `socks5://127.0.0.1:1080` etc., if YouTube is blocked on your network |
| Pause between videos | 2–5 s helps with playlists of hundreds of videos |

Only download content you have the right to download.
