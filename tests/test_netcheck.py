"""The connection monitor, against local servers instead of Google and YouTube."""
import http.server
import socket

import pytest

from ytdl import netcheck
from helpers import serve, wait


class NoContent(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(204)
        self.end_headers()


@pytest.fixture(scope="module")
def up():
    srv = serve(NoContent)
    yield f"http://127.0.0.1:{srv.server_address[1]}/generate_204"
    srv.shutdown()


@pytest.fixture
def down():
    """A port nobody listens on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/generate_204"


def monitor(monkeypatch, internet, youtube, settings=None):
    monkeypatch.setattr(netcheck, "TARGETS", {"internet": internet, "youtube": youtube})
    monkeypatch.setattr(netcheck, "PINGS", 2)
    monkeypatch.setattr(netcheck, "TIMEOUT", 2)
    changes = []
    m = netcheck.NetMonitor(lambda: settings or {}, lambda: 1234.0, on_change=lambda old, new: changes.append((old, new)))
    return m, changes


def test_online(monkeypatch, up):
    m, changes = monitor(monkeypatch, up, up)
    assert wait(lambda: m.state.get("checked"), 20), m.state
    assert m.state["level"] in ("excellent", "good") and m.state["bars"] >= 3, m.state
    assert changes and changes[0][0] == "checking" and changes[0][1] == m.state["level"]
    snap = m.snapshot()
    assert snap["speed"] == 1234.0 and snap["peak_speed"] == 1234.0 and len(snap["history"]) >= 1
    assert snap["youtube"]["ok"] and snap["youtube"]["loss"] == 0


def test_youtube_blocked(monkeypatch, up, down):
    m, _ = monitor(monkeypatch, up, down)
    assert wait(lambda: m.state.get("checked"), 20)
    assert m.state["level"] == "blocked" and "VPN" in m.state["advice"], m.state
    # Windows retries a refused local connection until the timeout, so it may report that instead
    assert m.state["youtube"]["error"] in ("Connection refused", "Timed out"), m.state["youtube"]


def test_offline(monkeypatch, down):
    m, _ = monitor(monkeypatch, down, down)
    assert wait(lambda: m.state.get("checked"), 20)
    assert m.state["level"] == "offline" and m.state["label"] == "Offline", m.state


def test_offline_through_proxy(monkeypatch, up, down):
    proxy = down.rsplit("/", 1)[0]
    m, _ = monitor(monkeypatch, up, up, {"proxy": proxy})
    assert wait(lambda: m.state.get("checked"), 30)
    assert m.state["level"] == "offline" and "proxy" in m.state["advice"] and m.state["proxy"] == proxy, m.state


def test_check_now_reacts(monkeypatch, up):
    m, _ = monitor(monkeypatch, up, up)
    assert wait(lambda: m.state.get("checked"), 20)
    first = m.state["checked"]
    m.check_now()
    assert wait(lambda: m.state["checked"] != first, 10), "check_now() runs a check at once, not in 10 s"


def classify(net, yt, proxy=""):
    m = object.__new__(netcheck.NetMonitor)
    m._proxy = proxy
    return m._classify(net, yt)[0]


def probe(ok=True, latency=50, loss=0, jitter=0):
    return {"ok": ok, "latency": latency if ok else None, "loss": loss, "jitter": jitter}


@pytest.mark.parametrize("yt,want", [
    (probe(latency=50), "excellent"),
    (probe(latency=400), "good"),
    (probe(latency=800), "fair"),
    (probe(latency=50, loss=33), "fair"),
    (probe(latency=50, jitter=700), "fair"),
    (probe(latency=1600), "weak"),
    (probe(latency=50, loss=34), "weak"),
    (probe(ok=False), "blocked"),
])
def test_classify(yt, want):
    assert classify(probe(), yt) == want


def test_classify_offline():
    assert classify(probe(ok=False), probe(ok=False)) == "offline"
    assert classify(probe(ok=False), probe(ok=False), proxy="http://p") == "offline"


@pytest.mark.parametrize("msg,want", [
    ("<urlopen error timed out>", "Timed out"), ("[Errno 61] Connection refused", "Connection refused"),
    ("nodename nor servname provided", "DNS lookup failed"), ("ProxyError: bad gateway", "Proxy error"),
    ("SSL: CERTIFICATE_VERIFY_FAILED", "Secure connection failed"), ("Connection reset by peer", "Connection reset"),
    ("something new\nsecond line", "something new"),
])
def test_friendly_errors(msg, want):
    assert netcheck._friendly(Exception(msg)) == want
