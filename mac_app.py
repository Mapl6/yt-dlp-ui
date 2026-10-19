"""YT Downloader as a Mac app: its own window plus a menu-bar icon. Run: ./run-mac.sh

Optional: needs pywebview (requirements-mac.txt). The browser version (./run.sh) works without it.
Closing the window only hides it, so downloads keep going; quit from the menu-bar icon or with ⌘Q.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
from ytdl import server  # noqa: E402

try:
    import webview
except ImportError:                                   # pragma: no cover - explained to the user
    sys.exit("The Mac app needs pywebview: run ./run-mac.sh (or pip install -r requirements-mac.txt).")

state = {"quitting": False, "hide_on_close": False, "window": None, "status": None}


def notify(title: str, body: str):
    """A macOS notification (Notification Center) — the page calls this through window.pywebview.api."""
    script = f"display notification {json.dumps(str(body)[:300])} with title {json.dumps(str(title)[:120])}"
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Api:
    """Functions the page can call (window.pywebview.api.*)."""

    def notify(self, title, body):
        notify(title, body)
        return True


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_until_up(port: int, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


# ---------------------------------------------------------------- macOS integration (PyObjC, comes with pywebview)

def patch_app_delegate():
    """⌘Q / Quit always quits (pywebview would ask the window, which only hides), and clicking the Dock
    icon brings the hidden window back. If this can't be done, closing the window simply quits."""
    try:
        import Foundation
        import objc
        from webview.platforms import cocoa

        def applicationShouldTerminate_(self, app):
            state["quitting"] = True
            return 1                                    # NSTerminateNow

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
            show_window()
            return Foundation.YES

        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [
            objc.selector(applicationShouldTerminate_, selector=b"applicationShouldTerminate:", signature=b"I@:@"),
            objc.selector(applicationShouldHandleReopen_hasVisibleWindows_,
                          selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z"),
        ])
        state["hide_on_close"] = True
    except Exception as exc:                            # keep the app usable without the extras
        print(f"Menu-bar extras unavailable: {exc}", file=sys.stderr)


def show_window():
    win = state["window"]
    if win:
        win.show()
        try:
            import AppKit
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass


def quit_app():
    state["quitting"] = True
    if state["window"]:
        state["window"].destroy()


def make_status_item():
    """The menu-bar icon: ↓N while downloading, with Show / Pause all / Resume all / Open folder / Quit."""
    import AppKit
    from Foundation import NSObject

    class Target(NSObject):
        def show_(self, sender):
            show_window()

        def pauseAll_(self, sender):
            server.manager.pause_all(True)

        def resumeAll_(self, sender):
            server.manager.pause_all(False)

        def openFolder_(self, sender):
            from ytdl.manager import _folder_problem, _open_folder
            import os
            folder = os.path.expanduser(server.manager.settings["output_dir"])
            if not _folder_problem(folder):
                _open_folder(folder)

        def quit_(self, sender):
            quit_app()

    target = Target.alloc().init()
    item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    item.button().setTitle_("⇣")
    item.button().setToolTip_("YT Downloader")
    menu = AppKit.NSMenu.alloc().init()
    for title, action in (("Show YT Downloader", "show:"), (None, None), ("Pause all", "pauseAll:"),
                          ("Resume all", "resumeAll:"), ("Open download folder", "openFolder:"), (None, None),
                          ("Quit YT Downloader", "quit:")):
        if title is None:
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        entry.setTarget_(target)
        menu.addItem_(entry)
    item.setMenu_(menu)
    state["status"] = (item, target, menu)               # keep references alive


def update_status_forever():
    from PyObjCTools import AppHelper
    while not state["quitting"]:
        try:
            ov = server.manager.overview()
            title = f"⇣{ov['active']}" if ov["active"] else "⇣"
            item = state["status"][0] if state["status"] else None
            if item:
                AppHelper.callAfter(item.button().setTitle_, title)
        except Exception:
            pass
        time.sleep(2)


def on_closing():
    """Close button: hide instead of quitting, so downloads continue (unless we are really quitting)."""
    if state["hide_on_close"] and not state["quitting"]:
        state["window"].hide()
        return False
    return True


def on_started(smoke: bool):
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(make_status_item)
        threading.Thread(target=update_status_forever, daemon=True).start()
    except Exception as exc:
        print(f"Menu-bar icon unavailable: {exc}", file=sys.stderr)
    if smoke:
        time.sleep(4)
        title = state["window"].evaluate_js("document.title")
        rows = state["window"].evaluate_js("document.querySelectorAll('.dash-stats b').length")
        print(f"SMOKE page title={title!r} dashboard tiles={rows} status item={'yes' if state['status'] else 'no'}")
        quit_app()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--smoke", action="store_true", help="start, check the page loads, quit (for tests)")
    args = parser.parse_args()
    if not port_free(args.port):
        sys.exit(f"Port {args.port} is already in use — is YT Downloader already running? "
                 f"Open http://127.0.0.1:{args.port} or quit the other copy first.")
    server.start_engine(args.port)
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=args.port, log_level="warning",
                                        timeout_graceful_shutdown=2))
    server.runtime["server"] = srv
    threading.Thread(target=srv.run, name="web", daemon=True).start()
    if not wait_until_up(args.port):
        sys.exit("The downloader didn't start.")

    patch_app_delegate()
    window = webview.create_window("YT Downloader", f"http://127.0.0.1:{args.port}/", js_api=Api(),
                                   width=1180, height=880, min_size=(420, 520))
    state["window"] = window
    window.events.closing += on_closing
    # Not private: pywebview's default forgets the page's storage (remembered tabs, filters) on every start.
    from ytdl.manager import DATA_DIR
    webview.start(on_started, (args.smoke,), private_mode=False, storage_path=str(DATA_DIR / "webview"))

    # the window is gone: stop the web server and save the library
    state["quitting"] = True
    srv.should_exit = True
    server.manager.save_state()
    time.sleep(0.5)


if __name__ == "__main__":
    main()
