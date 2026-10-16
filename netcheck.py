"""Connection monitor: is the internet up, is YouTube reachable (through the proxy, if set), and how fast."""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque

import yt_dlp
from yt_dlp.networking import Request

INTERVAL = 10            # seconds between automatic checks
PINGS = 3                # requests per target per check
TIMEOUT = 8

TARGETS = {
    "internet": "https://www.google.com/generate_204",
    "youtube": "https://www.youtube.com/generate_204",
}


def _friendly(exc: Exception) -> str:
    msg = str(exc).lower()
    for needle, text in (("timed out", "Timed out"), ("refused", "Connection refused"),
                         ("name or service", "DNS lookup failed"), ("nodename", "DNS lookup failed"),
                         ("getaddrinfo", "DNS lookup failed"), ("proxy", "Proxy error"),
                         ("ssl", "Secure connection failed"), ("reset", "Connection reset")):
        if needle in msg:
            return text
    return str(exc).split("\n")[0][:120]


class NetMonitor:
    def __init__(self, get_settings, get_speed, on_change=None):
        self._get_settings = get_settings
        self._get_speed = get_speed
        self._on_change = on_change or (lambda old, new: None)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._ydl = None
        self._proxy = None
        self.history: deque = deque(maxlen=40)   # (time, youtube latency ms or None)
        self.state: dict = {"level": "checking", "label": "Checking…", "checked": None}
        self.peak_speed = 0.0
        threading.Thread(target=self._loop, name="net-monitor", daemon=True).start()

    def check_now(self):
        self._wake.set()

    def snapshot(self) -> dict:
        speed = self._get_speed()
        self.peak_speed = max(self.peak_speed, speed)
        with self._lock:
            return self.state | {"speed": speed, "peak_speed": self.peak_speed,
                                 "history": [ms for _, ms in self.history]}

    # ---- internals

    def _client(self):
        proxy = (self._get_settings().get("proxy") or "").strip()
        if self._ydl is None or proxy != self._proxy:
            if self._ydl:
                self._ydl.close()
            opts = {"quiet": True, "no_warnings": True, "socket_timeout": TIMEOUT}
            if proxy:
                opts["proxy"] = proxy
            self._ydl, self._proxy = yt_dlp.YoutubeDL(opts), proxy
        return self._ydl

    def _probe(self, url: str) -> dict:
        times, errors = [], []
        ydl = self._client()
        for _ in range(PINGS):
            start = time.perf_counter()
            try:
                with ydl.urlopen(Request(url, headers={"Cache-Control": "no-cache"})) as res:
                    res.read()
                times.append((time.perf_counter() - start) * 1000)
            except Exception as exc:
                errors.append(_friendly(exc))
        return {
            "ok": bool(times),
            "latency": round(statistics.median(times)) if times else None,
            # the first request includes connection setup, so leave it out of the stability figure
            "jitter": round(max(times[1:]) - min(times[1:])) if len(times) > 2 else 0,
            "loss": round(100 * len(errors) / PINGS),
            "error": errors[-1] if errors and not times else None,
        }

    def _classify(self, net: dict, yt: dict) -> tuple[str, str, str, int]:
        """-> (level, label, advice, bars 0-4)"""
        proxy = bool(self._proxy)
        if not yt["ok"] and not net["ok"]:
            if proxy:
                return "offline", "No connection", "Can't reach anything through the proxy — is your VPN/proxy app running?", 0
            return "offline", "Offline", "No internet connection. Check Wi-Fi or your network cable.", 0
        if not yt["ok"]:
            return ("blocked", "YouTube unreachable",
                    "Internet works but YouTube doesn't. Turn on your VPN, or set a proxy in Settings.", 0)
        ms, loss, jitter = yt["latency"], yt["loss"], yt["jitter"]
        if loss >= 34 or ms > 1500:
            return "weak", "Weak", "Very slow or unstable — downloads may stall and retry. Try fewer parallel downloads.", 1
        if loss > 0 or ms > 700 or jitter > 600:
            return "fair", "Fair", "Usable, but slow responses. Large playlists will still work, just slower.", 2
        if ms > 300:
            return "good", "Good", "Connection to YouTube is working well.", 3
        return "excellent", "Excellent", "Fast, stable connection to YouTube.", 4

    def _loop(self):
        while True:
            try:
                net = self._probe(TARGETS["internet"])
                yt = self._probe(TARGETS["youtube"])
                level, label, advice, bars = self._classify(net, yt)
                old = self.state.get("level")
                with self._lock:
                    self.history.append((time.time(), yt["latency"]))
                    self.state = {
                        "level": level, "label": label, "advice": advice, "bars": bars,
                        "internet": net, "youtube": yt, "proxy": self._proxy or "",
                        "checked": time.time(),
                    }
                if level != old:
                    self._on_change(old, level)
            except Exception as exc:  # never let the monitor die
                with self._lock:
                    self.state = {"level": "checking", "label": "Check failed", "advice": str(exc)[:200],
                                  "bars": 0, "checked": time.time()}
            self._wake.wait(INTERVAL)
            self._wake.clear()
