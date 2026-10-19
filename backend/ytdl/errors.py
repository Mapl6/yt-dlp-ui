"""Error kinds (disk, permanent, ratelimit, cookies, network, other): exception types and HTTP status first,
message text only as a fallback."""

from __future__ import annotations

import errno
import re
import socket

from yt_dlp.cookies import CookieLoadError
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ContentTooShortError, ExtractorError, GeoRestrictedError

from .util import ANSI

# Text fallbacks for error classification (exception types and HTTP status are checked first).
DISK_ERRORS = re.compile(r"no space left|errno 28|disk (?:is )?full|not enough space", re.I)
PERMANENT_ERRORS = re.compile(
    r"private video|video unavailable|has been removed|members.only|not available in your country|"
    r"copyright|account associated with this video|sign in to confirm your age|premieres in|"
    r"this live event will begin|deleted video|is not a valid url|unsupported url|http error 404",
    re.I,
)
RATE_LIMIT_ERRORS = re.compile(r"http error 429|too many requests|confirm you.?re not a bot|rate.?limit", re.I)
COOKIE_ERRORS = re.compile(
    r"could not find [\w -]+ cookies database|cookies database not found|failed to (?:load|decrypt) cookies|"
    r"unsupported browser(?: specified for cookies)?:|cookies?\b.*operation not permitted",
    re.I,
)
NETWORK_ERRORS = re.compile(
    r"timed? ?out|connection (?:reset|refused|aborted|error|broken)|remote end closed|broken pipe|"
    r"network is unreachable|no route to host|temporary failure in name resolution|name or service not known|"
    r"nodename nor servname|getaddrinfo|failed to resolve|unable to connect|proxy|ssl|eof occurred|"
    r"incomplete ?read|content too short|did not get any data|http error 5\d\d|transport|urlopen error|"
    r"unable to download (?:webpage|video data|json|api)|giving up after|errno (?:32|54|60|61|65|104|110|111|113)",
    re.I,
)


def _clean_error(exc: Exception) -> str:
    msg = ANSI.sub("", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)  # drop "[youtube] abc123: " prefix
    return msg[:400]


def _exc_chain(exc: BaseException) -> list[BaseException]:
    """The exception plus everything it wraps (yt-dlp keeps the original in exc_info / cause)."""
    seen: dict[int, BaseException] = {}
    stack = [exc]
    while stack:
        e = stack.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen[id(e)] = e
        info = getattr(e, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1:
            stack.append(info[1])
        stack += [getattr(e, "cause", None), e.__cause__, e.__context__]
    return list(seen.values())


def _error_kind(exc: BaseException | str) -> str:
    """disk | cookies | permanent | ratelimit | network | other"""
    chain = [] if isinstance(exc, str) else _exc_chain(exc)
    if any(isinstance(e, CookieLoadError) for e in chain):
        return "cookies"                             # the browser's cookies couldn't be read: no retry fixes it
    for e in chain:
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            return "disk"
        if isinstance(e, HTTPError):
            if e.status == 429:
                return "ratelimit"
            if e.status in (404, 410):
                return "permanent"
            if e.status >= 500 or e.status in (403, 408):   # 403: expired stream URL, fixed by re-extracting
                return "network"
        if isinstance(e, GeoRestrictedError):
            return "permanent"
    msg = exc if isinstance(exc, str) else " | ".join(_clean_error(e) for e in chain)
    for kind, rx in (("disk", DISK_ERRORS), ("cookies", COOKIE_ERRORS), ("ratelimit", RATE_LIMIT_ERRORS),
                     ("permanent", PERMANENT_ERRORS)):
        if rx.search(msg):
            return kind
    if any(isinstance(e, (TransportError, ContentTooShortError, socket.timeout, TimeoutError, ConnectionError))
           for e in chain) or NETWORK_ERRORS.search(msg):
        return "network"
    if any(isinstance(e, ExtractorError) and e.expected for e in chain):
        return "permanent"                           # yt-dlp marks errors a retry can't fix as "expected"
    return "other"


class _IncompleteFile(Exception):
    """The download finished but the file on disk failed verification."""


class AlreadyRunning(RuntimeError):
    pass
