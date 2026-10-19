"""File health tests: "Check files" on existing downloads, quick missing-file check on refresh, "Clean up
leftovers", per-stream size check, and re-merging from kept streams instead of downloading again."""
import os
import subprocess
from types import SimpleNamespace

import pytest

import manager as M
from helpers import entry, wait

key = M.Manager._archive_key
IDS = "ABCDEQ"
OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False}


def listing():
    return {"kind": "playlist", "source_id": "H", "title": "Health", "thumbnail": None, "heights": M.VIDEO_HEIGHTS,
            "items": [entry(i + 1, v, duration=30) for i, v in enumerate(IDS)]}


@pytest.fixture(scope="module")
def health(mgr, out, good):
    """A: downloaded, healthy. B: downloaded, cut off. C: in archive but file gone. D: not marked, good file in
    the folder. E and Q: new."""
    with mgr.cond:
        job = mgr._create("https://youtube.com/playlist?list=H", listing())
        job["outdir"] = str(out)
        items = {it["id"]: it for it in job["items"]}
        for it in items.values():
            it["outdir"] = str(out)
    (out / "01 - Video A [A].mp4").write_bytes(good)
    (out / "02 - Video B [B].mp4").write_bytes(good[: len(good) // 3])
    (out / "04 - Video D [D].mp4").write_bytes(good)
    with mgr.cond:
        items["A"].update(status="done", filepath=str(out / "01 - Video A [A].mp4"))
        items["B"].update(status="done", filepath=str(out / "02 - Video B [B].mp4"))
        items["C"].update(status="skipped")
        for v in "ABC":
            mgr._archive_add(str(out), key(items[v]))
    return SimpleNamespace(job=job, items=items)


@pytest.fixture(scope="module")
def checked(mgr, health):
    """Check files has run on the playlist above."""
    r = SimpleNamespace(started=mgr.start_check(health.job["id"]))
    r.second = mgr.start_check(health.job["id"])
    r.finished = wait(lambda: not health.job["check"]["running"])
    return r


def test_check_files(mgr, health, checked, out):
    job, items = health.job, health.items
    assert checked.started, "check: starts"
    assert not checked.second, "check: second start while running is refused"
    assert checked.finished, "check: finishes"
    c = job["check"]
    assert (c["ok"], c["damaged"], c["missing"], c["found"]) == (1, 1, 1, 1), f"check: counts: {c}"
    assert items["A"]["status"] == "done" and items["A"]["health"] == "ok", "check: healthy file verified"
    assert items["B"]["status"] == "failed" and items["B"]["file_problem"] == "damaged" \
        and (out / "02 - Video B [B].mp4.damaged").exists() and not (out / "02 - Video B [B].mp4").exists(), \
        f"check: damaged file -> failed, renamed aside (not deleted): {items['B']['error']}"
    assert items["C"]["status"] == "failed" and "not in the folder" in items["C"]["error"], \
        "check: missing file -> failed with explanation"
    assert items["D"]["status"] == "skipped" and items["D"]["health"] == "ok" \
        and key(items["D"]) in M.Manager._archive_ids(str(out)), "check: good file already in folder is adopted"
    assert items["E"]["status"] == "new", "check: untouched video without a file stays new"
    assert any("File check" in line for line in mgr.item_log(job["id"], "B")["lines"]), "check: details log explains"
    assert mgr.summary(job)["check"]["damaged"] == 1, "summary: exposes the check result"

    mgr.item_action(job["id"], "B", "retry")
    try:
        assert items["B"]["status"] == "queued" and key(items["B"]) not in M.Manager._archive_ids(str(out)) \
            and not items["B"]["file_problem"], "retry: damaged video re-queued and removed from the archive"
    finally:
        mgr.item_action(job["id"], "B", "cancel")


def test_refresh_flags_missing_file(mgr, health, out, monkeypatch):
    # refresh: quick missing-file check
    (out / "01 - Video A [A].mp4").unlink(missing_ok=True)
    monkeypatch.setattr(M, "inspect_url", lambda url, settings: listing())
    mgr.refresh(health.job["id"])
    a = health.items["A"]
    assert a["status"] == "failed" and a["file_problem"] == "missing", \
        "refresh: downloaded video whose file vanished is flagged"


def test_cleanup_leftovers(mgr, health, checked, out):
    items = health.items
    for name in ("Video E [E].f137.mp4.part", "Video E [E].webp", "Video D [D].f140.m4a", "Video Q [Q].f137.mp4.part",
                 "unrelated.part", "Video D [D].jpg"):
        (out / name).write_bytes(b"x" * 1000)
    with mgr.cond:
        items["Q"]["status"] = "queued"                  # queued: its partial download must survive
    try:
        pre = mgr.cleanup(health.job["id"], dry_run=True)
        expect = {"Video E [E].f137.mp4.part", "Video E [E].webp", "Video D [D].f140.m4a", "02 - Video B [B].mp4.damaged"}
        assert set(pre["names"]) == expect, f"cleanup: preview lists only this playlist's leftovers: {pre['names']}"
        assert (out / "Video E [E].webp").exists(), "cleanup: preview deletes nothing"
        res = mgr.cleanup(health.job["id"], dry_run=False)
        assert res["files"] == 4 and not any((out / n).exists() for n in expect), "cleanup: leftovers deleted"
        assert (out / "Video Q [Q].f137.mp4.part").exists(), "cleanup: queued video's partial download kept"
        assert (out / "unrelated.part").exists() and (out / "Video D [D].jpg").exists() \
            and (out / "04 - Video D [D].mp4").exists(), "cleanup: unrelated files and a downloaded video's thumbnail kept"
    finally:
        with mgr.cond:
            items["Q"]["status"] = "new"


def test_part_sizes(tmp_path):
    # per-stream size check (unit)
    p1, p2 = tmp_path / "x [v].f137.mp4", tmp_path / "x [v].f140.m4a"
    p1.write_bytes(b"a" * 100)
    p2.write_bytes(b"b" * 50)
    info = {"requested_formats": [{"format_id": "137", "filesize": 100, "protocol": "https"},
                                  {"format_id": "140", "filesize": 50, "protocol": "https"}]}
    assert M._check_part_sizes(info, {"137": p1, "140": p2}) is None, "size: exact sizes pass"
    info["requested_formats"][1]["filesize"] = 60
    assert "140" in (M._check_part_sizes(info, {"137": p1, "140": p2}) or ""), "size: wrong size detected"
    info["requested_formats"][1]["protocol"] = "m3u8_native"
    assert M._check_part_sizes(info, {"137": p1, "140": p2}) is None, \
        "size: fragmented streams (no exact size) are not compared"
    assert set(M._part_files(str(tmp_path), "v")) == {"137", "140"}, "size: part files found by id"


# ---------- merged downloads from a local server (separate video + audio streams)

@pytest.fixture(scope="module")
def merge(mgr, tmp, fake):
    mout = tmp / "merge"
    mout.mkdir()
    with mgr.cond:
        mjob = mgr._create("test://mergevid", {
            "kind": "video", "source_id": "mergevid", "title": "Merge", "thumbnail": None, "heights": [360],
            "items": [{"index": 1, "id": "mergevid", "title": "Merge test", "url": "test://mergevid", "duration": 30,
                       "extractor": "Test"}]})
        mjob["outdir"] = str(mout)
    fake.titles["mergevid"] = "Merge test"
    return SimpleNamespace(job=mjob, it=mjob["items"][0], out=mout)


def run_download(mgr, net, merge, **opts):
    """Download the merge video again from scratch (no file, no archive line)."""
    it = merge.it
    if it.get("filepath") and os.path.exists(it["filepath"]):
        os.remove(it["filepath"])
    mgr._archive_remove(str(merge.out), key(it))
    with mgr.cond:
        it.update(status="new", next_try=0)
    net.level = "good"
    try:
        mgr.queue(merge.job["id"], None, dict(OPTS, output_dir=str(merge.out), **opts))
        wait(lambda: it["status"] in ("done", "failed") or (it["status"] == "queued" and it["error"]), 90)
    finally:
        net.level = "offline"


@pytest.mark.slow
def test_merged_download(mgr, net, merge):
    run_download(mgr, net, merge)
    mit = merge.it
    assert mit["status"] == "done" and mit["health"] == "ok", \
        f"merge: downloaded and verified: {(mit['status'], mit['error'])}"
    assert not M._part_files(str(merge.out), "mergevid"), "merge: separate streams deleted after verification"
    assert M._verify_file(mit["filepath"], 30) is None, "merge: final file plays to the end"


@pytest.mark.slow
def test_remerge_from_kept_streams(mgr, net, merge, media_server, monkeypatch):
    # the first merged file "fails" verification -> merged again locally, nothing downloaded again
    real_verify, calls = M._verify_file, {"n": 0}

    def flaky_verify(path, duration):
        calls["n"] += 1
        return "The end of the file is missing" if calls["n"] == 1 else real_verify(path, duration)

    monkeypatch.setattr(M, "_verify_file", flaky_verify)
    before = media_server.gets
    run_download(mgr, net, merge)
    downloads_first = media_server.gets - before
    monkeypatch.undo()
    mit = merge.it
    assert mit["status"] == "done", f"re-merge: recovered without failing: {(mit['status'], mit['error'])}"
    assert any("merging again" in line for line in mgr.item_log(merge.job["id"], "mergevid")["lines"]), "re-merge: logged"
    assert downloads_first == 2, f"re-merge: streams downloaded once, not twice: {downloads_first}"


@pytest.mark.slow
def test_cover_and_metadata(mgr, net, merge, fake, media_server, media):
    # the cover goes inside the video file, no image left next to it; playlist name and number tagged
    mit, mjob, mout = merge.it, merge.job, merge.out
    fake.extra = {"thumbnails": [{"url": media_server.url("thumb.png"), "id": "0"}],
                  "uploader": "Tester", "description": "About the video"}
    with mgr.cond:
        mjob.update(kind="playlist", title="My List")
        mit["number"] = "07"
    try:
        run_download(mgr, net, merge, thumbnail=True, metadata=True)
        assert mit["status"] == "done", f"cover: downloaded and verified: {(mit['status'], mit['error'])}"
        assert M._has_cover(mit["filepath"]), "cover: picture embedded in the video"
        left = [p.name for p in M._files_for(str(mout), "mergevid") if M.SIDECAR.search(p.name)]
        assert not left, f"cover: no separate thumbnail file left: {left}"
        tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", mit["filepath"]],
                              capture_output=True, text=True).stdout
        assert all(s in tags for s in ('"Merge test"', '"Tester"', "About the video", '"My List"', '"track": "7"')), \
            f"metadata: title, artist, description, album and track written: {tags}"
        (mout / "Merge test [mergevid].webp").write_bytes(b"x" * 100)    # left by an older version of the app
        assert mgr.cleanup(mjob["id"], dry_run=True)["names"] == ["Merge test [mergevid].webp"], \
            "cleanup: thumbnail of a video that has the cover inside is a leftover"
        assert not M._has_cover(str(media / "full.mp4")), "cover: a file without a cover is detected"
    finally:
        fake.extra = {}
        with mgr.cond:
            mjob.update(kind="video", title="Merge")
            mit["number"] = ""
        (mout / "Merge test [mergevid].webp").unlink(missing_ok=True)


@pytest.mark.slow
def test_stream_size_mismatch(mgr, net, merge, fake):
    # YouTube says the audio is bigger than what arrived -> streams discarded, retried
    fake.audio_extra = 1000
    try:
        run_download(mgr, net, merge)
    finally:
        fake.audio_extra = 0
    mit = merge.it
    try:
        assert mit["status"] == "queued" and "wrong size" in (mit["error"] or ""), \
            f"size mismatch: not marked done, retried: {(mit['status'], mit['error'])}"
        assert not M._part_files(str(merge.out), "mergevid"), "size mismatch: bad streams removed"
    finally:
        mgr.action(merge.job["id"], "stop")
