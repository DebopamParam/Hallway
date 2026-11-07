"""The organiser dashboard: a small web app on its own loopback-only port, in
the same process as Hallway, so it works on the live event (attendees, day
passes, settings) rather than a copy of it.

    python -m app.organiser          # Hallway on :8787 and the dashboard on http://127.0.0.1:8788

Attendees can't reach it: it has no routes on Hallway's port, and its own port
is bound to loopback (ORGANISER_HOST must be a loopback address; from another
machine, use an SSH tunnel). It also refuses any Host header but its own, so a
web page can't reach it through DNS rebinding.

Sign-in is ORGANISER_PASSWORD (or HALLWAY_ADMIN_KEY when that's unset),
compared in constant time. A session is a random cookie (HttpOnly,
SameSite=Strict, Secure over HTTPS) kept in memory, so a restart signs you out.
Every write needs that session's CSRF token in x-csrf-token and an Origin of
the dashboard itself.

Provider keys are write-only here: they're stored by app/providers.py (0600)
and only "set", where from and the last 4 characters come back.
"""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, mcp_server, providers, research, security
from .config import AGENDA, settings
from .core import HallwayError
from .main import _admin_attendee_view, _agent_online, _invite_view, _remaining_usd, hallway

log = logging.getLogger("hallway.organiser")

UI = Path(__file__).parent / "organiser_ui"
STATIC = Path(__file__).parent / "static"
COOKIE = "hw_organiser"
SESSION_TTL_S = 12 * 3600
LOGIN_FAILS = 5  # wrong passwords allowed per LOGIN_WINDOW_S, across all callers
LOGIN_WINDOW_S = 60
PASSWORD_MIN = 12

# token sha256 -> (csrf token, expires at). In memory: one process, and a restart signs everyone out.
_sessions: dict[str, tuple[str, float]] = {}
_fails: deque[float] = deque()


def password() -> str:
    """ORGANISER_PASSWORD, else the organiser's admin key; "" when neither is usable."""
    return config.usable_key(os.environ.get("ORGANISER_PASSWORD", "").strip()) or config.admin_key(settings)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _password_ok(sent: str) -> bool:
    """Constant time, and comparing digests so the length doesn't leak either."""
    want = password()
    return bool(want) and secrets.compare_digest(_digest(sent), _digest(want))


def _session(request: Request) -> tuple[str, str] | None:
    """(token hash, csrf token) for a live session cookie, else None."""
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None
    h = hashlib.sha256(token.encode()).hexdigest()
    hit = _sessions.get(h)
    if not hit or hit[1] < time.time():
        _sessions.pop(h, None)
        return None
    return h, hit[0]


def _require(request: Request) -> None:
    """A signed-in organiser; for a write, also the CSRF token and our own Origin."""
    s = _session(request)
    if not s:
        raise HTTPException(401, "Sign in first.")
    if request.method in security.UNSAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(sent.encode(), s[1].encode()):
            raise HTTPException(403, "Missing or stale CSRF token: reload the page.")


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


class Guard:
    """Host and Origin checks, plus no-store and the attendee app's security headers."""

    def __init__(self, app: ASGIApp, port: int) -> None:
        self.app, self.port = app, port

    def _host_ok(self, value: str) -> bool:
        m = re.fullmatch(r"(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(?::(\d+))?", value)
        return m is not None and _loopback(m.group(1)) and int(m.group(2) or 80) == self.port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = None
        if not self._host_ok(headers.get("host", "")):
            problem = "This dashboard only answers on localhost."
        elif scope["method"] in security.UNSAFE_METHODS:
            origin = headers.get("origin", "")
            if not origin.startswith("http://") or not self._host_ok(origin.removeprefix("http://")):
                problem = security.CSRF_REFUSED
        extra = [
            (b"content-security-policy", security.CSP.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cache-control", b"no-store"),
        ]

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [(k, v) for k, v in extra if k not in present]
            await send(message)

        if problem:
            body = json.dumps({"error": problem}).encode()
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send_with_headers)


def build(port: int) -> ASGIApp:
    app = FastAPI(title="Hallway organiser", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static/fonts", StaticFiles(directory=STATIC / "fonts"), name="fonts")
    app.mount("/static/brand", StaticFiles(directory=STATIC / "brand"), name="brand")
    app.mount("/ui", StaticFiles(directory=UI), name="ui")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 echoes the input back, which could be a key: name the field, never the value.
        fields = sorted({".".join(str(x) for x in e["loc"][1:]) or "body" for e in exc.errors()})
        return JSONResponse({"error": f"Check {', '.join(fields)}."}, status_code=422)

    @app.exception_handler(HallwayError)
    async def _hallway_error(_req: Request, exc: HallwayError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(HTTPException)
    async def _http_error(_req: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(UI / "index.html")

    # ------------------------------------------------------------ session
    class Login(BaseModel):
        password: str = Field(max_length=500)

    @app.post("/api/login")
    async def login(body: Login, request: Request) -> JSONResponse:
        now = time.time()
        while _fails and _fails[0] < now - LOGIN_WINDOW_S:
            _fails.popleft()
        if len(_fails) >= LOGIN_FAILS:
            raise HTTPException(429, "Too many wrong passwords. Wait a minute.")
        if not _password_ok(body.password):
            _fails.append(now)
            log.warning("organiser dashboard: wrong password")
            raise HTTPException(401, "Wrong password.")
        for h in [h for h, (_, until) in _sessions.items() if until < now]:
            del _sessions[h]
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        _sessions[hashlib.sha256(token.encode()).hexdigest()] = (csrf, now + SESSION_TTL_S)
        log.info("organiser dashboard: signed in")
        resp = JSONResponse({"signed_in": True, "csrf": csrf})
        resp.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_TTL_S,
            httponly=True,
            samesite="strict",
            # Secure whenever it's served over HTTPS. Plain http://127.0.0.1 can't
            # carry a Secure cookie in every browser; the port is loopback-only.
            secure=request.url.scheme == "https",
        )
        return resp

    @app.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        _require(request)
        s = _session(request)
        if s:
            _sessions.pop(s[0], None)
        resp = JSONResponse({"signed_in": False})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/session")
    async def session(request: Request) -> dict:
        s = _session(request)
        return {"signed_in": bool(s), "csrf": s[1] if s else None}

    # ------------------------------------------------------------ event
    @app.get("/api/overview")
    async def overview(request: Request) -> dict:
        _require(request)
        people = hallway.store.attendees()
        rows = []
        for a in people:
            view = _admin_attendee_view(a)
            view["online"] = _agent_online(a)
            rows.append(view)
        passes = [r["pass"] for r in rows if r["pass"]]
        spent = hallway.store.one("SELECT COALESCE(SUM(spent_usd), 0) AS s, COUNT(*) AS n FROM passes") or {}
        day = research.today()
        return {
            "event": {
                "name": settings.event_name,
                "agenda": AGENDA,
                "daypass_enabled": settings.daypass_enabled,
                "daypass_cap_usd": settings.daypass_cap_usd,
                "max_attendees": settings.max_attendees,
                "public_url": settings.public_url,
            },
            "invite": _invite_view(),
            "counts": {
                "attendees": sum(not a["is_persona"] for a in people),
                "personas": sum(bool(a["is_persona"]) for a in people),
                "online": sum(r["online"] for r in rows),
                "modes": {m: sum(a["mode"] == m for a in people) for m in ("connect", "live", "daypass")},
            },
            "daypass": {
                "active": sum(p["status"] == "active" for p in passes),
                "paused": sum(p["status"] == "paused" for p in passes),
                "spent_out": sum(p["status"] == "spent" for p in passes),
                "budget_usd": round(sum(p["cap_usd"] for p in passes), 2),
                "spent_usd": round(sum(p["spent_usd"] for p in passes), 4),
                "spent_all_time_usd": round(float(spent.get("s") or 0), 4),
                "runs": sum(p["runs"] for p in passes),
            },
            "research": {
                "enabled": research.enabled(),
                "today": hallway.store.kv_get(f"research:{day}") or 0,
                "daily_cap": settings.research_daily_cap,
            },
            "attendees": rows,
        }

    class AgendaItem(BaseModel):
        time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
        title: str = Field(min_length=1, max_length=100)
        room: str = Field(default="", max_length=60)

    class EventEdit(BaseModel):
        name: str = Field(min_length=1, max_length=80)
        agenda: list[AgendaItem] = Field(max_length=40)
        daypass_enabled: bool
        daypass_cap_usd: float = Field(ge=0, le=1000)

    @app.put("/api/event")
    async def edit_event(body: EventEdit, request: Request) -> dict:
        """Saved in the database and applied at once: the attendee app, new day
        passes and agents that connect from now on see it."""
        _require(request)
        values = {
            "event_name": " ".join(body.name.split()),
            "daypass_enabled": body.daypass_enabled,
            "daypass_cap_usd": round(body.daypass_cap_usd, 2),
            "agenda": sorted(
                ({"time": i.time, "title": i.title.strip(), "room": i.room.strip()} for i in body.agenda),
                key=lambda i: i["time"],
            ),
        }
        hallway.store.kv_set(config.OVERRIDES_KEY, values)
        config.apply_overrides(values)
        mcp_server.refresh_instructions()
        log.info("organiser dashboard: event settings saved")
        return await overview(request)

    # ------------------------------------------------------------ day passes
    def _attendee(handle: str) -> dict:
        a = hallway.store.attendee(handle=handle.strip().lstrip("@").lower())
        if not a:
            raise HTTPException(404, f"No attendee with the handle '{handle}'.")
        return a

    def _pass(a: dict) -> dict:
        view = _admin_attendee_view(hallway.store.need_attendee(id=a["id"]))
        p = hallway.day_pass(a)
        view["online"] = _agent_online(a)
        if p:
            view["pass"]["remaining_usd"] = _remaining_usd(p)
        return view

    @app.post("/api/passes/{handle}/issue")
    async def issue(handle: str, request: Request) -> dict:
        """Give an attendee a day pass: the organiser's agent drives theirs from now on
        (the attendee's own switch in the app, made for them). A pass they had before
        comes back with what it already spent."""
        _require(request)
        a = hallway.switch_mode(_attendee(handle), "daypass")
        hallway.poke()  # the worker's long poll picks it up now
        log.info("organiser dashboard: day pass issued to %s", a["handle"])
        return _pass(a)

    @app.post("/api/passes/{handle}/revoke")
    async def revoke(handle: str, request: Request) -> dict:
        """Take a day pass away: back to Connect (their own agent), and the token of
        a run in progress stops working at once. The pass row and its spend stay."""
        _require(request)
        a = _attendee(handle)
        if a["mode"] != "daypass":
            raise HallwayError(f"{a['handle']} has no day pass.")
        a = hallway.switch_mode(a, "connect")
        hallway.store.revoke_worker_tokens(a["id"])
        log.info("organiser dashboard: day pass revoked from %s", a["handle"])
        return _pass(a)

    class PassEdit(BaseModel):
        paused: bool | None = None
        cap_usd: float | None = Field(default=None, ge=0, le=1000)

    @app.post("/api/passes/{handle}")
    async def edit_pass(handle: str, body: PassEdit, request: Request) -> dict:
        """Pause or resume a pass, and/or set its budget cap (as `hallway_admin.py pass`)."""
        _require(request)
        if body.paused is None and body.cap_usd is None:
            raise HallwayError("Send paused and/or cap_usd.")
        a = _attendee(handle)
        hallway.admin_set_pass(a, paused=body.paused, cap_usd=body.cap_usd)
        return _pass(a)

    # ------------------------------------------------------------ providers
    @app.get("/api/providers")
    async def provider_status(request: Request) -> dict:
        _require(request)
        return providers.status()

    class ProviderEdit(BaseModel):
        # None: leave as is; "": remove (back to the environment). Values are never sent back.
        keys: dict[str, str | None] = Field(default_factory=dict)
        options: dict[str, str | None] = Field(default_factory=dict)

    @app.put("/api/providers")
    async def provider_edit(body: ProviderEdit, request: Request) -> dict:
        _require(request)
        for name in body.keys:
            if name not in providers.KEYS:
                raise HallwayError(f"Unknown key {name[:40]!r}.")
        for name in body.options:
            if name not in providers.OPTIONS:
                raise HallwayError(f"Unknown setting {name[:40]!r}.")
        for name, value in body.keys.items():
            if value and (len(value.strip()) > providers.KEY_MAX or not re.fullmatch(r"[\x21-\x7e]+", value.strip())):
                raise HallwayError(f"{name} doesn't look like an API key (printable characters, no spaces).")
        model = (body.options.get("DAYPASS_MODEL") or "").strip()
        if model and not re.fullmatch(r"[A-Za-z0-9._:/@-]{1,120}", model):
            raise HallwayError("DAYPASS_MODEL is a model name such as claude-haiku-5-5 or vercel:provider/model.")
        price = (body.options.get("DAYPASS_PRICE_PER_MTOK") or "").strip()
        if price and not re.fullmatch(r"\d+(\.\d+)?,\d+(\.\d+)?", price):
            raise HallwayError('DAYPASS_PRICE_PER_MTOK is "in,out" in USD per million tokens, such as 0.10,0.40.')
        providers.update({**body.keys, **body.options})
        return providers.status()

    @app.post("/api/providers/{name}/test")
    async def provider_test(name: str, request: Request) -> dict:
        """A cheap call with the stored key: listing models (Anthropic), the
        credit balance (Vercel), or one search with no page contents (Exa,
        about half a cent). Says whether the key works, never what it is."""
        _require(request)
        if name not in providers.KEYS:
            raise HTTPException(404, "Unknown provider.")
        key = providers.get(name)
        if not key:
            return {"ok": False, "detail": "Not set."}
        return await probe(name, key)

    return Guard(app, port)


PROBES: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    "ANTHROPIC_API_KEY": ("GET", "https://api.anthropic.com/v1/models?limit=1", None),
    "VERCEL_AI_GATEWAY_API_KEY": ("GET", "https://ai-gateway.vercel.sh/v1/credits", None),
    "EXA_API_KEY": ("POST", research.EXA_URL, {"query": "Hallway test", "numResults": 1}),
}


def _probe_headers(name: str, key: str) -> dict[str, str]:
    if name == "ANTHROPIC_API_KEY":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if name == "EXA_API_KEY":
        return {"x-api-key": key}
    return {"authorization": f"Bearer {key}"}


async def probe(name: str, key: str) -> dict:
    method, url, body = PROBES[name]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(method, url, json=body, headers=_probe_headers(name, key))
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"Couldn't reach the provider ({type(e).__name__})."}
    if r.status_code == 200:
        return {"ok": True, "detail": "The key works."}
    if r.status_code in (401, 403):
        return {"ok": False, "detail": "The provider refused this key."}
    return {"ok": False, "detail": f"The provider answered HTTP {r.status_code}."}


# ---------------------------------------------------------------- running
def dispatch(public: ASGIApp, dashboard: ASGIApp, dashboard_port: int) -> ASGIApp:
    """One ASGI app for both sockets: requests to the dashboard's port go to the
    dashboard, everything else (and the lifespan) to Hallway."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        server = scope.get("server") or (None, None)
        if scope["type"] != "lifespan" and server[1] == dashboard_port:
            return await dashboard(scope, receive, send)
        return await public(scope, receive, send)

    return app


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.set_inheritable(True)
    return s


def main() -> None:
    import uvicorn

    from .main import app as public_app

    ap = argparse.ArgumentParser(description="Hallway plus the organiser dashboard, in one process.")
    ap.add_argument("--host", default=os.environ.get("HALLWAY_HOST", "127.0.0.1"), help="Hallway's address")
    ap.add_argument("--port", type=int, default=int(os.environ.get("HALLWAY_PORT", "8787")))
    ap.add_argument("--dashboard-host", default=os.environ.get("ORGANISER_HOST", "127.0.0.1"))
    ap.add_argument("--dashboard-port", type=int, default=int(os.environ.get("ORGANISER_PORT", "8788")))
    a = ap.parse_args()
    if not _loopback(a.dashboard_host):
        raise SystemExit(
            "ORGANISER_HOST must be a loopback address (127.0.0.1, ::1). Use an SSH tunnel from elsewhere."
        )
    if len(password()) < PASSWORD_MIN:
        raise SystemExit(
            f"Set ORGANISER_PASSWORD (at least {PASSWORD_MIN} characters) or HALLWAY_ADMIN_KEY in .env first: "
            "openssl rand -hex 16"
        )
    sockets = [_bind(a.host, a.port), _bind(a.dashboard_host, a.dashboard_port)]
    app = dispatch(public_app, build(a.dashboard_port), a.dashboard_port)
    server = uvicorn.Server(uvicorn.Config(app, proxy_headers=True, log_config=None))
    log.info("organiser dashboard: http://%s:%d", a.dashboard_host, a.dashboard_port)
    asyncio.run(server.serve(sockets=sockets))


if __name__ == "__main__":
    main()
