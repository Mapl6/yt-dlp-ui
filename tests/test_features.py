"""Feature tests: scheduler index and priorities, per-video pause, the event log (history and stats),
processing sub-steps and format notes, queue controls, schedule and speed limits, file actions,
playlist removal with files, .m3u export, the second opinion before calling a file damaged, subtitle
languages, SponsorBlock, parts of videos, size estimates, subscriptions, adding many links, yt-dlp updates,
export and import, settings round-trip and cookies.txt import."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp.utils import DownloadError

from ytdl import manager as M
from helpers import items_of, make_job, new_manager, restore_paths, stop_manager, use_data_dir, wait

MB = 1024 * 1024


@pytest.fixture(scope="module")
def mgr_settings():
    return {"concurrency": 2}


@pytest.fixture(scope="module")
def opts(out):
    return {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "numbering": False,
            "output_dir": str(out)}


def full_scan(mgr):
    return {(j["id"], it["id"]) for j in mgr.jobs.values() for it in j["items"] if it["status"] in M.LIVE}


@pytest.fixture(scope="module")
def downloaded(mgr, net, out, fake, opts):
    """Playlist "three": ok1 downloaded for real, bad1 failed like a private video."""
    j3 = make_job(mgr, out, "three", ["ok1", "bad1"])
    fake.fail_ids.add("bad1")
    mgr.queue(j3["id"], None, dict(opts))
    net.level = "good"
    it3 = items_of(j3)
    finished = wait(lambda: it3["ok1"]["status"] == "done" and it3["bad1"]["status"] == "failed", 90)
    net.level = "offline"
    return SimpleNamespace(job=j3, it=it3, finished=finished)


def test_scheduler_index_and_priorities(mgr, net, out, opts):
    j1 = make_job(mgr, out, "one", ["a1", "a2", "a3"], created=1000)
    j2 = make_job(mgr, out, "two", ["b1", "b2"], created=2000)
    mgr.queue(j1["id"], None, dict(opts))
    mgr.queue(j2["id"], None, dict(opts))
    assert set(mgr._live) == full_scan(mgr) and len(mgr._live) == 5, f"index: queued items indexed: {len(mgr._live)}"
    with mgr.cond:
        order = [it["id"] for _, it in mgr._queue_order()]
    assert order == ["a1", "a2", "a3", "b1", "b2"], f"order: oldest playlist first, then playlist order: {order}"
    with mgr.cond:
        mgr._set(j2, items_of(j2)["b2"], priority=5)
        order = [it["id"] for _, it in mgr._queue_order()]
    assert order[0] == "b2", f"order: higher priority goes first: {order}"
    with mgr.cond:
        mgr._set(j2, items_of(j2)["b2"], held=True)
        net.level = "good"
        pick = mgr._next_item()
        net.level = "offline"
    assert pick and pick[1]["id"] == "a1", f"held: a paused video is skipped by the scheduler: {pick and pick[1]['id']}"
    mgr.item_action(j1["id"], "a3", "cancel")
    assert (j1["id"], "a3") not in mgr._live and set(mgr._live) == full_scan(mgr), \
        "index: cancelled video leaves the index"
    mgr.action(j2["id"], "stop")
    assert not any(k[0] == j2["id"] for k in mgr._live) and set(mgr._live) == full_scan(mgr), \
        "index: stopping a playlist empties its part"
    mgr.action(j2["id"], "remove")
    mgr.action(j1["id"], "stop")
    assert not mgr._live and not full_scan(mgr), "index: empty after stop/remove"


@pytest.mark.slow
def test_download_event_log(mgr, downloaded):
    # a real download: event log, format note, sub-step cleared
    j3, it3 = downloaded.job, downloaded.it
    assert downloaded.finished, f"download: finishes: {(it3['ok1']['status'], it3['ok1']['error'], it3['bad1']['status'])}"
    assert "640x360" in (it3["ok1"].get("fmt") or "") and "avc1" in it3["ok1"]["fmt"], \
        f"format note: chosen format recorded: {it3['ok1'].get('fmt')}"
    assert it3["ok1"].get("step") is None, "step: cleared once finished"
    assert it3["bad1"]["history"] and it3["bad1"]["history"][-1]["kind"] == "permanent", \
        f"history: failed attempt recorded with its kind: {it3['bad1'].get('history')}"
    rows = mgr.history()["rows"]
    done = [r for r in rows if r["id"] == "ok1"]
    failed = [r for r in rows if r["id"] == "bad1"]
    assert done and done[0]["bytes"] > 0 and done[0]["seconds"] > 0 and done[0]["status"] == "done", \
        f"events: done download logged with size and time: {done}"
    assert failed and failed[0]["kind"] == "permanent" and failed[0]["current"] == "failed", \
        f"events: failure logged with kind: {failed}"
    act = mgr.activity()
    assert {r["id"] for r in act["recent"]} >= {"ok1", "bad1"}, "activity: recently finished comes from the event log"
    assert act["stats"]["done_today"] >= 1 and act["stats"]["failed_today"] >= 1, \
        f"activity: 24-hour counts: {act['stats']}"
    # retrying keeps the history row
    mgr.item_action(j3["id"], "bad1", "retry")
    try:
        assert any(r["id"] == "bad1" for r in mgr.history()["rows"]), "events: a retried video stays in Recently finished"
        assert next(r for r in mgr.history()["rows"] if r["id"] == "bad1")["current"] == "queued", \
            "events: current status follows the video"
    finally:
        mgr.item_action(j3["id"], "bad1", "cancel")
    stats = mgr.stats(7)
    assert stats["done"] >= 1 and stats["failed"] >= 1 and stats["success"] is not None \
        and stats["kinds"][0]["kind"] == "permanent" and stats["days"], f"stats: totals, success rate, kinds: {stats}"
    mgr.clear_history()
    assert not mgr.history()["rows"] and mgr.store.count_events() == 0, "clear: Recently finished emptied"
    assert mgr.stats(7)["done"] == stats["done"], "clear: stats keep the rows"


def test_activity_filters(mgr, out, opts):
    j4 = make_job(mgr, out, "four", ["c1", "c2"])
    mgr.queue(j4["id"], None, dict(opts))
    try:
        assert {r["id"] for r in mgr.activity(job_id=j4["id"])["queue"]} == {"c1", "c2"}, "activity: filter by playlist"
        assert [r["id"] for r in mgr.activity(q="video c2")["queue"]] == ["c2"], "activity: title search"
        assert mgr.activity(q="no such title")["stats"]["queued"] == 2, "activity: totals ignore the filter"
    finally:
        mgr.action(j4["id"], "stop")


def test_event_log_concurrent_writers(mgr, out):
    # many writers at once with the saver
    job = make_job(mgr, out, "writers", ["w1"])
    it = items_of(job)["w1"]
    errors = []
    before = mgr.store.count_events(hidden=True)

    def writer(n):
        try:
            for i in range(20):
                mgr.store.add_events([{"time": time.time(), "job_id": "x", "item_id": f"{n}-{i}", "status": "done"}])
                mgr._set(job, it, title=f"T{n}{i}")
                mgr.save_state()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, f"store: concurrent writers never fail: {errors[:2]}"
    assert mgr.store.count_events(hidden=True) - before >= 120, "store: all rows written"


@pytest.fixture(scope="module")
def j5(mgr, out, opts, fake):
    job = make_job(mgr, out, "five", ["d1", "d2", "d3", "d4"])
    mgr.queue(job["id"], None, dict(opts))
    return job


def test_reorder_queue(mgr, downloaded, j5):
    def qorder():
        return [r["id"] for r in mgr.activity(job_id=j5["id"])["queue"]]

    mgr.move(j5["id"], "d3", "top")
    assert qorder()[0] == "d3", f"move: to top: {qorder()}"
    mgr.move(j5["id"], "d1", "bottom")
    assert qorder()[-1] == "d1", f"move: to bottom: {qorder()}"
    mgr.move(j5["id"], "d4", before=(j5["id"], "d2"))
    assert qorder() == ["d3", "d4", "d2", "d1"], f"move: drag before another video: {qorder()}"
    mgr.move(j5["id"], "d3")
    assert qorder() == ["d4", "d2", "d1", "d3"], f"move: drag to the end: {qorder()}"
    with pytest.raises(ValueError):
        mgr.move(downloaded.job["id"], "ok1", "top")    # move: a downloaded video can't be moved


def test_try_now_clear_hold_pause_all(mgr, j5):
    it5 = items_of(j5)
    try:
        with mgr.cond:
            mgr._set(j5, it5["d2"], next_try=time.time() + 300)
        mgr.item_action(j5["id"], "d2", "now")
        assert it5["d2"]["next_try"] == 0, "try now: retry wait cleared"
        mgr._hold_queue("YouTube is limiting requests", 300)
        assert mgr.waiting_reason(), "hold: queue-wide pause set"
        mgr.clear_hold()
        assert mgr.hold is None and "limiting" not in (mgr.waiting_reason() or ""), "try now: queue-wide pause cleared"
        mgr.pause_all(True)
        assert all(j["paused"] for j in mgr.jobs.values()), "pause all: every playlist paused"
        assert all(r["wait"] == "Playlist paused" for r in mgr.activity(job_id=j5["id"])["queue"]), \
            "pause all: dashboard says why"
        mgr.pause_all(False)
        assert not any(j["paused"] for j in mgr.jobs.values()), "resume all"
    finally:
        mgr.pause_all(False)
        mgr.action(j5["id"], "stop")


@pytest.fixture(scope="module")
def j6(mgr, out):
    """e1 failed because its file went missing; e2 failed and is no longer available."""
    job = make_job(mgr, out, "six", ["e1", "e2"])
    it6 = items_of(job)
    with mgr.cond:
        it6["e1"].update(status="failed", file_problem="missing", error="gone")
        it6["e2"].update(status="failed", removed=True)
        mgr._touch(job)
    return job


def test_retry_all_failed(mgr, out, j6):
    # drops the archive line of a missing file
    it6 = items_of(j6)
    mgr._archive_add(str(out), M.Manager._archive_key(it6["e1"]))
    try:
        n = mgr.retry_failed()
        assert it6["e1"]["status"] == "queued" and it6["e2"]["status"] == "failed", \
            f"retry all failed: requeued (not the unavailable one): {n}"
        assert M.Manager._archive_key(it6["e1"]) not in M.Manager._archive_ids(str(out)), \
            "retry all failed: missing file dropped from the archive"
    finally:
        mgr.action(j6["id"], "stop")


def at(hour, minute=0):
    t = list(time.localtime())
    t[3], t[4], t[5] = hour, minute, 0
    return time.mktime(time.struct_time(t))


def test_download_schedule(mgr, net, j6, opts):
    try:
        mgr.update_settings({"schedule_on": True, "schedule_start": "1:00", "schedule_end": "07:00"})
        assert mgr.settings["schedule_start"] == "01:00", "schedule: times normalised"
        mgr.clock = lambda: at(3)
        assert mgr._in_schedule(), "schedule: inside the window"
        mgr.clock = lambda: at(12)
        assert not mgr._in_schedule() and "schedule" in (mgr.waiting_reason() or ""), \
            f"schedule: outside the window: {mgr.waiting_reason()}"
        mgr.queue(j6["id"], ["e1"], dict(opts))
        with mgr.cond:
            net.level = "good"
            pick = mgr._next_item()
            net.level = "offline"
        assert pick is None, "schedule: nothing starts outside the window"
        mgr.update_settings({"schedule_start": "23:00", "schedule_end": "06:00"})
        mgr.clock = lambda: at(2)
        assert mgr._in_schedule(), "schedule: window across midnight"
        mgr.update_settings({"schedule_on": False, "schedule_start": "bad"})
        assert mgr.settings["schedule_start"] == "01:00", "schedule: bad time falls back"
    finally:
        mgr.clock = time.time
        mgr.update_settings({"schedule_on": False})
        mgr.action(j6["id"], "stop")


class FakeYdl:
    def __init__(self):
        self.params = {}


def test_total_speed_limit(mgr):
    try:
        mgr.update_settings({"rate_limit_mb": 0, "total_rate_mb": 4})
        assert abs(mgr._rate_for(3) - 4 * MB / 3) < 1, "speed: total shared between downloads"
        mgr.update_settings({"rate_limit_mb": 1})
        assert mgr._rate_for(2) == 1 * MB, "speed: per-download limit when lower"
        fakes = [FakeYdl(), FakeYdl()]
        with mgr.cond:
            mgr._ydls.update(fakes)
        try:
            mgr.update_settings({"rate_limit_mb": 0, "total_rate_mb": 2})
            assert all(f_.params["ratelimit"] == 1 * MB for f_ in fakes), \
                f"speed: running downloads rebalanced: {[f_.params for f_ in fakes]}"
        finally:
            with mgr.cond:
                mgr._ydls.difference_update(fakes)
    finally:
        mgr.update_settings({"rate_limit_mb": 0, "total_rate_mb": 0})


@pytest.fixture(scope="module")
def j7(mgr, out, fake):
    return make_job(mgr, out, "seven", ["slow1"])


@pytest.mark.slow
def test_pause_running_video(mgr, net, out, opts, j7, media_server):
    # pause one running video: it stops, keeps its partial download and continues later
    it7 = items_of(j7)["slow1"]
    media_server.throttle = True
    try:
        mgr.queue(j7["id"], None, dict(opts))
        net.level = "good"
        assert wait(lambda: it7["status"] == "downloading" and (it7.get("downloaded") or 0) > 0, 60), \
            f"hold: download running: {(it7['status'], it7.get('error'))}"
        mgr.item_action(j7["id"], "slow1", "hold")
        assert wait(lambda: it7["status"] == "queued" and it7["held"], 30), \
            f"hold: running video goes back to the queue, paused: {it7['status']}"
        time.sleep(2.5)
        assert it7["status"] == "queued", "hold: not restarted while paused"
        assert any(p.name.endswith(".part") for p in out.iterdir() if "[slow1]" in p.name), \
            f"hold: partial download kept: {[p.name for p in out.iterdir()]}"
        media_server.throttle = False
        mgr.item_action(j7["id"], "slow1", "unhold")
        assert wait(lambda: it7["status"] == "done", 90), f"unhold: finishes: {(it7['status'], it7.get('error'))}"
    finally:
        media_server.throttle = False
        net.level = "offline"


def test_file_actions(mgr, net, out, opts, j6, j7, monkeypatch):
    # open, delete and mark files
    it7 = items_of(j7)["slow1"]
    if it7["status"] != "done":                      # run on its own: download it first
        mgr.item_action(j7["id"], "slow1", "unhold")
        mgr.queue(j7["id"], None, dict(opts))
        net.level = "good"
        wait(lambda: it7["status"] == "done", 90)
        net.level = "offline"
    opened = []
    monkeypatch.setattr(M, "_open_path", lambda path, reveal=False: opened.append((path, reveal)))
    mgr.item_action(j7["id"], "slow1", "reveal")
    mgr.item_action(j7["id"], "slow1", "play")
    assert opened == [(it7["filepath"], True), (it7["filepath"], False)], f"reveal/play: the video's own file: {opened}"
    with pytest.raises(ValueError):
        mgr.item_action(j6["id"], "e2", "play")          # play: refused without a file
    keep = out / "Unrelated [slow1x].mp4"
    keep.write_bytes(b"x")
    (out / "Video slow1 [slow1].en.srt").write_text("1")
    mgr.item_action(j7["id"], "slow1", "delete-file")
    assert it7["status"] == "new" and not any("[slow1]" in p.name for p in out.iterdir()), \
        f"delete file: video's files gone, status not downloaded: {[p.name for p in out.iterdir()]}"
    assert keep.exists(), "delete file: other files kept"
    assert M.Manager._archive_key(it7) not in M.Manager._archive_ids(str(out)), "delete file: archive line removed"
    mgr.item_action(j7["id"], "slow1", "mark-done")
    assert it7["status"] == "skipped" and M.Manager._archive_key(it7) in M.Manager._archive_ids(str(out)), \
        "mark as downloaded: skipped and archived"


@pytest.mark.slow
def test_export_m3u(mgr, out, downloaded):
    m3u = mgr.export_m3u(downloaded.job["id"])
    text = Path(m3u["path"]).read_text()
    assert m3u["count"] == 1 and Path(m3u["path"]).parent == out, f"m3u: written in the playlist folder: {m3u}"
    assert "#EXTINF:30," in text and os.path.basename(downloaded.it["ok1"]["filepath"]) in text, \
        f"m3u: relative path and duration: {text}"


@pytest.mark.slow
def test_second_opinion_before_damaged(mgr, media, good, tmp_path, downloaded, monkeypatch):
    # no false "damaged": a file ffprobe can't open but that decodes fully is kept
    j3, it3 = downloaded.job, downloaded.it
    assert M._decodes_fully(str(media / "full.mp4"), 30), "full decode: good file plays"
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(os.urandom(50000))
    assert not M._decodes_fully(str(junk), 30), "full decode: garbage fails"
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(good[: len(good) // 2])
    assert not M._decodes_fully(str(cut), 30), "full decode: cut-off file fails"
    monkeypatch.setattr(M, "_verify_file", lambda path, duration: M.DAMAGED)
    mgr.start_check(j3["id"])
    wait(lambda: not j3["check"]["running"])
    monkeypatch.undo()
    assert it3["ok1"]["status"] == "done" and os.path.exists(it3["ok1"]["filepath"]) and j3["check"]["damaged"] == 0, \
        f"check: file that plays is not renamed: {(it3['ok1']['status'], j3['check'])}"
    assert any("plays to the end" in line for line in it3["ok1"]["log"]), "check: second opinion logged"


def test_remove_with_files(mgr, out, tmp_path):
    # remove a playlist with its files (only its own, tagged files)
    rm = tmp_path / "rm"
    rm.mkdir()
    j8 = make_job(mgr, out, "eight", ["r1", "r2"])
    j9 = make_job(mgr, out, "nine", ["r2"])                 # another playlist in the same folder shares r2
    with mgr.cond:
        for j in (j8, j9):
            j["outdir"] = str(rm)
            for it in j["items"]:
                it.update(outdir=str(rm), status="done")
    for name in ("01 - Video r1 [r1].mp4", "Video r1 [r1].jpg", "02 - Video r2 [r2].mp4", "notes [other].txt"):
        (rm / name).write_bytes(b"x")
    mgr._archive_add(str(rm), "test r1")
    mgr._archive_add(str(rm), "test r2")
    mgr.remove(j8["id"], delete_files=True)
    assert j8["id"] not in mgr.jobs, "remove: playlist gone at once"
    assert wait(lambda: not (rm / "01 - Video r1 [r1].mp4").exists(), 10) and not (rm / "Video r1 [r1].jpg").exists(), \
        f"remove + delete: own files deleted: {[p.name for p in rm.iterdir()]}"
    assert (rm / "02 - Video r2 [r2].mp4").exists(), "remove + delete: file another playlist still has is kept"
    assert (rm / "notes [other].txt").exists(), "remove + delete: unrelated file kept"
    assert M.Manager._archive_ids(str(rm)) == {"test r2"}, \
        f"remove + delete: archive lines of deleted files dropped: {M.Manager._archive_ids(str(rm))}"


def test_subtitle_and_sponsorblock_options():
    o, pps = M.build_format_opts({"quality": "720", "subtitles": True, "sub_langs": "en, fa", "auto_subs": True})
    assert o["subtitleslangs"] == ["en", "en-.*", "fa", "fa-.*"], \
        f"subs: chosen languages with regional variants: {o['subtitleslangs']}"
    assert o.get("writeautomaticsub") is True, "subs: auto-generated subtitles"
    o, pps = M.build_format_opts({"quality": "720", "subtitles": True, "sub_langs": "../etc; $(x)"})
    assert o["subtitleslangs"] == ["en", "en-.*"], f"subs: bad language codes fall back to English: {o['subtitleslangs']}"
    o, pps = M.build_format_opts({"quality": "720", "sponsorblock": "remove"})
    keys = [p_["key"] for p_ in pps]
    mc = next(p_ for p_ in pps if p_["key"] == "ModifyChapters")
    assert keys.index("ModifyChapters") < keys.index("FFmpegMetadata") \
        and mc["remove_sponsor_segments"] == set(M.SPONSOR_CATEGORIES), \
        f"sponsorblock: segments cut before metadata is written: {(keys, mc)}"
    o, pps = M.build_format_opts({"quality": "720", "sponsorblock": "mark", "metadata": False})
    assert any(p_["key"] == "FFmpegMetadata" and p_["add_chapters"] and not p_["add_metadata"] for p_ in pps) \
        and next(p_ for p_ in pps if p_["key"] == "ModifyChapters")["remove_sponsor_segments"] == set(), \
        "sponsorblock: mark adds chapters even without metadata"


def test_sections():
    # parts of a video
    assert M._parse_section({"start": "1:00", "end": "2:30.5"}) == {"start": 60.0, "end": 150.5}, "section: times parsed"
    assert M._parse_section({"start": "90"}) == {"start": 90.0, "end": None}, "section: empty end = to the end"
    assert M._parse_section({"start": "", "end": ""}) is None, "section: nothing asked"
    assert M._section_label({"start": 60, "end": 150}) == "1-00–2-30", "section: file-name label"
    assert M._expected_duration({"duration": 300, "options": {"section": {"start": 60, "end": None}}}) == 240, \
        "section: expected length"


BAD_SECTIONS = [{"start": "abc"}, {"start": "2:00", "end": "1:00"}]


@pytest.mark.parametrize("bad", BAD_SECTIONS, ids=[f"section: rejects {b}" for b in BAD_SECTIONS])
def test_section_rejects(bad):
    with pytest.raises(ValueError):
        M._parse_section(bad)


@pytest.mark.slow
def test_sponsorblock_and_parts_downloads(mgr, net, out, opts, fake, monkeypatch):
    # real downloads with SponsorBlock (patched: no internet) and with a part
    monkeypatch.setattr(M.SponsorBlockPP, "EXTRACTORS", {**M.SponsorBlockPP.EXTRACTORS, "Test": "YouTube"})
    segments = {"mode": "fail"}

    def fake_segments(self, video_id, service):
        if segments["mode"] == "fail":
            raise OSError("network is unreachable")
        return [{"segment": [5.0, 10.0], "category": "sponsor", "UUID": "u1", "videoDuration": 30,
                 "actionType": "skip", "description": ""}]

    monkeypatch.setattr(M.SponsorBlockPP, "_get_sponsor_segments", fake_segments)
    j10 = make_job(mgr, out, "ten", ["sb1", "sb2", "part1"])
    it10 = items_of(j10)
    try:
        mgr.queue(j10["id"], ["sb1"], dict(opts, sponsorblock="remove"))
        net.level = "good"
        assert wait(lambda: it10["sb1"]["status"] == "done", 90), \
            f"sponsorblock: unavailable API doesn't fail the download: {(it10['sb1']['status'], it10['sb1']['error'])}"
        assert any("SponsorBlock segments unavailable" in line for line in it10["sb1"]["log"]), \
            f"sponsorblock: failure noted in the log: {it10['sb1']['log'][-4:]}"
        segments["mode"] = "ok"
        mgr.queue(j10["id"], ["sb2"], dict(opts, sponsorblock="remove"))
        assert wait(lambda: it10["sb2"]["status"] in ("done", "failed"), 120) and it10["sb2"]["status"] == "done", \
            "sponsorblock: video with a segment cut is verified, not called incomplete: " \
            f"{(it10['sb2']['status'], it10['sb2']['error'], it10['sb2']['log'][-3:])}"
        real = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     it10["sb2"]["filepath"]], capture_output=True, text=True).stdout or 0)
        assert 23 < real < 27 and abs(it10["sb2"]["media_duration"] - real) < 2, \
            f"sponsorblock: 5 s segment removed: {(real, it10['sb2'].get('media_duration'))}"
        mgr.queue(j10["id"], ["part1"], dict(opts, section={"start": "0:05", "end": "0:15"}))
        assert wait(lambda: it10["part1"]["status"] in ("done", "failed"), 120) and it10["part1"]["status"] == "done", \
            f"section: part downloaded and verified: {(it10['part1']['status'], it10['part1']['error'])}"
        assert "(0-05–0-15) [part1]" in os.path.basename(it10["part1"]["filepath"] or ""), \
            f"section: own file name: {it10['part1']['filepath']}"
        assert M.Manager._archive_key(it10["part1"]) not in M.Manager._archive_ids(str(out)), \
            "section: not written to the archive"
        assert "section" not in j10["options"], "section: playlist keeps no part in its saved options"
    finally:
        net.level = "offline"
    mgr.start_check(j10["id"])
    wait(lambda: not j10["check"]["running"])
    assert j10["check"]["damaged"] == 0 and it10["part1"]["status"] == "done" and it10["sb2"]["status"] == "done", \
        f"check files: part and cut video count as healthy: {j10['check']}"


def test_size_estimate(mgr, j6, tmp_path, monkeypatch):
    est = mgr.estimate(j6["id"], None, {"quality": "1080"})
    assert est["bytes"] == int(30 * 5e6 / 8) and est["count"] == 1 and est["free"] > 0, f"estimate: duration × bitrate: {est}"
    est = mgr.estimate(j6["id"], ["e1"], {"quality": "mp3", "section": {"start": "0", "end": "0:10"}})
    assert est["bytes"] == int(10 * 160e3 / 8), f"estimate: audio, part of a video: {est}"
    made = []
    with monkeypatch.context() as mp:
        mp.setattr(Path, "mkdir", lambda self, *a, **k: made.append(self))
        d = M._disk_free(str(tmp_path / "not" / "there" / "yet"))
    assert d["free"] > 0 and not made and not (tmp_path / "not").exists(), \
        f"disk free: nearest existing folder, nothing created: {d}"


@pytest.mark.skipif(sys.platform != "darwin", reason="external drives are under /Volumes on macOS")
def test_disk_free_unplugged_drive():
    assert M._disk_free("/Volumes/NoSuchDrive123/x")["problem"], "disk free: unplugged drive reported"


@pytest.fixture(scope="module")
def subs(mgr, out, opts):
    """A subscribed playlist: each fetch finds one more video (s3, then s4...)."""
    fetches = {"n": 0}

    def fake_inspect(url, settings):
        fetches["n"] += 1
        ids = ["s1", "s2"] + [f"s{2 + k}" for k in range(1, fetches["n"])]
        return {"kind": "playlist", "source_id": "subs", "title": "List subs", "thumbnail": None, "heights": [360],
                "items": [{"index": i + 1, "id": v, "title": f"Video {v}", "url": f"test://{v}", "duration": 30,
                           "extractor": "Test"} for i, v in enumerate(ids)]}

    mp = pytest.MonkeyPatch()
    mp.setattr(M, "inspect_url", fake_inspect)
    js = mgr.open_url("test://list-subs")
    jsub = mgr.jobs[js["id"]]
    with mgr.cond:
        jsub["outdir"] = str(out)
        jsub["options"] = dict(opts)
    yield jsub
    mp.undo()


def test_subscriptions(mgr, net, subs):
    # due playlists are refreshed and new videos queued
    jsub = subs
    with pytest.raises(ValueError):
        mgr.set_sync(jsub["id"], 5, True)                  # sync: only offered intervals accepted
    mgr.set_sync(jsub["id"], 6, True)
    assert mgr.sync_due() == [], "sync: not due right after it's set"
    with mgr.cond:
        jsub["sync"]["last"] = time.time() - 7 * 3600
    net.level = "offline"
    assert mgr.sync_due() == [], "sync: waits while offline"
    net.level = "good"
    mgr.pause_all(True)                     # keep it from downloading; queueing must not resume the playlist
    try:
        assert mgr.sync_due() == [jsub["id"]], "sync: due playlist refreshed"
        sub_items = items_of(jsub)
        assert sub_items["s3"]["status"] == "queued" and sub_items["s1"]["status"] == "new", \
            f"sync: new video queued automatically: { {k: v['status'] for k, v in sub_items.items()} }"
        assert jsub["paused"], "sync: paused playlist stays paused"
        assert time.time() - jsub["sync"]["last"] < 5, "sync: last check recorded"
    finally:
        mgr.pause_all(False)
        mgr.action(jsub["id"], "stop")
        net.level = "offline"


def test_save_options(mgr, net, out, opts, subs):
    # a playlist's Save button: options kept without queueing anything, and used by auto-download
    jsub = subs
    saved_dir = out / "saved"
    mgr.set_options(jsub["id"], dict(opts, quality="720", sponsorblock="mark", output_dir=str(saved_dir),
                                     section={"start": "0:05", "end": ""}))
    assert jsub["options"]["quality"] == "720" and jsub["options"]["sponsorblock"] == "mark" \
        and jsub["outdir"] == str(saved_dir) and saved_dir.is_dir() \
        and not any(i["status"] == "queued" for i in jsub["items"]), \
        f"save options: stored without queueing: {(jsub['options'], jsub['outdir'])}"
    assert "section" not in jsub["options"] and "output_dir" not in jsub["options"], \
        f"save options: a part of a video and the folder aren't stored as options: {jsub['options']}"
    if sys.platform == "darwin":
        with pytest.raises(OSError):
            mgr.set_options(jsub["id"], dict(opts, output_dir="/Volumes/No Such Drive ytdl/x"))
        assert jsub["outdir"] == str(saved_dir) and jsub["options"]["quality"] == "720", \
            "save options: a folder on an unplugged drive is refused, nothing changes"
    if not jsub.get("sync"):
        mgr.set_sync(jsub["id"], 6, True)
    with mgr.cond:
        jsub["sync"]["last"] = time.time() - 7 * 3600
    known = set(items_of(jsub))
    net.level = "good"
    mgr.pause_all(True)
    try:
        mgr.sync_due()
        new = [it for i, it in items_of(jsub).items() if i not in known]
        s4 = new[0] if len(new) == 1 else {}
        assert s4.get("status") == "queued" and (s4.get("options") or {}).get("quality") == "720" \
            and s4.get("outdir") == str(saved_dir), \
            "save options: auto-download of a new video uses the saved options: " \
            f"{(s4.get('status'), s4.get('options'), s4.get('outdir'))}"
    finally:
        mgr.pause_all(False)
        mgr.action(jsub["id"], "stop")
        net.level = "offline"


def test_add_many_links(mgr, monkeypatch):
    def fake_inspect_many(url, settings):
        if "broken" in url:
            raise DownloadError("ERROR: Unsupported URL: " + url)
        vid = url.rsplit("/", 1)[-1]
        return {"kind": "video", "source_id": vid, "title": f"Single {vid}", "thumbnail": None, "heights": [360],
                "items": [{"index": 1, "id": vid, "title": f"Single {vid}", "url": url, "duration": 30,
                           "extractor": "Test"}]}

    monkeypatch.setattr(M, "inspect_url", fake_inspect_many)
    res = mgr.open_many(["https://x.test/m1", "not a link", "https://x.test/m1", "https://x.test/broken",
                         "https://x.test/m2"])
    assert res["total"] == 3, f"import: links cleaned and de-duplicated: {res}"
    assert wait(lambda: not mgr.importing["running"], 30), "import: finishes"
    assert mgr.importing["added"] == 2 and len(mgr.importing["failed"]) == 1 \
        and "Unsupported" in mgr.importing["failed"][0]["error"], f"import: progress and failures reported: {mgr.importing}"
    assert mgr.overview()["import"]["done"] == 3, "import: in the overview"


def test_ytdlp_update(mgr, monkeypatch):
    # no network: command and version lookup replaced
    info = mgr.ytdlp_info()
    assert info["version"] == M.yt_dlp.version.__version__, "ytdlp: version reported"
    monkeypatch.setattr(mgr, "_update_cmd", lambda: [sys.executable, "-c", "print('updated')"], raising=False)
    monkeypatch.setattr(mgr, "_installed_ytdlp", lambda: "2099.01.01", raising=False)
    mgr.update_ytdlp()
    assert wait(lambda: not mgr.updating["running"], 30) and mgr.updating["ok"] and mgr.updating["restart"] \
        and "updated" in mgr.updating["output"], f"ytdlp: update runs and asks for a restart: {mgr.updating}"


# ---------- a second library: backfill, export/import, settings, cookies.txt

@pytest.fixture(scope="module")
def lib2(mgr, tmp, out, good, downloaded):
    """A library from before the event log, with one downloaded video (a copy of playlist "three")."""
    j3, it3 = downloaded.job, downloaded.it
    lib = tmp / "lib2"
    lib.mkdir()
    f = out / "Old [old1].mp4"
    f.write_bytes(good)
    os.utime(f, (1_700_000_000, 1_700_000_000))
    old_job = {k: v for k, v in j3.items() if k != "items"} | {"id": "oldjob", "rev": 3, "items": [
        dict(it3["ok1"], id="old1", title="Old", status="done", filepath=str(f), finished=None, rev=3)]}
    con = sqlite3.connect(lib / "library.db")
    con.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
                      "CREATE TABLE items (job_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, "
                      "PRIMARY KEY (job_id, id));")
    con.execute("INSERT INTO jobs VALUES (?, ?)", ("oldjob", json.dumps({k: v for k, v in old_job.items() if k != "items"})))
    con.execute("INSERT INTO items VALUES (?, ?, ?)", ("oldjob", "old1", json.dumps(old_job["items"][0])))
    con.commit()
    con.close()
    before = use_data_dir(lib)
    m2 = new_manager(lambda: "offline")
    yield m2
    stop_manager(m2)
    restore_paths(before)


@pytest.mark.slow
def test_backfill_events(lib2, good):
    # a library from before the event log gets its downloads added once
    rows = lib2.history()["rows"]
    assert len(rows) == 1 and rows[0]["id"] == "old1" and int(rows[0]["finished"]) == 1_700_000_000 \
        and rows[0]["bytes"] == len(good), f"backfill: earlier download added, dated by its file: {rows}"
    assert lib2.store.version == 1, "backfill: done once (version recorded)"


def expected(st):
    return "queued" if st in M.ACTIVE else st


@pytest.mark.slow
def test_export_import(mgr, lib2, out, tmp, good, downloaded):
    # export all data, import it into another library (merge, never delete)
    m2, j3 = lib2, downloaded.job
    make_job(mgr, out, "export", ["x1", "x2"])        # at least one playlist besides "three"
    data = json.loads(json.dumps(mgr.export_data()))          # must survive JSON
    n_items = sum(len(j["items"]) for j in mgr.jobs.values())
    assert len(data["jobs"]) == len(mgr.jobs) and sum(len(j["items"]) for j in data["jobs"]) == n_items \
        and len(data["events"]) == mgr.store.count_events(hidden=True) > 0, \
        f"export: every playlist, video and event: {(len(data['jobs']), len(data['events']))}"
    assert data["app"] == M.EXPORT_APP and data["format"] == M.EXPORT_FORMAT and data["settings"]["output_dir"] == str(out) \
        and not any(k in it for j in data["jobs"] for it in j["items"] for k in M.TRANSIENT), \
        "export: settings, marker, no runtime fields"
    assert all(j["summary"]["videos"] == len(j["items"]) for j in data["jobs"]) \
        and sum(j["summary"]["downloaded"] for j in data["jobs"]) \
        == sum(it["status"] in ("done", "skipped") for j in data["jobs"] for it in j["items"]
               if not (it.get("removed") and it["status"] in ("new", "failed"))), "export: summary per playlist"
    m2_out = m2.settings["output_dir"]
    done_before = sum(it["status"] == "done" for it in m2.jobs["oldjob"]["items"])   # oldjob is a copy of j3's playlist
    r = m2.import_data(data, check_files=False)
    assert r["playlists_added"] == len(mgr.jobs) - 1 and r["playlists_merged"] == 1 and r["videos_added"] == n_items \
        and r["events_added"] == len(data["events"]) and r["skipped"] == 0, \
        f"import data: playlists, videos and history added: {r}"
    assert "oldjob" in m2.jobs and j3["id"] not in m2.jobs and set(items_of(j3)) <= set(items_of(m2.jobs["oldjob"])), \
        "import data: the same playlist is merged, not duplicated"
    assert not r["settings"] and m2.settings["output_dir"] == m2_out, "import data: settings kept unless asked"
    got = {it["id"]: it["status"] for j in m2.jobs.values() for it in j["items"]}
    assert all(got[it["id"]] == expected(it["status"]) or (it["status"] == "new" and got[it["id"]] == "skipped")
               for j in data["jobs"] for it in j["items"]), \
        "import data: every video keeps its state (interrupted ones queued again): " \
        f"{[(it['id'], it['status'], got[it['id']]) for j in data['jobs'] for it in j['items'] if got[it['id']] != expected(it['status'])]}"
    assert {k[1] for k in m2._live} == {i for i, st in got.items() if st in M.LIVE}, \
        "import data: queued videos are in the scheduler"
    assert sum(it["status"] == "done" for j in m2.jobs.values() for it in j["items"]) \
        == sum(it["status"] == "done" for j in mgr.jobs.values() for it in j["items"]) + done_before > 0, \
        "import data: done videos stay done"
    assert len(m2.store.load()) == len(m2.jobs) == len(mgr.jobs) \
        and sum(len(j["items"]) for j in m2.store.load()) == n_items + 1, "import data: saved to the database"
    r = m2.import_data(data, check_files=False)
    assert r["playlists_added"] == 0 and r["playlists_merged"] == len(mgr.jobs) and r["videos_added"] == 0 \
        and r["events_added"] == 0, f"import data: twice adds nothing: {r}"
    src = next(j for j in data["jobs"] if j["kind"] == "playlist" and j["items"] and j["id"] != j3["id"])
    local = m2.jobs[src["id"]]
    local_numbers = {it["number"] for it in local["items"]}
    extra = dict(src["items"][0], id="extra1", title="Extra", status="queued")   # same file number as a known video
    r = m2.import_data(data | {"jobs": [src | {"items": src["items"] + [extra]}], "events": []}, check_files=False)
    new = items_of(local).get("extra1")
    assert r["videos_added"] == 1 and r["playlists_merged"] == 1 and new, f"import data: merge adds only the missing video: {r}"
    assert new["number"] not in local_numbers and new["index"] == max(it["index"] for it in local["items"]) \
        and new["status"] == "queued", \
        f"import data: merged video gets a free number, goes last, keeps its state: {(new['number'], local_numbers, new['status'])}"
    r = m2.import_data(data | {"jobs": [], "events": [], "settings": data["settings"] | {"concurrency": 5}}, True, False)
    assert r["settings"] and m2.settings["concurrency"] == 5, f"import data: settings when asked: {r}"
    with pytest.raises(ValueError):
        m2.import_data({"app": "other", "jobs": []})    # import data: refuses a foreign file
    with pytest.raises(ValueError):
        m2.import_data(data | {"format": 99})           # import data: refuses a newer format
    with pytest.raises(ValueError):
        m2.import_data([1, 2])                          # import data: refuses a non-object
    r = m2.import_data(data | {"jobs": [{"id": "x"}, src | {"id": "new-id", "url": "test://other", "source_id": "other",
                                                          "items": [{"id": "y"}, *src["items"]]}], "events": []},
                       check_files=False)
    assert r["skipped"] == 2 and r["playlists_added"] == 1 and r["videos_added"] == len(src["items"]), \
        f"import data: bad entries skipped, the rest kept: {r}"

    # the folder of an imported playlist is checked: good files found, recorded files confirmed, missing ones flagged
    fold = tmp / "imported"
    fold.mkdir()
    (fold / "Here [have1].mp4").write_bytes(good)
    (fold / "Kept [kept1].mp4").write_bytes(good)
    vid = lambda i, **k: {"index": i, "id": k.pop("id"), "title": "T", "extractor": "Test", "duration": 30,
                          "url": "test://x", **k}
    r = m2.import_data({"app": M.EXPORT_APP, "format": 1, "jobs": [{
        "id": "folderjob", "url": "test://folder", "source_id": "folder", "title": "Folder", "kind": "playlist",
        "outdir": str(fold), "items": [vid(1, id="have1", status="new"),
                                       vid(2, id="kept1", status="done", filepath=str(fold / "Kept [kept1].mp4")),
                                       vid(3, id="gone1", status="done", filepath=str(fold / "Gone [gone1].mp4")),
                                       vid(4, id="todo1", status="new")]}]})
    fj = m2.jobs["folderjob"]
    assert r["checking"] == 1 and wait(lambda: (fj.get("check") or {}).get("finished"), 60), \
        f"import data: folders checked after import: {fj.get('check')}"
    fi = items_of(fj)
    assert fi["have1"]["status"] == "skipped" and fi["kept1"]["status"] == "done" and fi["kept1"]["health"] == "ok" \
        and fi["gone1"]["status"] == "failed" and fi["gone1"].get("file_problem") and fi["todo1"]["status"] == "new", \
        "import data: check finds files, confirms done ones, flags missing ones: " \
        f"{ {k: (v['status'], v.get('file_problem')) for k, v in fi.items()} }"


SETTING_VALUES = {"concurrency": 5, "cookies_browser": "firefox", "cookies_profile": "abc.default-release",
                  "rate_limit_mb": 1.5, "sleep_between": 3.0, "proxy": "http://127.0.0.1:9", "wait_for_network": False,
                  "total_rate_mb": 4.0, "schedule_on": True, "schedule_start": "02:30", "schedule_end": "06:15",
                  "sub_langs": "en, fa", "notify": True}


@pytest.mark.slow
def test_settings_round_trip(lib2, tmp):
    # every setting survives Save and a reload ("Desktop notifications" came back unticked)
    m2 = lib2
    before = dict(m2.settings)
    values = {"output_dir": str(tmp / "elsewhere"), **SETTING_VALUES}
    assert set(values) == set(M.DEFAULT_SETTINGS), \
        f"settings: the round-trip covers every setting: {set(M.DEFAULT_SETTINGS) ^ set(values)}"
    assert all(values[k] != M.DEFAULT_SETTINGS[k] for k in values), "settings: every test value differs from its default"
    try:
        saved = m2.update_settings(values)
        reread = M._coerce_settings(json.loads(M.SETTINGS_FILE.read_text()))
        assert saved == values and reread == values, "settings: every value saved and read back unchanged: " \
            f"{ {k: (saved.get(k), reread.get(k)) for k in values if saved.get(k) != values[k] or reread.get(k) != values[k]} }"
        assert m2.overview()["settings"]["notify"] is True, "settings: sent to the page"
    finally:
        m2.update_settings(before)


NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc\n"
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1999999999\tLOGIN_INFO\txyz\n"
            ".example.com\tTRUE\t/\tFALSE\t0\tother\t1\n")
BAD_COOKIES = {"cookies file: refuses not cookies.txt": "hello, these are not cookies",
               "cookies file: refuses no YouTube cookies": ".example.com\tTRUE\t/\tFALSE\t0\tother\t1\n"}


@pytest.mark.slow
@pytest.mark.parametrize("text", BAD_COOKIES.values(), ids=BAD_COOKIES.keys())
def test_cookies_file_refused(lib2, text):
    with pytest.raises(ValueError):
        lib2.import_cookies(text)
    assert not M._cookie_file().exists()


@pytest.mark.slow
def test_cookies_file(lib2, monkeypatch):
    # import a cookies.txt (Netscape format), remove it again
    m2 = lib2
    m2._hold_queue("YouTube is limiting requests", 600, "ratelimit")
    m2.rate_probe = True
    st = m2.import_cookies(NETSCAPE)
    assert st.get("youtube") == 2 and st.get("count") == 3 and st.get("signed_in") \
        and m2.overview()["cookie_file"]["youtube"] == 2, f"cookies file: imported with counts: {st}"
    assert (M._cookie_file().stat().st_mode & 0o077) == 0, \
        f"cookies file: readable only by the user: {oct(M._cookie_file().stat().st_mode)}"
    assert m2.hold is None and not m2.rate_probe, f"cookies file: importing ends the rate-limit pause at once: {m2.hold}"
    seen = []
    real_ydl = M.yt_dlp.YoutubeDL

    class SpyYDL(real_ydl):
        def __init__(self, params=None, *a, **k):
            seen.append(dict(params or {}))
            super().__init__(params, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(M.yt_dlp, "YoutubeDL", SpyYDL)
        opts = M._base_opts({"cookies_browser": "chrome"})
        with M._ytdl(opts) as y1, M._ytdl(opts) as y2:
            paths = [y1.params.get("cookiefile"), y2.params.get("cookiefile")]
            copies_ok = all(p and Path(p).read_text() == NETSCAPE for p in paths)
    assert paths[0] != paths[1] and copies_ok and all(p != str(M._cookie_file()) for p in paths), \
        f"cookies file: each download gets its own copy (yt-dlp writes cookies back): {paths}"
    assert all("cookiesfrombrowser" not in s for s in seen), f"cookies file: used instead of the browser's cookies: {seen}"
    assert not any(Path(p).exists() for p in paths), "cookies file: copies removed afterwards"
    assert "SAPISID" not in json.dumps(m2.export_data()), "cookies file: never in the data export"
    assert m2.remove_cookies() and not M._cookie_file().exists() and m2.overview()["cookie_file"] is None, \
        "cookies file: removed"
