"""Item status changes (new → queued → downloading → processing → done | failed, cancel, hold) and every
re-queue path: each one goes through _requeue, so a missing or damaged file is dropped from the archive and all
three retry counters start again."""
import pytest

import manager as M
from helpers import items_of, make_job, wait

OPTS = {"quality": "best", "container": "mp4", "thumbnail": False, "metadata": False, "numbering": False}
key = M.Manager._archive_key


@pytest.fixture
def statuses(mgr, monkeypatch):
    """Records the status of watched videos after every _touch (the only way a change is published)."""
    seen: dict[str, list[str]] = {}
    real = mgr._touch

    def touch(job, item=None):
        real(job, item)
        for it in job["items"]:
            if it["id"] in seen and (not seen[it["id"]] or seen[it["id"]][-1] != it["status"]):
                seen[it["id"]].append(it["status"])

    monkeypatch.setattr(mgr, "_touch", touch)
    return seen


@pytest.mark.slow
def test_download_status_flow(mgr, net, out, fake, statuses):
    job = make_job(mgr, out, "flow", ["f1"])
    it = items_of(job)["f1"]
    assert it["status"] == "new"
    statuses["f1"] = []
    mgr.queue(job["id"], None, dict(OPTS, output_dir=str(out)))
    net.level = "good"
    try:
        assert wait(lambda: it["status"] in ("done", "failed"), 90), it["status"]
    finally:
        net.level = "offline"
    seq = statuses["f1"]
    assert it["status"] == "done", (it["status"], it["error"])
    assert seq[0] == "queued" and seq[-1] == "done" and seq.count("done") == 1, seq
    assert set(seq) <= {"queued", "downloading", "processing", "done"}, seq
    assert seq.index("downloading") < seq.index("processing"), seq
    assert it["filepath"] and key(it) in M.Manager._archive_ids(str(out))


@pytest.mark.slow
def test_permanent_error_fails_at_once(mgr, net, out, fake, statuses):
    job = make_job(mgr, out, "perm", ["gone1"])
    it = items_of(job)["gone1"]
    fake.fail_ids.add("gone1")
    statuses["gone1"] = []
    mgr.queue(job["id"], None, dict(OPTS, output_dir=str(out)))
    net.level = "good"
    try:
        assert wait(lambda: it["status"] == "failed", 60), it["status"]
    finally:
        net.level = "offline"
        fake.fail_ids.discard("gone1")
    assert statuses["gone1"] == ["queued", "downloading", "failed"], statuses["gone1"]
    assert it["attempts"] <= 1 and "Private video" in it["error"]
    assert (job["id"], "gone1") not in mgr._live, "a failed video leaves the scheduler's index"


@pytest.mark.slow
def test_cancel_running_download(mgr, net, out, fake, media_server):
    job = make_job(mgr, out, "cancel", ["c1"])
    it = items_of(job)["c1"]
    media_server.throttle = True
    mgr.queue(job["id"], None, dict(OPTS, output_dir=str(out)))
    net.level = "good"
    try:
        assert wait(lambda: it["status"] == "downloading" and (it.get("downloaded") or 0) > 0, 60), it["status"]
        mgr.item_action(job["id"], "c1", "cancel")
        assert wait(lambda: it["status"] == "new", 30), f"a cancelled download goes back to new: {it['status']}"
    finally:
        media_server.throttle = False
        net.level = "offline"
    assert key(it) not in M.Manager._archive_ids(str(out)) and (job["id"], "c1") not in mgr._live


def test_cancel_and_hold_queued(mgr, out):
    job = make_job(mgr, out, "queued", ["q1", "q2"])
    its = items_of(job)
    mgr.queue(job["id"], None, dict(OPTS))
    try:
        assert its["q1"]["status"] == "queued" and (job["id"], "q1") in mgr._live
        mgr.item_action(job["id"], "q1", "hold")
        assert its["q1"]["held"] and its["q1"]["status"] == "queued", "a held video stays queued"
        mgr.item_action(job["id"], "q1", "cancel")
        assert its["q1"]["status"] == "new" and not its["q1"]["held"] and (job["id"], "q1") not in mgr._live
        with pytest.raises(ValueError):
            mgr.item_action(job["id"], "q1", "hold")     # only queued videos can be paused
        with pytest.raises(ValueError):
            mgr.item_action(job["id"], "q2", "mark-done")
    finally:
        mgr.action(job["id"], "stop")
    assert its["q2"]["status"] == "new", "stop puts queued videos back to new"


def test_queue_skips_done_and_unavailable(mgr, out):
    job = make_job(mgr, out, "skips", ["d1", "s1", "r1", "n1"])
    its = items_of(job)
    with mgr.cond:
        its["d1"]["status"] = "done"
        its["s1"]["status"] = "skipped"
        its["r1"]["removed"] = True
        mgr._touch(job)
    try:
        assert mgr.queue(job["id"], None, dict(OPTS)) == 1 and its["n1"]["status"] == "queued"
        assert its["d1"]["status"] == "done" and its["s1"]["status"] == "skipped" and its["r1"]["status"] == "new"
        assert mgr.queue(job["id"], ["r1"], dict(OPTS)) == 1, "an unavailable video can still be asked for by id"
    finally:
        mgr.action(job["id"], "stop")


# ---------- every re-queue path

def requeue_by_queue(mgr, job, vid):
    mgr.queue(job["id"], [vid], dict(OPTS))


def requeue_by_playlist_retry(mgr, job, vid):
    mgr.action(job["id"], "retry")


def requeue_by_video_retry(mgr, job, vid):
    mgr.item_action(job["id"], vid, "retry")


def requeue_by_retry_all_failed(mgr, job, vid):
    mgr.retry_failed()


PATHS = [requeue_by_queue, requeue_by_playlist_retry, requeue_by_video_retry, requeue_by_retry_all_failed]


@pytest.mark.parametrize("problem", ["missing", "damaged"])
@pytest.mark.parametrize("path", PATHS, ids=[p.__name__ for p in PATHS])
def test_requeue_forgets_the_file(mgr, out, path, problem):
    name = f"{path.__name__}-{problem}"
    job = make_job(mgr, out, name, ["v"])
    it = items_of(job)["v"]
    with mgr.cond:
        it.update(status="failed", file_problem=problem, filepath=str(out / "gone [v].mp4"), health=problem,
                  error="file gone", attempts=3, net_attempts=7, rate_attempts=4, rate_first=123.0, held=True)
        mgr._touch(job, it)
    mgr._archive_add(str(out), key(it))
    try:
        path(mgr, job, "v")
        assert it["status"] == "queued", it["status"]
        assert key(it) not in M.Manager._archive_ids(str(out)), "the archive line is dropped, or it'd be skipped again"
        assert it["file_problem"] is None and it["filepath"] is None and it["health"] is None
        assert (it["attempts"], it["net_attempts"], it["rate_attempts"], it["rate_first"]) == (0, 0, 0, None)
        assert it["error"] is None and not it["held"] and it["next_try"] == 0
        assert (job["id"], "v") in mgr._live, "the scheduler sees it"
    finally:
        mgr.action(job["id"], "stop")
        mgr.remove(job["id"])


def test_requeue_without_file_problem_keeps_archive(mgr, out):
    """A plain failure (no file problem) has no archive line to drop, and other videos' lines stay."""
    job = make_job(mgr, out, "plain", ["p1", "p2"])
    its = items_of(job)
    with mgr.cond:
        its["p1"].update(status="failed", error="boom", attempts=2)
        mgr._touch(job)
    mgr._archive_add(str(out), key(its["p2"]))
    try:
        mgr.item_action(job["id"], "p1", "retry")
        assert its["p1"]["status"] == "queued" and its["p1"]["attempts"] == 0
        assert key(its["p2"]) in M.Manager._archive_ids(str(out))
    finally:
        mgr.action(job["id"], "stop")
        mgr._archive_remove(str(out), key(its["p2"]))


def test_retry_only_failed_or_queued(mgr, out):
    job = make_job(mgr, out, "retryonly", ["n1"])
    it = items_of(job)["n1"]
    with pytest.raises(ValueError):
        mgr.item_action(job["id"], "n1", "retry")    # refused (as "Unknown action retry")
    assert it["status"] == "new"
