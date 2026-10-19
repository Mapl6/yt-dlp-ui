"""Protection for the local API: the Host check (DNS rebinding), the per-process token, and the optional password
for network mode (login page, sessions, rate limit)."""

from __future__ import annotations

import html
import secrets
import time
import urllib.parse
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

router = APIRouter()

# Protection for the local API:
#  * Host check: a web page can't reach us through DNS rebinding (its requests carry its own host name).
#  * Session token: every /api call needs the token embedded in our page, which other sites can't read,
#    and the custom header forces a CORS preflight that other origins fail.
TOKEN = secrets.token_urlsafe(24)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
security = {"any_host": False,   # set when the user deliberately serves on the network (--host 0.0.0.0)
            "password": "",      # --password / YTDL_PASSWORD: required to open the app (meant for network mode)
            "dev": False}        # --dev: `next dev` (another port) may ask for the token at /api/dev-token
SESSION_COOKIE = "ytdl_session"
sessions: set[str] = set()        # logged-in browsers (in memory: a restart asks for the password again)
login_failures: dict[str, deque] = defaultdict(deque)   # client ip -> times of recent wrong passwords
LOGIN_LIMIT = 5                   # wrong passwords per minute per client
DEV_TOKEN_PATH = "/api/dev-token"


async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    path = request.url.path
    if not security["any_host"] and host not in LOOPBACK_HOSTS:
        return PlainTextResponse("Forbidden: unknown host", status_code=403)
    if security["password"] and path != "/login" and request.cookies.get(SESSION_COOKIE) not in sessions:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Password needed — reload the page."}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    if path.startswith("/api/") and not (security["dev"] and path == DEV_TOKEN_PATH):
        # EventSource can't send headers, so the live stream (only) takes the token from the query string.
        sent = request.query_params.get("token", "") if path == "/api/stream" else request.headers.get("x-token", "")
        if not secrets.compare_digest(sent, TOKEN):
            return JSONResponse({"detail": "Session expired — reload the page."}, status_code=403)
    response = await call_next(request)
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"   # always revalidate: an update must never run old code
    return response


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>YT Downloader</title><style>
:root{--bg:#f6f6f4;--card:#fff;--text:#1d1d1b;--muted:#6b6b66;--border:#deded9;--accent:#d93a32}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--text:#ecece8;--muted:#a3a39c;--border:#363633}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px;width:100%;max-width:340px}
h1{font-size:18px;margin:0 0 4px}p{color:var(--muted);margin:0 0 18px}.err{color:var(--accent)}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border-radius:9px}
input{border:1px solid var(--border);background:var(--bg);color:var(--text);margin-bottom:12px}
button{border:0;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
</style></head><body><form method="post" action="/login"><h1>YT Downloader</h1><p class="{cls}">{msg}</p>
<input type="password" name="password" placeholder="Password" autofocus required autocomplete="current-password">
<button>Open</button></form></body></html>"""


def login_page(msg: str = "Enter the password to open the downloader.", error: bool = False, status: int = 200):
    page = LOGIN_PAGE.replace("{cls}", "err" if error else "").replace("{msg}", html.escape(msg))
    return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/login")
def login_form():
    return login_page()


@router.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    recent = login_failures[ip]
    while recent and time.time() - recent[0] > 60:
        recent.popleft()
    if len(recent) >= LOGIN_LIMIT:
        return login_page("Too many wrong passwords — wait a minute and try again.", True, 429)
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    given = (form.get("password") or [""])[0]
    if not security["password"] or not secrets.compare_digest(given.encode(), security["password"].encode()):
        recent.append(time.time())
        return login_page("Wrong password.", True, 401)
    session = secrets.token_urlsafe(32)
    sessions.add(session)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, session, max_age=30 * 86400, httponly=True, samesite="strict")
    return response


@router.get("/api/dev-token")
def dev_token():
    """Only with --dev: the page served by `next dev` can't have the token written into it, so it asks here.
    Still behind the Host check (and the password, if one is set)."""
    if not security["dev"]:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return {"token": TOKEN}
