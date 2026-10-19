"""The running engine: the Manager and the connection monitor, started once the web port is ours."""

from __future__ import annotations

import sys

from ..errors import AlreadyRunning
from ..manager import Manager
from ..netcheck import NetMonitor

runtime = {"can_restart": False,  # True when started as "python server.py" (the process can re-exec itself)
           "server": None}        # the uvicorn.Server, so live streams can end when it shuts down

manager: Manager = None   # type: ignore[assignment]  — created by start_engine()
net: NetMonitor = None    # type: ignore[assignment]


def start_engine(port: int = 8765):
    """Start the download manager. Called only once we know the web port is ours, so a second copy of the
    app never starts downloading before it notices the first one."""
    global manager, net
    try:
        manager = Manager()
    except AlreadyRunning as exc:
        sys.exit(f"{exc}\nOpen http://127.0.0.1:{port} in your browser, or stop the other copy first.")
    net = NetMonitor(lambda: manager.settings, manager.total_speed, on_change=manager.network_changed)
    manager.net_level = lambda: net.state.get("level", "checking")
    manager.on_network_error = net.check_now   # re-test the connection as soon as a download hits trouble
