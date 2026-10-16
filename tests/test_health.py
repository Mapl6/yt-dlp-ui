"""Run: .venv/bin/python tests/test_health.py   (needs ffmpeg; no internet required)

File health tests: "Check files" on existing downloads, quick missing-file check on refresh, "Clean up
leftovers", per-stream size check, and re-merging from kept streams instead of downloading again.
"""
import http.server, os, socketserver, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ytdl-health-"))
os.environ["YTDL_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT))
import manager as M
from yt_dlp import YoutubeDL

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, "" if cond else extra)

def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False

# ---------- media
media = TMP / "media"; media.mkdir()
def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "30",
   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart", str(media / "full.mp4"))
ff("-i", str(media / "full.mp4"), "-an", "-c", "copy", str(media / "v.mp4"))
ff("-i", str(media / "full.mp4"), "-vn", "-c", "copy", str(media / "a.m4a"))
GOOD = (media / "full.mp4").read_bytes()

mgr = M.Manager()
out = TMP / "out"; out.mkdir()
mgr.update_settings({"output_dir": str(out)})
level = {"v": "offline"}                 # keep workers idle until a test wants them
mgr.net_level = lambda: level["v"]

def entry(i, vid):
    return {"index": i, "id": vid, "title": f"Video {vid}", "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": 30, "extractor": "Youtube"}

with mgr.cond:
    job = mgr._create("https://youtube.com/playlist?list=H", {
        "kind": "playlist", "source_id": "H", "title": "Health", "thumbnail": None, "heights": M.VIDEO_HEIGHTS,
        "items": [entry(i + 1, v) for i, v in enumerate("ABCDEQ")]})
    job["outdir"] = str(out)
    items = {it["id"]: it for it in job["items"]}
    for it in items.values():
        it["outdir"] = str(out)
key = M.Manager._archive_key

# A: downloaded, healthy. B: downloaded, cut off. C: in archive but file gone. D: not marked, good file in folder.
(out / "01 - Video A [A].mp4").write_bytes(GOOD)
(out / "02 - Video B [B].mp4").write_bytes(GOOD[: len(GOOD) // 3])
(out / "04 - Video D [D].mp4").write_bytes(GOOD)
with mgr.cond:
    items["A"].update(status="done", filepath=str(out / "01 - Video A [A].mp4"))
    items["B"].update(status="done", filepath=str(out / "02 - Video B [B].mp4"))
    items["C"].update(status="skipped")
    for v in "ABC":
        mgr._archive_add(str(out), key(items[v]))

# ---------- Check files
check("check: starts", mgr.start_check(job["id"]))
check("check: second start while running is refused", not mgr.start_check(job["id"]))
check("check: finishes", wait(lambda: not job["check"]["running"]))
c = job["check"]
check("check: counts", (c["ok"], c["damaged"], c["missing"], c["found"]) == (1, 1, 1, 1), c)
check("check: healthy file verified", items["A"]["status"] == "done" and items["A"]["health"] == "ok")
check("check: damaged file -> failed, renamed aside (not deleted)",
      items["B"]["status"] == "failed" and items["B"]["file_problem"] == "damaged"
      and (out / "02 - Video B [B].mp4.damaged").exists() and not (out / "02 - Video B [B].mp4").exists(), items["B"]["error"])
check("check: missing file -> failed with explanation", items["C"]["status"] == "failed" and "not in the folder" in items["C"]["error"])
check("check: good file already in folder is adopted", items["D"]["status"] == "skipped" and items["D"]["health"] == "ok"
      and key(items["D"]) in M.Manager._archive_ids(str(out)))
check("check: untouched video without a file stays new", items["E"]["status"] == "new")
check("check: details log explains", any("File check" in l for l in mgr.item_log(job["id"], "B")["lines"]))
check("summary: exposes the check result", mgr.summary(job)["check"]["damaged"] == 1)

mgr.item_action(job["id"], "B", "retry")
check("retry: damaged video re-queued and removed from the archive",
      items["B"]["status"] == "queued" and key(items["B"]) not in M.Manager._archive_ids(str(out)) and not items["B"]["file_problem"])
mgr.item_action(job["id"], "B", "cancel")

# ---------- refresh: quick missing-file check
(out / "01 - Video A [A].mp4").unlink()
M.inspect_url = lambda url, settings: {"kind": "playlist", "source_id": "H", "title": "Health", "thumbnail": None,
                                       "heights": M.VIDEO_HEIGHTS, "items": [entry(i + 1, v) for i, v in enumerate("ABCDEQ")]}
mgr.refresh(job["id"])
check("refresh: downloaded video whose file vanished is flagged", items["A"]["status"] == "failed" and items["A"]["file_problem"] == "missing")

# ---------- Clean up leftovers
for name in ("Video E [E].f137.mp4.part", "Video E [E].webp", "Video D [D].f140.m4a", "Video Q [Q].f137.mp4.part",
             "unrelated.part", "Video D [D].jpg"):
    (out / name).write_bytes(b"x" * 1000)
with mgr.cond:
    items["Q"]["status"] = "queued"                  # queued: its partial download must survive
pre = mgr.cleanup(job["id"], dry_run=True)
expect = {"Video E [E].f137.mp4.part", "Video E [E].webp", "Video D [D].f140.m4a", "02 - Video B [B].mp4.damaged"}
check("cleanup: preview lists only this playlist's leftovers", set(pre["names"]) == expect, pre["names"])
check("cleanup: preview deletes nothing", (out / "Video E [E].webp").exists())
res = mgr.cleanup(job["id"], dry_run=False)
check("cleanup: leftovers deleted", res["files"] == 4 and not any((out / n).exists() for n in expect))
check("cleanup: queued video's partial download kept", (out / "Video Q [Q].f137.mp4.part").exists())
check("cleanup: unrelated files and a downloaded video's thumbnail kept",
      (out / "unrelated.part").exists() and (out / "Video D [D].jpg").exists() and (out / "04 - Video D [D].mp4").exists())
with mgr.cond:
    items["Q"]["status"] = "new"

# ---------- per-stream size check (unit)
p1, p2 = TMP / "x [v].f137.mp4", TMP / "x [v].f140.m4a"
p1.write_bytes(b"a" * 100); p2.write_bytes(b"b" * 50)
info = {"requested_formats": [{"format_id": "137", "filesize": 100, "protocol": "https"},
                              {"format_id": "140", "filesize": 50, "protocol": "https"}]}
check("size: exact sizes pass", M._check_part_sizes(info, {"137": p1, "140": p2}) is None)
info["requested_formats"][1]["filesize"] = 60
check("size: wrong size detected", "140" in (M._check_part_sizes(info, {"137": p1, "140": p2}) or ""))
info["requested_formats"][1]["protocol"] = "m3u8_native"
check("size: fragmented streams (no exact size) are not compared", M._check_part_sizes(info, {"137": p1, "140": p2}) is None)
check("size: part files found by id", set(M._part_files(str(TMP), "v")) == {"137", "140"})

# ---------- merged downloads from a local server (separate video + audio streams)
gets = {"n": 0}
class Static(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(media), **k)
    def log_message(self, *a): pass
    def do_GET(self):
        gets["n"] += 1
        return super().do_GET()
class TS(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
srv = TS(("127.0.0.1", 0), Static); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

audio_size = {"v": (media / "a.m4a").stat().st_size}
def fake_info(url):
    return {"id": "mergevid", "title": "Merge test", "extractor": "test", "extractor_key": "Test",
            "webpage_url": url, "duration": 30, "formats": [
        {"format_id": "137", "url": f"http://127.0.0.1:{port}/v.mp4", "ext": "mp4", "vcodec": "avc1.4d401e",
         "acodec": "none", "width": 640, "height": 360, "filesize": (media / "v.mp4").stat().st_size, "protocol": "http"},
        {"format_id": "140", "url": f"http://127.0.0.1:{port}/a.m4a", "ext": "m4a", "vcodec": "none",
         "acodec": "mp4a.40.2", "filesize": audio_size["v"], "protocol": "http"}]}
real_extract = YoutubeDL.extract_info
def extract(self, url, download=True, *a, **k):
    if str(url).startswith("test://"):
        return self.process_ie_result(fake_info(url), download=download)
    return real_extract(self, url, download, *a, **k)
YoutubeDL.extract_info = extract

mout = TMP / "merge"; mout.mkdir()
with mgr.cond:
    mjob = mgr._create("test://merge", {"kind": "video", "source_id": "mergevid", "title": "Merge", "thumbnail": None,
                                       "heights": [360], "items": [{"index": 1, "id": "mergevid", "title": "Merge test",
                                       "url": "test://merge", "duration": 30, "extractor": "Test"}]})
    mjob["outdir"] = str(mout)
mit = mjob["items"][0]
OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "output_dir": str(mout)}
level["v"] = "good"

def run_download():
    with mgr.cond:
        mit.update(status="new", next_try=0)
    mgr.queue(mjob["id"], None, dict(OPTS))
    wait(lambda: mit["status"] in ("done", "failed") or (mit["status"] == "queued" and mit["error"]), 90)

run_download()
check("merge: downloaded and verified", mit["status"] == "done" and mit["health"] == "ok", (mit["status"], mit["error"]))
check("merge: separate streams deleted after verification", not M._part_files(str(mout), "mergevid"),
      list(M._part_files(str(mout), "mergevid")))
check("merge: final file plays to the end", M._verify_file(mit["filepath"], 30) is None)

# re-merge: the first merged file "fails" verification -> merged again locally, nothing downloaded again
os.remove(mit["filepath"]); mgr._archive_remove(str(mout), key(mit))
real_verify, calls = M._verify_file, {"n": 0}
def flaky_verify(path, duration):
    calls["n"] += 1
    return "The end of the file is missing" if calls["n"] == 1 else real_verify(path, duration)
M._verify_file = flaky_verify
before = gets["n"]
run_download()
downloads_first = gets["n"] - before
M._verify_file = real_verify
check("re-merge: recovered without failing", mit["status"] == "done", (mit["status"], mit["error"]))
check("re-merge: logged", any("merging again" in l for l in mgr.item_log(mjob["id"], "mergevid")["lines"]))
check("re-merge: streams downloaded once, not twice", downloads_first == 2, downloads_first)

# thumbnail + metadata: the cover goes inside the video file, no image left next to it; playlist name and number tagged
os.remove(mit["filepath"]); mgr._archive_remove(str(mout), key(mit))
ff("-f", "lavfi", "-i", "testsrc=size=320x180", "-frames:v", "1", str(media / "thumb.png"))
plain_info = fake_info
fake_info = lambda url: plain_info(url) | {"thumbnails": [{"url": f"http://127.0.0.1:{port}/thumb.png", "id": "0"}],
                                           "uploader": "Tester", "description": "About the video"}
with mgr.cond:
    mjob.update(kind="playlist", title="My List"); mit["number"] = "07"
OPTS.update(thumbnail=True, metadata=True)
run_download()
check("cover: downloaded and verified", mit["status"] == "done", (mit["status"], mit["error"]))
check("cover: picture embedded in the video", M._has_cover(mit["filepath"]))
left = [p.name for p in M._files_for(str(mout), "mergevid") if M.SIDECAR.search(p.name)]
check("cover: no separate thumbnail file left", not left, left)
tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", mit["filepath"]],
                      capture_output=True, text=True).stdout
check("metadata: title, artist, description, album and track written",
      all(s in tags for s in ('"Merge test"', '"Tester"', "About the video", '"My List"', '"track": "7"')), tags)
(mout / "Merge test [mergevid].webp").write_bytes(b"x" * 100)    # left by an older version of the app
check("cleanup: thumbnail of a video that has the cover inside is a leftover",
      mgr.cleanup(mjob["id"], dry_run=True)["names"] == ["Merge test [mergevid].webp"])
check("cover: a file without a cover is detected", not M._has_cover(str(media / "full.mp4")))
fake_info = plain_info
with mgr.cond:
    mjob.update(kind="video", title="Merge"); mit["number"] = ""
OPTS.update(thumbnail=False, metadata=False)
(mout / "Merge test [mergevid].webp").unlink()

# wrong stream size: YouTube says the audio is bigger than what arrived -> streams discarded, retried
os.remove(mit["filepath"]); mgr._archive_remove(str(mout), key(mit))
audio_size["v"] += 1000
run_download()
level["v"] = "offline"
check("size mismatch: not marked done, retried", mit["status"] == "queued" and "wrong size" in (mit["error"] or ""),
      (mit["status"], mit["error"]))
check("size mismatch: bad streams removed", not M._part_files(str(mout), "mergevid"))

print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
