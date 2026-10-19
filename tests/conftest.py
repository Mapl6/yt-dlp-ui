"""Shared fixtures. Nothing here needs the internet; media comes from ffmpeg and local HTTP servers.

Isolation comes first: manager.py reads YTDL_DATA_DIR when it is imported, so it's pointed at a scratch
directory before anything imports it, and the real library (<repo>/data) is refused outright.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="ytdl-pytest-"))
os.environ["YTDL_DATA_DIR"] = str(SCRATCH / "data")
(SCRATCH / "data").mkdir()          # app.log lives here for the whole run (one log handler per process)
sys.path.insert(0, str(ROOT / "backend"))

from ytdl import config  # noqa: E402

from helpers import (FakeExtractor, Flaky, MediaServer, Net, ff, new_manager, restore_paths,  # noqa: E402
                     stop_manager, use_data_dir)

if config.DATA_DIR.resolve() == (ROOT / "data").resolve():
    raise SystemExit("Refusing to run: the tests would use the real library in data/")


# ---------- media and the local media server

@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """30 s test clips: full.mp4 (h264 + aac), its video and audio streams apart, and a thumbnail."""
    d = tmp_path_factory.mktemp("media")
    ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "30",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart", str(d / "full.mp4"))
    ff("-i", str(d / "full.mp4"), "-an", "-c", "copy", str(d / "v.mp4"))
    ff("-i", str(d / "full.mp4"), "-vn", "-c", "copy", str(d / "a.m4a"))
    ff("-f", "lavfi", "-i", "testsrc=size=320x180", "-frames:v", "1", str(d / "thumb.png"))
    return d


@pytest.fixture(scope="session")
def good(media):
    """The bytes of a healthy 30 s video."""
    return (media / "full.mp4").read_bytes()


@pytest.fixture(scope="session")
def media_server(media):
    s = MediaServer(media)
    yield s
    s.srv.shutdown()


@pytest.fixture(scope="module")
def flaky_server(good):
    s = Flaky(good)
    yield s
    s.srv.shutdown()


# ---------- a fake YouTube: test://<id> videos with separate video and audio streams

@pytest.fixture(scope="module")
def fake(media_server, media):
    fx = FakeExtractor(media_server, media)
    restore = fx.install()
    yield fx
    restore()


# ---------- the manager

@pytest.fixture(scope="module")
def tmp(tmp_path_factory, request):
    return tmp_path_factory.mktemp(request.module.__name__)


@pytest.fixture(scope="module")
def out(tmp):
    d = tmp / "out"
    d.mkdir()
    return d


@pytest.fixture(scope="module")
def net():
    return Net("offline")


@pytest.fixture(scope="module")
def mgr_settings():
    """Override in a module for other starting settings."""
    return {}


@pytest.fixture(scope="module")
def mgr(tmp, out, net, mgr_settings):
    """One Manager per test module, on its own scratch library. Workers stay idle until a test sets
    `net.level = "good"`."""
    before = use_data_dir(tmp / "data")
    m = new_manager(net)
    m.update_settings({"output_dir": str(out), **mgr_settings})
    yield m
    stop_manager(m)
    restore_paths(before)


@pytest.fixture
def make_manager():
    """Start another Manager on another library (or the same one after a restart). Paths are restored after."""
    made, before = [], {}

    def make(data_dir, level="offline"):
        prev = use_data_dir(data_dir)
        before.setdefault("paths", prev)
        m = new_manager(lambda: level)
        made.append(m)
        return m

    yield make
    for m in made:
        stop_manager(m)
    if before:
        restore_paths(before["paths"])
