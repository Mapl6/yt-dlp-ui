"""The guarantees in .claude/rules/download-core.md: the archive line only after verification, never the same
video twice at once, nothing lost after a crash, deletes only for files whose last [id] tag matches."""
import subprocess
import sys
import textwrap

import pytest

from ytdl import config, verify
from ytdl import manager as M
from helpers import ROOT, items_of, make_job, wait

OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "numbering": False}
key = M.Manager._archive_key


def download(mgr, net, job, out, timeout=90):
    mgr.queue(job["id"], None, dict(OPTS, output_dir=str(out)))
    net.level = "good"
    try:
        wait(lambda: all(it["status"] in ("done", "failed") or (it["status"] == "queued" and it["error"])
                         for it in job["items"]), timeout)
    finally:
        net.level = "offline"


@pytest.mark.slow
def test_archive_written_only_after_verification(mgr, net, out, fake, monkeypatch):
    events = []
    real_verify, real_add = verify._verify_file, mgr._archive_add

    def spy_verify(path, duration):
        result = real_verify(path, duration)
        events.append(("verify", result))
        return result

    def add(outdir, k):
        events.append(("archive", k))
        return real_add(outdir, k)

    monkeypatch.setattr(verify, "_verify_file", spy_verify)
    monkeypatch.setattr(mgr, "_archive_add", add)
    job = make_job(mgr, out, "verified", ["ok2"])
    it = items_of(job)["ok2"]
    download(mgr, net, job, out)
    assert it["status"] == "done", (it["status"], it["error"])
    adds = [i for i, e in enumerate(events) if e == ("archive", key(it))]
    passes = [i for i, e in enumerate(events) if e == ("verify", None)]
    assert len(adds) == 1 and passes and passes[0] < adds[0], events


@pytest.mark.slow
def test_failed_verification_never_archived(mgr, net, out, fake, monkeypatch):
    monkeypatch.setattr(verify, "_verify_file", lambda path, duration: "The end of the file is missing")
    job = make_job(mgr, out, "unverified", ["bad2"])
    it = items_of(job)["bad2"]
    try:
        download(mgr, net, job, out)
        assert it["status"] != "done", "a file that fails verification is never done"
        assert key(it) not in M.Manager._archive_ids(str(out)), "and never in the archive"
    finally:
        mgr.action(job["id"], "stop")


@pytest.mark.slow
def test_wrong_stream_size_never_archived(mgr, net, out, fake):
    fake.audio_extra = 1000
    job = make_job(mgr, out, "wrongsize", ["size2"])
    it = items_of(job)["size2"]
    try:
        download(mgr, net, job, out)
        assert it["status"] == "queued" and "wrong size" in (it["error"] or ""), (it["status"], it["error"])
        assert key(it) not in M.Manager._archive_ids(str(out))
    finally:
        fake.audio_extra = 0
        mgr.action(job["id"], "stop")


@pytest.mark.slow
def test_part_of_a_video_ignores_the_archive(mgr, net, out, fake):
    """A part (options.section) has its own file name and never reads or writes the archive: the whole video
    being downloaded already doesn't skip it, and finishing it doesn't add a line."""
    job = make_job(mgr, out, "part", ["pt1"])
    it = items_of(job)["pt1"]
    mgr._archive_add(str(out), key(it))
    lines = (out / config.ARCHIVE_NAME).read_text().count(key(it))
    try:
        mgr.queue(job["id"], None, dict(OPTS, output_dir=str(out), section={"start": "0:01", "end": "0:04"}))
        net.level = "good"
        assert wait(lambda: it["status"] in ("done", "failed", "skipped"), 90)
    finally:
        net.level = "offline"
    assert it["status"] == "done", f"downloaded, not skipped: {(it['status'], it['error'])}"
    assert "(0-01–0-04) [pt1]" in (it["filepath"] or ""), it["filepath"]
    assert (out / config.ARCHIVE_NAME).read_text().count(key(it)) == lines, "the archive is unchanged"
    mgr._archive_remove(str(out), key(it))


def test_same_video_never_twice_at_once(mgr, net, out):
    """Two playlists with the same video in the same folder: while one downloads it, the other waits."""
    a = make_job(mgr, out, "twinA", ["twin"])
    b = make_job(mgr, out, "twinB", ["twin"])
    mgr.queue(a["id"], None, dict(OPTS))
    mgr.queue(b["id"], None, dict(OPTS))
    try:
        with mgr.cond:
            net.level = "good"
            first = mgr._next_item()
            assert first and first[1]["id"] == "twin"
            mgr.inflight.add(mgr._flight_key(*first))
            second = mgr._next_item()
            net.level = "offline"
            assert second is None, f"the twin in the other playlist must wait: {second and second[0]['id']}"
            mgr.inflight.discard(mgr._flight_key(*first))
    finally:
        net.level = "offline"
        mgr.action(a["id"], "stop")
        mgr.action(b["id"], "stop")


def test_different_folders_may_download_together(mgr, net, out, tmp_path):
    a = make_job(mgr, out, "folderA", ["same"])
    b = make_job(mgr, out, "folderB", ["same"])
    mgr.queue(a["id"], None, dict(OPTS))
    mgr.queue(b["id"], None, dict(OPTS, output_dir=str(tmp_path)))
    try:
        assert mgr._flight_key(a, items_of(a)["same"]) != mgr._flight_key(b, items_of(b)["same"])
    finally:
        mgr.action(a["id"], "stop")
        mgr.action(b["id"], "stop")


CRASH = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1] + "/backend")
    from ytdl import manager as M
    m = M.Manager()
    m.net_level = lambda: "offline"
    with m.cond:
        job = m._create("test://crash", {"kind": "playlist", "source_id": "crash", "title": "Crash", "thumbnail": None,
                        "heights": [360], "items": [{"index": i + 1, "id": v, "title": v, "url": "test://" + v,
                        "duration": 30, "extractor": "Test"} for i, v in enumerate(["c1", "c2", "c3"])]})
        m._touch(job)
    m.save_state()
    c1, c2, c3 = job["items"]
    m._set(job, c1, status="done", filepath="/x/c1.mp4")     # saved by the background saver, not explicitly
    m._set(job, c2, status="downloading")                    # interrupted mid-download
    time.sleep(float(sys.argv[2]))
    os._exit(9)                                              # a crash: no cleanup, no final save
""")


@pytest.mark.slow
def test_crash_loses_nothing_saved(tmp_path, make_manager):
    data = tmp_path / "crashlib"
    script = tmp_path / "crash.py"
    script.write_text(CRASH)
    r = subprocess.run([sys.executable, str(script), str(ROOT), "4.5"], env={"YTDL_DATA_DIR": str(data),
                       "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 9, r.stderr[-500:]
    m = make_manager(data)
    job = next(j for j in m.jobs.values() if j["url"] == "test://crash")
    its = items_of(job)
    assert its["c1"]["status"] == "done" and its["c1"]["filepath"] == "/x/c1.mp4", "the saver wrote the change"
    assert its["c2"]["status"] == "queued", "an interrupted download resumes after the restart"
    assert its["c3"]["status"] == "new"
    assert (job["id"], "c2") in m._live


@pytest.mark.slow
def test_damaged_database_falls_back_to_backup(tmp_path, make_manager):
    data = tmp_path / "dmg"
    m = make_manager(data)
    with m.cond:
        job = m._create("test://dmg", {"kind": "video", "source_id": "d", "title": "D", "thumbnail": None,
                                       "heights": [360], "items": [{"index": 1, "id": "d1", "title": "d1",
                                                                    "url": "test://d1", "duration": 30,
                                                                    "extractor": "Test"}]})
        m._touch(job)
    m.save_state()
    m.store.backup()
    m.store.conn.close()
    m._instance_lock.release()
    for suffix in ("", "-wal", "-shm"):
        (data / f"library.db{suffix}").unlink(missing_ok=True)
    (data / "library.db").write_bytes(b"garbage" * 500)
    m2 = make_manager(data)
    assert job["id"] in m2.jobs, "the backup is loaded"
    assert list(data.glob("library.damaged-*.db")), "the damaged file is kept aside"


# ---------- deletes only touch files whose last [id] tag is the video

NAMES = {
    "01 - Video v1 [v1].mp4": True,           # the video
    "Video v1 [v1].en.srt": True,             # its subtitles
    "Video v1 [v1].f137.mp4.part": True,      # a partial stream
    "Song [v1] [v9].mp4": False,              # last tag is another video
    "Other [v10].mp4": False,                 # a longer id
    "Other [xv1].mp4": False,
    "notes v1.txt": False,                    # no tag at all
}


def test_delete_file_only_own_tagged_files(mgr, out, tmp_path):
    folder = tmp_path / "del"
    folder.mkdir()
    job = make_job(mgr, out, "delete", ["v1"])
    it = items_of(job)["v1"]
    for name in NAMES:
        (folder / name).write_bytes(b"x")
    with mgr.cond:
        job["outdir"] = str(folder)
        it.update(outdir=str(folder), status="done", filepath=str(folder / "01 - Video v1 [v1].mp4"))
        mgr._touch(job, it)
    mgr._archive_add(str(folder), key(it))
    mgr.item_action(job["id"], "v1", "delete-file")
    left = {p.name for p in folder.iterdir() if p.name in NAMES}
    assert left == {n for n, gone in NAMES.items() if not gone}, left
    assert key(it) not in M.Manager._archive_ids(str(folder)) and it["status"] == "new"


def test_delete_file_refused_while_queued(mgr, out):
    job = make_job(mgr, out, "busy", ["b1"])
    mgr.queue(job["id"], None, dict(OPTS))
    try:
        with pytest.raises(ValueError):
            mgr.item_action(job["id"], "b1", "delete-file")
    finally:
        mgr.action(job["id"], "stop")


def test_remove_with_files_only_own_tagged_files(mgr, out, tmp_path):
    folder = tmp_path / "rmv"
    folder.mkdir()
    job = make_job(mgr, out, "removefiles", ["v1"])
    for name in NAMES:
        (folder / name).write_bytes(b"x")
    with mgr.cond:
        job["outdir"] = str(folder)
        for it in job["items"]:
            it.update(outdir=str(folder), status="done")
        mgr._touch(job)
    mgr.remove(job["id"], delete_files=True)
    assert wait(lambda: not (folder / "01 - Video v1 [v1].mp4").exists(), 10)
    left = {p.name for p in folder.iterdir() if p.name in NAMES}
    assert left == {n for n, gone in NAMES.items() if not gone}, left


def test_cleanup_never_touches_finished_files_or_other_videos(mgr, out, tmp_path):
    folder = tmp_path / "clean"
    folder.mkdir()
    job = make_job(mgr, out, "cleanup", ["v1"])
    for name in NAMES:
        (folder / name).write_bytes(b"x")
    with mgr.cond:
        job["outdir"] = str(folder)
        for it in job["items"]:
            it.update(outdir=str(folder), status="done", filepath=str(folder / "01 - Video v1 [v1].mp4"))
        mgr._touch(job)
    names = set(mgr.cleanup(job["id"], dry_run=True)["names"])
    assert names == {"Video v1 [v1].f137.mp4.part"}, names
    assert all((folder / n).exists() for n in NAMES), "the preview deletes nothing"
    mgr.cleanup(job["id"], dry_run=False)
    assert not (folder / "Video v1 [v1].f137.mp4.part").exists()
    assert all((folder / n).exists() for n in NAMES if n != "Video v1 [v1].f137.mp4.part")
