"""Cookies: the browsers and profiles on this computer, reading them, and an imported cookies.txt."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from yt_dlp import cookies as ytdlp_cookies

from . import config
from .util import ANSI

# ---------------------------------------------------------------- browser cookies

# Chromium browsers yt-dlp doesn't know by name: (folder under Application Support, keychain name).
EXTRA_BROWSERS = {"arc": ("Arc/User Data", "Arc"), "chrome-canary": ("Google/Chrome Canary", "Chrome")} \
    if sys.platform == "darwin" else {}
BROWSER_NAMES = {"chrome": "Google Chrome", "chrome-canary": "Chrome Canary", "arc": "Arc", "brave": "Brave",
                 "edge": "Microsoft Edge", "chromium": "Chromium", "opera": "Opera", "vivaldi": "Vivaldi",
                 "whale": "Naver Whale", "firefox": "Firefox", "safari": "Safari"}
LOGIN_COOKIES = {"SAPISID", "__Secure-3PAPISID", "__Secure-1PSID", "LOGIN_INFO"}
FULL_DISK_HINT = ("macOS blocks access to it: open System Settings → Privacy & Security → Full Disk Access and "
                  "turn it on for the app that runs YT Downloader (Terminal, or YT Downloader), then try again")


def _register_extra_browsers():
    """Teach yt-dlp the Chromium browsers in EXTRA_BROWSERS (it reads cookies from them like from Chrome)."""
    orig = getattr(ytdlp_cookies, "_get_chromium_based_browser_settings", None)
    if not EXTRA_BROWSERS or orig is None or getattr(orig, "_extra", False):
        return

    def settings(name):
        if name in EXTRA_BROWSERS:
            folder, keyring = EXTRA_BROWSERS[name]
            return {"browser_dir": os.path.expanduser(f"~/Library/Application Support/{folder}"),
                    "keyring_name": keyring, "supports_profiles": True}
        return orig(name)

    settings._extra = True
    ytdlp_cookies._get_chromium_based_browser_settings = settings
    ytdlp_cookies.SUPPORTED_BROWSERS.update(EXTRA_BROWSERS)
    ytdlp_cookies.CHROMIUM_BASED_BROWSERS.update(EXTRA_BROWSERS)


_register_extra_browsers()


def _browser_dir(browser: str) -> str | None:
    try:
        return ytdlp_cookies._get_chromium_based_browser_settings(browser)["browser_dir"]
    except Exception:
        return None


def _listdir(path: str) -> list[str] | None:
    """Folder entries; None when the folder is missing; raises PermissionError when macOS blocks it."""
    try:
        return sorted(os.listdir(path))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _chromium_profiles(root: str) -> list[dict]:
    names = {}
    try:                                           # the names people gave their profiles ("Work", "Mahdi")
        cache = json.loads((Path(root) / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"]
        names = {k: (v.get("name") or k) for k, v in cache.items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    out = []
    for entry in _listdir(root) or []:
        base = Path(root) / entry
        if (base / "Cookies").is_file() or (base / "Network" / "Cookies").is_file():
            out.append({"id": entry, "name": names.get(entry, entry)})
    if not out and ((Path(root) / "Cookies").is_file() or (Path(root) / "Network" / "Cookies").is_file()):
        out.append({"id": "", "name": "Default"})   # browsers without profiles (Opera)
    return out


def cookie_sources() -> list[dict]:
    """Browsers found on this computer that cookies can be read from, with their profiles.
    status: ok | no-profiles (installed, but no profile with cookies) | blocked (macOS needs Full Disk Access)."""
    found = []

    def add(browser, profiles=None, status="ok", note=""):
        found.append({"browser": browser, "name": BROWSER_NAMES.get(browser, browser.title()),
                      "profiles": profiles or [], "status": status, "note": note})

    chromium = sorted(ytdlp_cookies.CHROMIUM_BASED_BROWSERS, key=lambda b: list(BROWSER_NAMES).index(b)
                      if b in BROWSER_NAMES else 99)
    for browser in chromium:
        root = _browser_dir(browser)
        if not root:
            continue
        try:
            if _listdir(root) is None:
                continue
            profiles = _chromium_profiles(root)
        except PermissionError:
            add(browser, status="blocked", note=FULL_DISK_HINT)
            continue
        add(browser, profiles, "ok" if profiles else "no-profiles",
            "" if profiles else f"No profile with cookies in “{root}” — sign in to YouTube in this browser first, "
                                "or the browser keeps its data somewhere else")
    try:
        roots = list(ytdlp_cookies._firefox_browser_dirs())
    except Exception:
        roots = []
    ff, ff_blocked = [], False
    for root in roots:
        try:
            for entry in _listdir(root) or []:
                if (Path(root) / entry / "cookies.sqlite").is_file():
                    ff.append({"id": str(Path(root) / entry), "name": entry.split(".", 1)[-1]})
        except PermissionError:
            ff_blocked = True
    if ff:
        add("firefox", ff)
    elif ff_blocked:
        add("firefox", status="blocked", note=FULL_DISK_HINT)
    if sys.platform == "darwin":
        for path in ("~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
                     "~/Library/Cookies/Cookies.binarycookies"):
            path = os.path.expanduser(path)
            try:
                with open(path, "rb") as f:
                    f.read(4)
                add("safari")
                break
            except FileNotFoundError:
                continue
            except PermissionError:
                add("safari", status="blocked", note=FULL_DISK_HINT)
                break
    return found


class _CookieLog:
    def __init__(self):
        self.warnings: list[str] = []

    def debug(self, msg): pass
    def info(self, msg): pass

    def warning(self, msg, only_once=False):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.warnings.append(str(msg))


def _cookie_error_text(browser: str, exc: BaseException) -> str:
    msg = ANSI.sub("", str(exc)).strip()
    if isinstance(exc, PermissionError) or "not permitted" in msg.lower():
        return f"Couldn't read the cookies of {BROWSER_NAMES.get(browser, browser)}: {FULL_DISK_HINT}"
    if "could not find" in msg.lower():
        root = _browser_dir(browser)
        if root:
            try:
                os.listdir(root)
            except PermissionError:
                return f"Couldn't read the cookies of {BROWSER_NAMES.get(browser, browser)}: {FULL_DISK_HINT}"
            except OSError:
                pass
        return (f"{BROWSER_NAMES.get(browser, browser)} has no cookies to read ({msg}). Choose the browser and "
                "profile you're signed in to YouTube with, or import a cookies.txt file")
    return (f"Couldn't read the cookies of {BROWSER_NAMES.get(browser, browser)}: {msg} — choose another browser "
            "or profile in Settings, or import a cookies.txt file")


def check_cookies(browser: str, profile: str = "") -> dict:
    """Read a browser's cookies the way a download would, and say whether YouTube will see a signed-in user."""
    if browser not in ytdlp_cookies.SUPPORTED_BROWSERS:
        return {"ok": False, "error": f"Unknown browser “{browser}”"}
    logger = _CookieLog()
    try:
        jar = ytdlp_cookies.extract_cookies_from_browser(browser, profile or None, logger)
    except Exception as exc:
        return {"ok": False, "error": _cookie_error_text(browser, exc)}
    yt = [c for c in jar if c.domain.lstrip(".").endswith(("youtube.com", "google.com"))]
    signed_in = any(c.name in LOGIN_COOKIES for c in yt)
    failed = [w for w in logger.warnings if "decrypt" in w.lower()]
    out = {"ok": bool(yt), "total": len(jar), "youtube": len(yt), "signed_in": signed_in}
    if not yt:
        out["error"] = ("Couldn't decrypt the cookies — allow access when macOS asks for the keychain password"
                        if failed else "No YouTube cookies in this profile — open YouTube in this browser and sign in")
    elif not signed_in:
        out["warning"] = "Found YouTube cookies but no signed-in account; signing in helps against the bot check"
    return out


# ---------------------------------------------------------------- imported cookies.txt

MAX_COOKIE_FILE = 2 << 20


def _cookie_file() -> Path:
    return config.DATA_DIR / "cookies.txt"


def _parse_cookie_text(text: str) -> list[tuple[str, str]]:
    """(domain, name) of each cookie in a Netscape cookies.txt; ValueError if it isn't one."""
    cookies = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 7 or parts[1].upper() not in ("TRUE", "FALSE") or parts[3].upper() not in ("TRUE", "FALSE"):
            raise ValueError(f"Line {n} isn't in the cookies.txt (Netscape) format. Export the cookies with a "
                             "browser extension such as “Get cookies.txt LOCALLY”.")
        cookies.append((parts[0].lstrip("."), parts[5]))
    return cookies


def _cookie_summary(cookies: list[tuple[str, str]]) -> dict:
    yt = [(d, n) for d, n in cookies if d.endswith(("youtube.com", "google.com"))]
    return {"count": len(cookies), "youtube": len(yt), "signed_in": any(n in LOGIN_COOKIES for _, n in yt)}


_cookie_status_cache: dict = {}


def cookie_file_status() -> dict | None:
    """The imported cookies.txt: when, how many cookies, whether YouTube sees a signed-in user (cached by mtime)."""
    path = _cookie_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cookie_status_cache.get("key") != (str(path), mtime):
        try:
            summary = _cookie_summary(_parse_cookie_text(path.read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError):
            summary = {"count": 0, "youtube": 0, "signed_in": False}
        _cookie_status_cache.update(key=(str(path), mtime), value=summary | {"imported": mtime})
    return dict(_cookie_status_cache["value"])
