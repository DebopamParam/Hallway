"""Hallway web server: the attendee app (SPA + JSON API), the MCP endpoint at
/mcp, and the privacy guard Switch calls at /guardrails/check."""

import asyncio
import contextlib
import csv
import functools
import hashlib
import io
import ipaddress
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, guard, mailer, mcp_server, metrics, obs, security
from .config import AGENDA, check_keys, check_signin, settings
from .core import AUTONOMY_KEYS, LOCKED_ASK, AlreadyJoined, Hallway, HallwayError, clean_profile, public_profile, vault
from .ratelimit import RateLimited, limiter
from .store import token_hash
from .switch import GatewayClient, SwitchError

# LOG_FORMAT=json|text (app/obs.py). Request ids and the attendee id on every
# line; sign-in tokens (?t=), MCP keys (?key=, hw_...) and bearer headers are
# redacted everywhere, uvicorn's access log included.
obs.setup_logging(settings.log_format)
log = logging.getLogger("hallway")
logging.getLogger("httpx").setLevel(logging.WARNING)
_RedactSecrets = obs.RedactSecrets  # the access log's filter, under its old name

STATIC = Path(__file__).parent / "static"
hallway = Hallway()
mcp_server.hallway = hallway
mcp_app = mcp_server.http_app()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    check_signin(settings)
    for warning in check_keys(settings):
        log.warning(warning)
    if "DEV_MAGIC_LINKS" in os.environ and "SIGNIN_MODE" not in os.environ:
        log.warning("DEV_MAGIC_LINKS is deprecated: set SIGNIN_MODE=%s instead.", settings.signin_mode)
    log.info(
        "sign-in mode: %s%s",
        settings.signin_mode,
        f" (email via {settings.email_sender})" if settings.signin_mode == "email" else "",
    )
    config.apply_overrides(hallway.store.kv_get(config.OVERRIDES_KEY) or {})  # the organiser dashboard's edits
    mcp_server.refresh_instructions()
    await hallway.start()
    async with mcp_server.mcp.session_manager.run():
        yield
    await hallway.stop()


async def _session_write_limit(request: Request) -> None:
    """Cookie-authenticated writes, per session (every route; the organiser's
    admin key is exempt)."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if (
        cookie
        and request.method in security.UNSAFE_METHODS
        and request.url.path.startswith("/api/")
        and not _is_admin(request)
    ):
        limiter.check("session_write", token_hash(cookie))


_docs = config.api_docs_enabled(settings)  # off in production, see config.api_docs_enabled
app = FastAPI(
    title="Hallway",
    lifespan=lifespan,
    dependencies=[Depends(_session_write_limit)],
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)
# Security headers on every response (the SPA, the API, /mcp) and the Origin
# check on cookie-authenticated writes. Reads `settings` per request.
app.add_middleware(security.SecurityMiddleware, get_settings=lambda: settings)
app.add_middleware(obs.RequestMiddleware)  # added last: outermost, so it times and ids everything
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(RateLimited)
async def _rate_limited(_req: Request, exc: RateLimited) -> JSONResponse:
    return JSONResponse(
        {
            "error": f"Too many requests. Wait {exc.retry_after} s and try again.",
            "code": "rate_limited",
            "retry_after": exc.retry_after,
        },
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(HallwayError)
async def _hallway_error(_req: Request, exc: HallwayError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(SwitchError)
async def _switch_error(_req: Request, exc: SwitchError) -> JSONResponse:
    log.warning("switch: %s", exc)
    return JSONResponse({"error": "Switch couldn't do that just now. Please try again in a moment."}, status_code=502)


@app.exception_handler(AlreadyJoined)
async def _already_joined(_req: Request, exc: AlreadyJoined) -> JSONResponse:
    resp = JSONResponse({"error": str(exc), "code": "already_joined"}, status_code=409)
    resp.delete_cookie(SESSION_COOKIE)  # its onboarding is over
    return resp


# ------------------------------------------------------------------ session
# The hw_session cookie is a random token; the server keeps only its sha256:
# a row in `sessions` once the person has an account, or `pre:<hash>` ->
# {email, expires_at} between opening the magic link and finishing onboarding.
# Registering writes the new attendee's id into that record (core.register):
# only the session that created an account may get into it that way, and if
# the registration's response never reached the browser, the same cookie is
# exchanged for a real session on its next request (see _redeem).
SESSION_COOKIE = "hw_session"
ONBOARDING_TTL_S = 2 * 3600  # an onboarding session left unfinished this long must sign in again


def _pre_key(hw_session: str) -> str:
    return f"pre:{token_hash(hw_session)}"


def _pending(hw_session: str) -> dict | None:
    """The onboarding record for this cookie, if it is still valid."""
    rec = hallway.store.kv_get(_pre_key(hw_session))
    if isinstance(rec, str):  # a plain email, from before onboarding expired: it starts its clock now
        rec = {"email": rec, "expires_at": time.time() + ONBOARDING_TTL_S}
        hallway.store.kv_set(_pre_key(hw_session), rec)
    if not isinstance(rec, dict) or not rec.get("email"):
        return None
    if rec.get("expires_at", 0) <= time.time():
        hallway.store.kv_pop(_pre_key(hw_session))
        return None
    return rec


def _lookup(hw_session: str | None) -> tuple[dict | None, dict | None, dict | None]:
    """(attendee, pending onboarding record, session row). Any may be None."""
    if not hw_session:
        return None, None, None
    s = hallway.store.session(hw_session)
    a = hallway.store.attendee(id=s["attendee_id"]) if s else None
    if a:
        obs.set_attendee(a["id"])
        return a, None, s
    return None, _pending(hw_session), None


def _session(hw_session: str | None) -> tuple[dict | None, str | None]:
    """(attendee, pending_email). Either may be None."""
    a, rec, _ = _lookup(hw_session)
    return a, rec["email"] if rec else None


def _redeem(rec: dict, hw_session: str, request: Request, response: Response) -> dict | None:
    """The account this onboarding cookie created, now signed in with a fresh
    session, or None if it created none. Single use."""
    aid = rec.get("attendee_id")
    a = hallway.store.attendee(id=aid) if aid else None
    if not a or a["email"] != rec["email"] or not hallway.store.kv_pop(_pre_key(hw_session)):
        return None
    obs.set_attendee(a["id"])
    _set_session_cookie(response, hallway.store.create_session(a["id"], _ua(request)), settings.session_ttl_s)
    return a


def _set_session_cookie(resp: Response, token: str, max_age: float) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=max(0, int(max_age)),
        secure=settings.public_url.startswith("https://"),
    )


def client_ip(request: Request) -> str:
    """The visitor's IP: the one place that knows about CF-Connecting-IP.
    Behind Cloudflare it's that header; otherwise the peer address (uvicorn has
    already applied X-Forwarded-For from FORWARDED_ALLOW_IPS proxies).
    TRUST_CF_CONNECTING_IP=false ignores the header."""
    if settings.trust_cf_connecting_ip:
        try:
            return str(ipaddress.ip_address(request.headers.get("cf-connecting-ip", "").strip()))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")


def _me(hw_session: str | None) -> dict:
    a, _ = _session(hw_session)
    if not a:
        raise HTTPException(401, "Sign in first.")
    return a


AGENT_ACTIVE_S = 900  # an agent seen in the last 15 minutes counts as active


AGENT_ONLINE_S = 45  # a watcher starts a poll at least every 25 s, so 45 s of silence means it's gone
AGENT_RUN_QUIET_S = 300  # mid-run, its tool calls keep it seen; a run quiet this long died with its watcher


def _agent_active(a: dict) -> bool:
    return bool(a.get("agent_seen_at") and time.time() - a["agent_seen_at"] < AGENT_ACTIVE_S)


def _agent_online(a: dict) -> bool:
    """Is their agent there right now? A day pass is while the pass runs; a Live
    stand-in while its Switch socket is up; an agent they connect while its
    watcher polls (or it is mid-run, or calling Hallway's tools)."""
    if a["mode"] == "daypass":
        p = hallway.day_pass(a)
        return bool(p and p["status"] == "active")
    if a["mode"] == "live":
        return a["id"] in hallway.presence.connected
    quiet = time.time() - (a.get("agent_seen_at") or 0)
    return quiet < AGENT_ONLINE_S or (quiet < AGENT_RUN_QUIET_S and hallway.agent_busy(a))


def _mcp_url() -> str:
    return f"{settings.public_url}/mcp"


def _remaining_usd(p: dict) -> float:
    """What's left of a pass's budget. Never negative: one run can overshoot the cap."""
    left: float = round(p["cap_usd"] - p["spent_usd"], 6)
    return max(0.0, left)


def _pass_view(a: dict) -> dict | None:
    p = hallway.day_pass(a)
    if not p:
        return None
    runs = hallway.store.all(
        "SELECT cost_usd, tool_calls, summary, ok, created_at FROM pass_runs WHERE attendee_id = ? ORDER BY id DESC LIMIT 8",
        a["id"],
    )
    return {
        "cap_usd": p["cap_usd"],
        "spent_usd": p["spent_usd"],
        "remaining_usd": _remaining_usd(p),
        "runs": p["runs"],
        "status": p["status"],
        "last_run_at": p["last_run_at"],
        "every_s": settings.daypass_every_s,
        "note": p["note"],
        "note_at": p["note_at"],
        "recent": runs,
    }


KEY_PLACEHOLDER = "<your-hallway-key>"


def _me_view(a: dict, token: str | None = None) -> dict:
    """`token` is the plain MCP key, passed only right after it was issued
    (registration or rotation): it is stored hashed and can't be shown again."""
    key = token or KEY_PLACEHOLDER
    return {
        **public_profile(a),
        "email": a["email"],
        "profile": clean_profile(a["profile"], strict=False),
        "contact": vault(a),
        "autonomy": a["autonomy"],
        "autonomy_labels": AUTONOMY_KEYS,
        "locked": sorted(LOCKED_ASK),
        "privacy": a["privacy"],
        "listed": bool(a["listed"]),
        "web_lookup": bool(a["web_lookup"]),
        "lobby_agent": bool(a["lobby_agent"]),
        "agent_seen_at": a["agent_seen_at"],
        "agent_online": _agent_online(a),
        "agent_verified": bool(a["agent_verified"]),
        "live_proof": hallway.live_proof(a),
        "pass": _pass_view(a),
        "connect": {
            "mcp_url": _mcp_url(),
            "token": token,
            "token_hint": a["mcp_token_hint"],
            "claude_code": f'claude mcp add --transport http hallway {_mcp_url()} --header "Authorization: Bearer {key}"',
            "codex": (
                f'[mcp_servers.hallway]\nurl = "{_mcp_url()}"\nhttp_headers = {{ "Authorization" = "Bearer {key}" }}'
            ),
            "url_with_key": f"{_mcp_url()}?key={key}",
            "kickoff": _kickoff(),
            # One prompt per agent that sets the MCP server up and then starts work.
            "setup": {"cc": _setup_prompt("cc", key), "cx": _setup_prompt("cx", key)},
        },
    }


def _kickoff() -> str:
    return (
        f"You are my agent at {settings.event_name}. Use the hallway MCP tools: call whoami, do the privacy "
        "check, then work the lobby for me. Check my_inbox every few minutes.\n\n"
        "Connect with your built-in MCP client (HTTP transport). To test the endpoint by hand, use curl, "
        "Python httpx or requests, or Node fetch. Don't use Python's urllib: Cloudflare, in front of "
        "public Hallway deployments, refuses its default user agent (HTTP 403, error 1010)."
    )


def _setup_prompt(agent: str, key: str) -> str:
    """What an attendee pastes into Claude Code or Codex: add (or replace) the
    hallway MCP server with their key, then start the watcher (/watch.mjs),
    which runs the agent headless whenever the event feed has something new.
    The watcher starts a fresh agent process each time, so a new key works at
    once; only the session the prompt was pasted into keeps the old setup."""
    url = _mcp_url()
    watch = (
        "mkdir -p ~/.hallway-agent && "
        f"curl -fsSL {settings.public_url}/watch.mjs -o ~/.hallway-agent/hallway-watch.mjs && "
        f"HALLWAY_KEY={key} node ~/.hallway-agent/hallway-watch.mjs {'claude' if agent == 'cc' else 'codex'} "
        "--background"
    )
    if agent == "cc":
        add = (
            "1. Add the Hallway MCP server for your user (remove an old one first, it may hold an old key):\n"
            "   claude mcp remove -s user hallway 2>/dev/null; "
            f'claude mcp add -s user --transport http hallway {url} --header "Authorization: Bearer {key}"'
        )
        restart = "To use the hallway tools in this session too, restart Claude Code (or run /mcp)."
    else:
        add = (
            "1. In ~/.codex/config.toml, replace any existing [mcp_servers.hallway] section (it may hold an old key) "
            "with exactly:\n"
            f'[mcp_servers.hallway]\nurl = "{url}"\nhttp_headers = {{ "Authorization" = "Bearer {key}" }}\n'
            'default_tools_approval_mode = "approve"'
        )
        restart = (
            "To use the hallway tools in this session too, quit and run `codex resume --last`: Codex loads MCP "
            'servers when a session starts, so until then "Unknown Hallway token" means the old key is loaded.'
        )
    return (
        "Set up Hallway for me. You'll set things up; a watcher then runs my agent whenever something happens "
        f"at {settings.event_name}, so nobody has to poll.\n\n{add}\n"
        "2. Start the Hallway watcher in the background (Node 18+) by running this command exactly as written, "
        "with no added flags. It waits on Hallway's event feed without an LLM, runs a headless agent with only the "
        "hallway tools the moment something arrives (and a short proactive round every 20 minutes), and replaces "
        "any watcher already running:\n"
        f"   {watch}\n"
        "3. Check ~/.hallway-agent/watch.log says it is watching, then tell me it's running: I can talk to my agent "
        "and watch what it does in the Hallway app's Agent tab, or follow it with tail -f ~/.hallway-agent/watch.log. "
        f"{restart}\n\n"
        "If you test the endpoint by hand, use curl, Python httpx or requests, or Node fetch. Don't use Python's "
        "urllib: Cloudflare, in front of public Hallway deployments, refuses its default user agent (HTTP 403)."
    )


@app.get("/watch.mjs")
async def watch_script() -> Response:
    """The watcher (static/hallway-watch.mjs) with this Hallway's address in it."""
    body = (STATIC / "hallway-watch.mjs").read_text().replace("__HALLWAY_URL__", settings.public_url)
    return Response(body, media_type="text/javascript; charset=utf-8")


class StartAuth(BaseModel):
    email: str
    invite: str | None = None


# ------------------------------------------------------------- invite gate
def _key_matches(sent: str | None, key: str) -> bool:
    """Constant-time comparison; an unset or placeholder key never matches."""
    key = config.usable_key(key)
    return bool(key) and secrets.compare_digest((sent or "").encode(), key.encode())


def _is_admin(request: Request) -> bool:
    """The organiser: HALLWAY_ADMIN_KEY in x-hallway-admin (admin endpoints, and
    seeding personas). Deliberately not "any localhost caller": behind a tunnel
    or reverse proxy every visitor arrives from 127.0.0.1."""
    return _key_matches(request.headers.get("x-hallway-admin"), config.admin_key(settings))


def _local_dev_caller(request: Request) -> bool:
    """A loopback caller on a dev box: HALLWAY_ENV=dev AND a localhost public URL.
    Never on a deployed instance, where a tunnel makes every visitor look local."""
    return (
        settings.hallway_env == "dev"
        and config.is_local_url(settings)
        and request.client is not None
        and request.client.host in ("127.0.0.1", "::1", "localhost")
    )


def _joined() -> int:
    """People who joined as attendees. Seeded personas don't take a demo spot."""
    n: int = hallway.store.need("SELECT COUNT(*) AS n FROM attendees WHERE is_persona = 0")["n"]
    return n


def _spots_left() -> int | None:
    return max(0, settings.invite_limit - _joined()) if settings.invite_limit > 0 else None


class JoinRefused(Exception):
    def __init__(self, code: str):
        self.code = code


INVITE_ONLY = "Hallway's live demo is invite only. Open the invite link you were sent."


def _demo_full_message() -> str:
    return f"This demo is full: all {settings.invite_limit} spots are taken."


INVITE_CODE_KEY = "admin:invite_code"  # set by the organiser (rotate); overrides INVITE_CODE


def _invite_code() -> str:
    code: str = hallway.store.kv_get(INVITE_CODE_KEY) or settings.invite_code
    return code


def _check_invite(invite: str | None) -> None:
    code = _invite_code()
    if code and not secrets.compare_digest((invite or "").strip().encode(), code.encode()):
        raise JoinRefused("invite_only")


def _check_spots() -> None:
    if _spots_left() == 0:
        raise JoinRefused("demo_full")


@app.exception_handler(JoinRefused)
async def _join_refused(_req: Request, exc: JoinRefused) -> JSONResponse:
    if exc.code == "invite_only":
        return JSONResponse({"error": INVITE_ONLY, "code": "invite_only"}, status_code=403)
    return JSONResponse(
        {"error": _demo_full_message(), "code": "demo_full", "demo_video_url": settings.demo_video_url or None},
        status_code=409,
    )


@app.get("/api/event")
async def event() -> dict:
    people = hallway.store.attendees()
    return {
        "name": settings.event_name,
        "signin_mode": settings.signin_mode,
        "daypass_enabled": settings.daypass_enabled,
        "daypass": {"price": settings.daypass_price, "cap_usd": settings.daypass_cap_usd}
        if settings.daypass_enabled
        else None,
        "max_attendees": settings.max_attendees,
        "lobby": settings.lobby_room_name,
        "attendees": len(people),
        "agents_active": sum(_agent_online(a) for a in people),
        "agenda": AGENDA,
        "invite_only": bool(_invite_code()),
        "invite_limit": settings.invite_limit or None,
        "spots_left": _spots_left(),
        "demo_video_url": settings.demo_video_url or None,
    }


ALREADY_JOINED = "This email already joined. Open Hallway on the device you signed up with."


def _issue_link(email: str, *, join: bool) -> str:
    """A fresh single-use sign-in link. Only the fact that one was issued is
    logged. `join`: issued for an email with no account yet, so it may only
    start an onboarding, never sign in to an account made since."""
    now = time.time()
    hallway.store.kv_purge("magic:", now - settings.magic_link_ttl_s)
    token = secrets.token_urlsafe(24)
    hallway.store.kv_set(f"magic:{token}", {"email": email, "at": now, "join": join})
    metrics.signin_links.inc(mode=settings.signin_mode)
    log.info("sign-in link issued for %s (%s mode)", mailer.mask_email(email), settings.signin_mode)
    return f"{settings.public_url}/auth/magic?t={token}"


def _signin_email(link: str) -> tuple[str, str]:
    minutes = max(1, settings.magic_link_ttl_s // 60)
    return (
        f"Your sign-in link for {settings.event_name}",
        f"Open this link to sign in to Hallway at {settings.event_name}:\n\n{link}\n\n"
        f"It works once, for the next {minutes} minutes. If you didn't ask for it, ignore this email.",
    )


def _refusal_email(code: str) -> tuple[str, str]:
    why = INVITE_ONLY if code == "invite_only" else _demo_full_message()
    video = f"\n\nWatch the demo instead: {settings.demo_video_url}" if settings.demo_video_url else ""
    return (
        f"About signing in to {settings.event_name}",
        f"Someone asked to join Hallway at {settings.event_name} with this email.\n\n{why}{video}\n\n"
        "If it wasn't you, ignore this email.",
    )


@app.post("/api/auth/start")
async def auth_start(body: StartAuth, request: Request) -> dict:
    email = body.email.strip().lower()
    admin = _is_admin(request)
    if not admin:
        limiter.check("auth_start_ip", client_ip(request))
    if "@" not in email:
        raise HallwayError("Enter a valid email.")
    if admin:
        # Organiser tooling (scripts/personas.py): a link for any email, in any mode.
        return {"sent": True, "dev_link": _issue_link(email, join=False)}
    limiter.check("auth_start_email", email)
    mode = settings.signin_mode
    existing = hallway.store.attendee(email=email)
    if mode == "email":
        # Same answer whether or not the email has an account, and whether or
        # not it may join: no enumeration. A refusal goes to the inbox instead.
        try:
            if not existing:
                _check_invite(body.invite)
                _check_spots()
            subject, text = _signin_email(_issue_link(email, join=not existing))
        except JoinRefused as refused:
            subject, text = _refusal_email(refused.code)
        try:
            await mailer.make_sender(settings).send(email, subject, text)
        except mailer.SendError as e:
            log.error("sign-in email to %s failed: %s", mailer.mask_email(email), e)
            raise HTTPException(503, "We couldn't send the email just now. Try again in a minute.") from e
        return {"sent": True, "dev_link": None}
    if mode == "demo" and existing:
        # No email provider, so a link on screen for an existing account would
        # be a sign-in as somebody else. They're already signed in on their device.
        return {"sent": False, "dev_link": None, "already_joined": True, "message": ALREADY_JOINED}
    if not existing:
        _check_invite(body.invite)
        _check_spots()
    return {"sent": True, "dev_link": _issue_link(email, join=not existing)}


@app.get("/auth/magic")
async def auth_magic(t: str, request: Request, hw_session: str | None = Cookie(None)) -> RedirectResponse:
    limiter.check("magic_ip", client_ip(request))
    current, _ = _session(hw_session)
    peek = hallway.store.kv_get(f"magic:{t}")
    if current and peek and peek["email"] != current["email"]:
        # Signed in as someone else on this device: never swap accounts behind
        # their back. The link stays unused, so it still works elsewhere.
        metrics.signins.inc(outcome="signed_in_elsewhere")
        return RedirectResponse("/#/now?signedin=1", status_code=303)
    rec = hallway.store.kv_pop(f"magic:{t}")  # single use: gone on first open
    if not rec or time.time() - rec["at"] > settings.magic_link_ttl_s:
        metrics.signins.inc(outcome="expired")
        return RedirectResponse("/#/?expired=1")
    a = hallway.store.attendee(email=rec["email"])
    if a and rec.get("join"):
        # Issued to join with an email that had no account yet, and someone
        # has joined with it since: on the demo nobody checked that inbox, so
        # this is not a sign-in to that account.
        metrics.signins.inc(outcome="already_joined")
        return RedirectResponse("/#/?joined=1")
    metrics.signins.inc(outcome="ok" if a else "onboarding")
    if a:  # every sign-in is a new session; other devices stay signed in
        obs.set_attendee(a["id"])
        session, target = hallway.store.create_session(a["id"], _ua(request)), "/#/home"
    else:
        session, target = secrets.token_urlsafe(32), "/#/onboard"
        hallway.store.kv_purge("pre:", time.time(), field="expires_at")
        hallway.store.kv_set(_pre_key(session), {"email": rec["email"], "expires_at": time.time() + ONBOARDING_TTL_S})
    resp = RedirectResponse(target, status_code=303)
    _set_session_cookie(resp, session, settings.session_ttl_s)
    return resp


@app.post("/api/auth/logout")
async def logout(hw_session: str | None = Cookie(None)) -> JSONResponse:
    """Ends this session on the server too: a copied cookie stops working."""
    if hw_session:
        hallway.store.revoke_session(hw_session)
        hallway.store.kv_pop(_pre_key(hw_session))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/auth/logout-all")
async def logout_all(hw_session: str | None = Cookie(None)) -> JSONResponse:
    """Sign out everywhere: every session of this attendee, this one included."""
    a = _me(hw_session)
    n = hallway.store.revoke_sessions(a["id"])
    log.info("signed out everywhere: %s (%d sessions)", a["handle"], n)
    resp = JSONResponse({"ok": True, "sessions": n})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/session")
async def session_state(hw_session: str | None = Cookie(None)) -> dict:
    """Whether this browser has a session, without a 401 when it hasn't: the
    landing page asks, to send a signed-in attendee into the app."""
    a, rec, _ = _lookup(hw_session)
    return {"signed_in": bool(a or rec), "onboarding": bool(rec and not a)}


@app.get("/api/me")
async def me(request: Request, response: Response, hw_session: str | None = Cookie(None)) -> dict:
    a, rec, s = _lookup(hw_session)
    if a and s and hw_session:
        # The cookie slides with the server-side expiry (the app loads /api/me on open).
        _set_session_cookie(response, hw_session, s["expires_at"] - time.time())
        return _me_view(a)
    if rec and hw_session:
        a = _redeem(rec, hw_session, request, response)
        if a:  # registered, but the response with the new session never arrived
            return _me_view(a)
        return {"onboarding": True, "email": rec["email"]}
    raise HTTPException(401, "Sign in first.")


@app.post("/api/me/agent-key")
async def rotate_agent_key(hw_session: str | None = Cookie(None)) -> dict:
    """A new MCP key, shown once in this response. The old one stops working now."""
    a = _me(hw_session)
    token = hallway.store.set_mcp_token(a["id"])
    log.info("agent key rotated: %s", a["handle"])
    return _me_view(hallway.store.need_attendee(id=a["id"]), token=token)


@app.post("/api/me/verify-agent")
async def verify_agent(hw_session: str | None = Cookie(None)) -> dict:
    """Check now whether my Live agent has posted its code (the sync loop also checks)."""
    a = _me(hw_session)
    if a["mode"] != "live":
        raise HallwayError("Only a Live agent needs verifying.")
    await hallway.verify_live_agent(a)
    return _me_view(hallway.store.need_attendee(id=a["id"]))


class Register(BaseModel):
    name: str
    profile: dict = {}
    contact: dict = {}
    autonomy: dict = {}
    mode: str = "connect"
    live_agent_name: str | None = None
    lobby_agent: bool = False  # set with the row, so a day pass's first run already sees it


@app.post("/api/register")
async def register(body: Register, request: Request, response: Response, hw_session: str | None = Cookie(None)) -> dict:
    persona = _is_admin(request)
    if not persona:
        limiter.check("register_ip", client_ip(request))
    a, rec, _ = _lookup(hw_session)
    if a:
        return _me_view(a)
    if not rec or not hw_session:
        raise HTTPException(401, "Open the sign-in link from your email first.")
    a = _redeem(rec, hw_session, request, response)
    if a:  # this onboarding already created its account
        return _me_view(a)
    email = rec["email"]
    # Checked again here, one registration at a time when there is a limit:
    # links issued while spots were left must not let more than INVITE_LIMIT in.
    async with hallway._lock if settings.invite_limit > 0 else contextlib.nullcontext():
        if not persona and not hallway.store.attendee(email=email):
            _check_spots()
        try:
            a = await hallway.register(
                name=body.name,
                email=email,
                profile=body.profile,
                contact=body.contact,
                autonomy=body.autonomy,
                mode=body.mode,
                live_agent_name=body.live_agent_name,
                lobby_agent=body.lobby_agent and body.mode != "live",
                is_persona=persona,
                onboarding_key=_pre_key(hw_session),
            )
        except AlreadyJoined:
            # Someone else's onboarding created this email's account first.
            hallway.store.kv_pop(_pre_key(hw_session))
            raise
    obs.set_attendee(a["id"])
    if a.get("mcp_token"):  # only a new attendee gets their key in the response
        metrics.registrations.inc(mode=a["mode"], persona=str(persona).lower())
        log.info("registered attendee %s (%s mode)", a["id"], a["mode"])
    # The new account gets a fresh session. The onboarding record stays for a
    # few minutes (marked with the account) in case this response is lost.
    _set_session_cookie(response, hallway.store.create_session(a["id"], _ua(request)), settings.session_ttl_s)
    return _me_view(hallway.store.need_attendee(id=a["id"]), token=a.get("mcp_token"))


class UpdateMe(BaseModel):
    profile: dict | None = None
    contact: dict | None = None
    autonomy: dict | None = None
    listed: bool | None = None
    web_lookup: bool | None = None
    lobby_agent: bool | None = None


@app.put("/api/me")
async def update_me(body: UpdateMe, hw_session: str | None = Cookie(None)) -> dict:
    before = _me(hw_session)
    a = hallway.update_settings(
        before,
        profile=body.profile,
        contact=body.contact,
        autonomy=body.autonomy,
        listed=body.listed,
        web_lookup=body.web_lookup,
        lobby_agent=body.lobby_agent,
    )
    if body.lobby_agent and not before["lobby_agent"]:
        hallway.lobby_opened(a)
    elif body.lobby_agent is False and before["lobby_agent"]:
        hallway.lobby_closed(a)
    return _me_view(a)


class ChatIn(BaseModel):
    text: str


@app.get("/api/me/chat")
async def my_chat(after: int = 0, hw_session: str | None = Cookie(None)) -> dict:
    """Your conversation with your agent: what you asked, its replies, and the
    steps it is taking (every Hallway tool it calls). `busy` while it works."""
    a = _me(hw_session)
    msgs = [
        {"id": m["id"], "role": m["role"], "text": m["text"], "at": m["created_at"]}
        for m in hallway.store.chat(a["id"], after)
    ]
    return {"messages": msgs, "busy": hallway.agent_busy(a)}


@app.post("/api/me/chat")
async def chat_to_agent(body: ChatIn, hw_session: str | None = Cookie(None)) -> dict:
    """Message your own agent. It starts on it right away (a day pass agent
    within a second or two; your own agent through its watcher)."""
    a = _me(hw_session)
    if wait_s := limiter.hit("mcp_call", a["id"]):
        raise HallwayError(f"Slow down a little: try again in {wait_s} s.")
    return {"id": hallway.chat_from_person(a, body.text)}


class AgentSays(BaseModel):
    text: str = ""
    busy: bool | None = None


class ModeChange(BaseModel):
    mode: str


@app.post("/api/me/mode")
async def change_mode(body: ModeChange, hw_session: str | None = Cookie(None)) -> dict:
    """Switch between your own agent (connect) and a day pass (daypass)."""
    a = hallway.switch_mode(_me(hw_session), body.mode)
    log.info("attendee %s switched to %s mode", a["handle"], a["mode"])
    return _me_view(a)


@app.post("/agent/chat")
async def agent_chat(body: AgentSays, request: Request) -> dict:
    """The agent's side of the app chat, for the watcher and the day pass
    worker: `busy` while a run works, then `text`, its reply to its person."""
    a = _agent(request)
    if body.busy is not None:
        hallway.set_agent_busy(a, body.busy)
    hallway.chat_from_agent(a, body.text)
    return {"ok": True}


# ---------------------------------------------------------------- event feed
EVENTS_WAIT_MAX_S = 25  # under Cloudflare's 100 s, and short enough to notice a dead watcher


def _agent(request: Request) -> dict:
    """The attendee whose agent key (or day pass run token) came with the
    request: as a Bearer header like /mcp, x-hallway-key, or ?key=."""
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or request.headers.get("x-hallway-key", "") or request.query_params.get("key", "")
    a = hallway.store.mcp_attendee(token) if token else None
    if not a:
        raise HTTPException(401, "Unknown Hallway key.")
    if not hallway.pass_usable(a):
        raise HTTPException(403, "This day pass is paused or out of budget.")
    if wait_s := limiter.hit("mcp_call", a["id"]):
        raise HTTPException(429, f"Too many requests: wait {wait_s} s.")
    return a


@app.get("/agent/events")
async def agent_events(request: Request, after: str = "", wait: float = 0) -> dict:
    """The agent's event feed, for a watcher script or the day pass worker: no
    LLM, no tokens. Holds the request up to `wait` seconds until something is
    new for the agent (a mention, a message in one of its small rooms, a
    meeting request, a decision), then returns it with the next `after`.
    Authenticated like /mcp (the agent's key). Reading it marks nothing read."""
    a = _agent(request)
    cursor = None
    if after:
        try:
            n, t = after.split(".", 1)
            cursor = {"n": int(n), "t": int(t)}
        except ValueError:
            raise HTTPException(400, "Bad 'after': pass back the value from the last answer.") from None
    # A watcher counts as the agent online. Marked as the poll starts, not when it
    # ends: a poll a dead watcher left behind still runs out its wait.
    hallway.store.update("attendees", a["id"], agent_seen_at=time.time())
    events, cursor = await hallway.wait_events(a, cursor, max(0.0, min(wait, EVENTS_WAIT_MAX_S)))
    return {"events": events, "after": f"{cursor['n']}.{cursor['t']}"}


class PassState(BaseModel):
    paused: bool | None = None
    note: str | None = None


@app.post("/api/me/pass")
async def pass_state(body: PassState, hw_session: str | None = Cookie(None)) -> dict:
    a = _me(hw_session)
    if body.paused is not None:
        hallway.set_pass_paused(a, body.paused)
    if body.note is not None:
        hallway.set_pass_note(a, body.note)
    return _me_view(a)


# -------------------------------------------------------------------- rooms
@app.get("/api/rooms")
async def rooms(hw_session: str | None = Cookie(None)) -> list[dict]:
    a = _me(hw_session)
    out = []
    for room in hallway.store.all("SELECT * FROM rooms ORDER BY created_at"):
        members = await hallway.room_members(room)
        if a["mm_user_id"] in members:
            people = [x for x in hallway.store.attendees() if x["mm_user_id"] in members]
            meet = (
                hallway.store.one("SELECT time, place FROM meetings WHERE room_id = ?", room["id"])
                if room["kind"] == "meet"
                else None
            )
            out.append(
                {
                    "name": room["name"],
                    "title": room["title"],
                    "kind": room["kind"],
                    "meeting": meet,
                    "people": [{"name": x["name"], "handle": x["handle"]} for x in people],
                    "waiting_for": hallway.invited_to(room),
                }
            )
    return out


@app.get("/api/rooms/{name}/messages")
async def room_messages(name: str, since: int | None = None, hw_session: str | None = Cookie(None)) -> dict:
    a = _me(hw_session)
    room = hallway.room_by_ref(name)
    return {
        "room": {"name": room["name"], "title": room["title"], "kind": room["kind"]},
        "messages": await hallway.messages_for_person(a, room, since),
    }


class PostBody(BaseModel):
    text: str
    confirm: bool = False


@app.post("/api/rooms/{name}/messages")
async def room_post(name: str, body: PostBody, hw_session: str | None = Cookie(None)) -> dict:
    a = _me(hw_session)
    room = hallway.room_by_ref(name)
    outcome, findings = guard.scan(body.text)
    if outcome == "redacted" and not body.confirm:
        return {
            "warning": "This looks like private info ("
            + ", ".join(sorted({f.category for f in findings}))
            + "). Others in the room, people and agents, will see it struck out. Send anyway?"
        }
    await hallway.person_post(a, room, body.text)
    return {"ok": True}


class NewRoom(BaseModel):
    title: str
    invite: list[str]


@app.post("/api/rooms")
async def new_room(body: NewRoom, hw_session: str | None = Cookie(None)) -> dict:
    room = await hallway.create_room(_me(hw_session), body.title, body.invite)
    return {"name": room["name"], "title": room["title"], "waiting_for": room["waiting_for"]}


@app.get("/api/invites")
async def invites(hw_session: str | None = Cookie(None)) -> list[dict]:
    """Room invites waiting on you: join, or pass."""
    return hallway.invites(_me(hw_session))


class InviteAnswer(BaseModel):
    accept: bool


@app.post("/api/invites/{invite_id}")
async def answer_invite(invite_id: str, body: InviteAnswer, hw_session: str | None = Cookie(None)) -> dict:
    return await hallway.respond_invite(_me(hw_session), invite_id, body.accept, by_person=True)


# ------------------------------------------------- approvals, meetings, people
@app.get("/api/approvals")
async def approvals(hw_session: str | None = Cookie(None)) -> list[dict]:
    a = _me(hw_session)
    rows = hallway.store.all(
        "SELECT id, kind, summary, status, created_at, decided_at, payload FROM approvals WHERE attendee_id = ? "
        "ORDER BY status = 'pending' DESC, created_at DESC LIMIT 50",
        a["id"],
    )
    # Only a shortlist's payload is for the app (it renders the candidate cards).
    return [{**r, "payload": r["payload"] if r["kind"] == "shortlist" else None} for r in rows]


@app.get("/api/cards")
async def cards(hw_session: str | None = Cookie(None)) -> list[dict]:
    """The pages the attendee's agent made for them, newest first (the HTML itself is at /cards/{id})."""
    a = _me(hw_session)
    return hallway.store.all(
        "SELECT id, title, approval_id, created_at FROM cards WHERE attendee_id = ? ORDER BY created_at DESC LIMIT 10",
        a["id"],
    )


@app.get("/cards/{card_id}")
async def card(card_id: str, hw_session: str | None = Cookie(None)) -> HTMLResponse:
    """One agent-made card, for the app's sandboxed iframe: only to its owner,
    under CARD_CSP (its own sandbox, so opening the URL directly is just as safe)."""
    a = _me(hw_session)
    row = hallway.store.one("SELECT html FROM cards WHERE id = ? AND attendee_id = ?", card_id, a["id"])
    if not row:
        raise HTTPException(404, "No such card.")
    return HTMLResponse(
        row["html"],
        headers={
            "content-security-policy": security.CARD_CSP,
            "x-frame-options": "SAMEORIGIN",
            "referrer-policy": "no-referrer",
            "cache-control": "private, no-store",
        },
    )


class Decide(BaseModel):
    approve: bool
    selected: list[str] | None = None  # a shortlist's picks, by handle


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: Decide, hw_session: str | None = Cookie(None)) -> dict:
    return await hallway.decide(_me(hw_session), approval_id, body.approve, body.selected)


@app.get("/api/meetings")
async def meetings(hw_session: str | None = Cookie(None)) -> list[dict]:
    return hallway.meetings(_me(hw_session))


class Respond(BaseModel):
    accept: bool


@app.post("/api/meetings/{meeting_id}")
async def respond(meeting_id: str, body: Respond, hw_session: str | None = Cookie(None)) -> dict:
    return await hallway.respond_meeting(_me(hw_session), meeting_id, body.accept, by_person=True)


@app.get("/api/people")
async def people(q: str = "", hw_session: str | None = Cookie(None)) -> list[dict]:
    """Public profiles, plus what the app needs to show where you stand with each
    person: is their agent active, is a meeting in play, have you swapped contacts."""
    a = _me(hw_session)
    return _where_you_stand(a, hallway.search_people(a, q, limit=100))


def _where_you_stand(a: dict, found: list[dict]) -> list[dict]:
    """Add to public profiles what the app shows about you and each person: is
    their agent online, is a meeting in play, have you swapped contacts."""
    by_handle = {x["handle"]: x for x in hallway.store.attendees()}
    meetings = {m["with"]: m for m in reversed(hallway.meetings(a)) if m["status"] not in ("declined", "cancelled")}
    contacts = {}
    for c in hallway.store.all("SELECT * FROM contacts WHERE requester_id = ? OR target_id = ?", a["id"], a["id"]):
        other = c["target_id"] if c["requester_id"] == a["id"] else c["requester_id"]
        contacts[other] = c["status"]
    for p in found:
        other = by_handle.get(p["handle"]) or {}
        m = meetings.get(p["handle"])
        p["agent_active"] = bool(other) and _agent_online(other)
        p["meeting"] = {k: m[k] for k in ("time", "place", "status", "room")} if m else None
        p["contact"] = contacts.get(other.get("id"))
    return found


@app.get("/api/people/{handle}")
async def person(handle: str, hw_session: str | None = Cookie(None)) -> dict:
    """One person's full badge, opened from anywhere they appear. A private
    badge shows only to people they already deal with: a shared room, a
    meeting, a room invite or a contact request."""
    a = _me(hw_session)
    other = hallway.store.attendee(handle=handle.lstrip("@").lower())
    if not other:
        raise HTTPException(404, "No one with that handle.")
    if not (other["listed"] or other["id"] == a["id"] or await hallway.knows(a, other)):
        raise HTTPException(404, "This badge is private.")
    p = public_profile(other)
    p["shared"] = sorted(
        {i.lower() for i in (a["profile"].get("interests") or [])}
        & {i.lower() for i in (other["profile"].get("interests") or [])}
    )
    p["you"] = other["id"] == a["id"]
    p["rooms_together"] = await hallway.rooms_together(a, other)
    return _where_you_stand(a, [p])[0]


class ContactReq(BaseModel):
    handle: str


@app.post("/api/contacts")
async def contact_request(body: ContactReq, hw_session: str | None = Cookie(None)) -> dict:
    return await hallway.request_contact(_me(hw_session), body.handle)


@app.get("/api/contacts")
async def contacts(hw_session: str | None = Cookie(None)) -> list[dict]:
    return hallway.contacts(_me(hw_session))


@app.get("/api/notices")
async def notices(hw_session: str | None = Cookie(None)) -> list[dict]:
    a = _me(hw_session)
    # Structured: the app renders from kind, actor, subject and payload. `text`
    # is the same thing as one line, shown as-is only for old unstructured rows.
    return hallway.store.notices(a["id"], "AND n.audience IN ('person','both') ORDER BY n.id DESC", limit=30)


@app.get("/api/privacy-log")
async def privacy_log(hw_session: str | None = Cookie(None)) -> list[dict]:
    return await hallway.privacy_log(_me(hw_session))


# ------------------------------------------------------------------ guard
# Switch waits GUARD_TIMEOUT_S for an answer, then delivers the message
# unchecked (it fails open, as it does on any non-2xx answer). A check slower
# than GUARD_SLOW_S most likely missed that deadline: it is counted and logged.
GUARD_TIMEOUT_S = 2.0
GUARD_SLOW_S = 1.0


class GuardStats:
    """Counters for /health: how often the guard could not give Switch a real answer."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.checks = 0
        self.errors = 0  # internal error: answered BLOCKED instead of a 500
        self.rate_limited = 0  # over RATE_LIMIT_GUARD: answered BLOCKED instead of a 429
        self.slow = 0  # took over GUARD_SLOW_S: Switch may have let it through unchecked
        self.bad_key = 0  # 401: if this is Switch, its Trust key is wrong and it fails open
        self.log_errors = 0  # the privacy log write failed (the verdict still went out)
        self.max_ms = 0.0
        self.recent_ms: list[float] = []

    def timed(self, ms: float) -> None:
        metrics.guard_latency.observe(ms / 1000)
        self.checks += 1
        self.max_ms = max(self.max_ms, ms)
        self.recent_ms.append(ms)
        del self.recent_ms[:-500]

    def as_dict(self) -> dict:
        recent = sorted(self.recent_ms)
        pct = (lambda q: round(recent[min(len(recent) - 1, int(q * len(recent)))], 2)) if recent else (lambda q: None)
        return {
            "checks": self.checks,
            "errors": self.errors,
            "rate_limited": self.rate_limited,
            "slow": self.slow,
            "bad_key": self.bad_key,
            "log_errors": self.log_errors,
            "p50_ms": pct(0.5),
            "p99_ms": pct(0.99),
            "max_ms": round(self.max_ms, 2),
            "slow_threshold_ms": GUARD_SLOW_S * 1000,
            "switch_timeout_ms": GUARD_TIMEOUT_S * 1000,
        }


guard_stats = GuardStats()


def _guard_messages(body: object) -> tuple[str, str | None]:
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        raise ValueError("no messages array")
    parts = []
    for m in messages:
        content = m.get("content", "") if isinstance(m, dict) else None
        if not isinstance(content, str):
            raise ValueError("a message has no text content")
        parts.append(content)
    role = messages[0].get("role") if messages else None
    return "\n".join(parts), role if isinstance(role, str) else None


@app.post("/guardrails/check")
async def guardrails_check(request: Request) -> dict:
    """Switch Trust's check for every message. Fails CLOSED wherever it can:
    whatever goes wrong in here is answered BLOCKED (with a 200), because a
    500, a 429 or a timeout would make Switch deliver the message unchecked."""
    started = time.perf_counter()
    # Unset only on a localhost dev box (check_keys refuses a public start without it).
    if settings.guard_api_key and not _key_matches(request.headers.get("x-flintai-api-key"), settings.guard_api_key):
        guard_stats.bad_key += 1
        log.warning(
            "guard: rejected a check with a bad key from %s (if that is Switch, every message "
            "goes through unchecked: fix its Trust key)",
            client_ip(request),
        )
        raise HTTPException(401, "bad guard key")
    try:
        # Generous (only Switch calls this), and never a 429: Switch would treat
        # that as fail-open, so over the limit the message is blocked instead.
        if limiter.hit("guard", client_ip(request)):
            guard_stats.rate_limited += 1
            metrics.guard_verdicts.inc(outcome="blocked_rate_limited")
            log.warning("guard: over RATE_LIMIT_GUARD, answering BLOCKED")
            return guard.blocked_response("rate_limited")
        content, role = _guard_messages(await request.json())
        outcome, findings = guard.scan(content, hallway.private_matcher())
        response = guard.wire_response(outcome, findings)
        metrics.guard_verdicts.inc(outcome=outcome)
    except Exception:
        guard_stats.errors += 1
        metrics.guard_verdicts.inc(outcome="blocked_error")
        log.exception("guard: check failed, answering BLOCKED")
        return guard.blocked_response("guard_error")
    finally:
        ms = (time.perf_counter() - started) * 1000
        guard_stats.timed(ms)
        if ms > GUARD_SLOW_S * 1000:
            guard_stats.slow += 1
            log.warning("guard: a check took %.0f ms; Switch delivers unchecked after %.0f s", ms, GUARD_TIMEOUT_S)
    if outcome != "ok":
        try:
            excerpt = guard.redact(content, findings) if outcome == "redacted" else content
            about = hallway.store.private_owners([f.text for f in findings]) if outcome == "redacted" else []
            hallway.store.insert(
                "guard_log",
                room_id=request.headers.get("x-agent-session-id"),
                role=role,
                outcome=outcome,
                categories=", ".join(sorted({f.category for f in findings})),
                excerpt=excerpt[:240],
                about="," + ",".join(about) + "," if about else None,
                created_at=time.time(),
            )
        except Exception:
            guard_stats.log_errors += 1
            log.exception("guard: could not write the privacy log (the verdict was still returned)")
    return response


@app.post("/api/admin/enable-guard")
async def enable_guard(request: Request) -> dict:
    """One-time event setup: point Switch's Trust guardrails at this Hallway
    instance and turn off agents' join greetings. Needs HALLWAY_ADMIN_KEY in
    `x-hallway-admin`; a localhost caller needs none only on a dev box."""
    if not (_is_admin(request) or _local_dev_caller(request)):
        raise HTTPException(403, "send the admin key in x-hallway-admin")
    endpoint = os.environ.get("HALLWAY_GUARD_ENDPOINT", settings.public_url)
    gw = GatewayClient()
    try:
        await gw.login()
        res = await gw.put_trust_settings(endpoint, "hallway-privacy", settings.guard_api_key)
        res["bridges_without_greetings"] = await gw.set_agent_greetings(False)
    finally:
        await gw.close()
    return res


# ---------------------------------------------------------------- organiser
# What scripts/hallway_admin.py calls. Like enable-guard and /metrics: the admin
# key in x-hallway-admin (which also exempts the request from the CSRF origin
# check and the per-session write limit), or a loopback caller on a dev box.
def _require_admin(request: Request) -> None:
    if not (_is_admin(request) or _local_dev_caller(request)):
        raise HTTPException(403, "send the admin key in x-hallway-admin")


def _admin_attendee(handle: str) -> dict:
    a = hallway.store.attendee(handle=handle.strip().lstrip("@").lower())
    if not a:
        raise HTTPException(404, f"No attendee with the handle '{handle}'.")
    return a


def _agent_status(a: dict) -> str:
    """active: seen in the last 15 minutes; idle: seen before that; never: hasn't checked in."""
    return "active" if _agent_active(a) else "idle" if a.get("agent_seen_at") else "never"


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""


def _admin_attendee_view(a: dict) -> dict:
    p = hallway.day_pass(a)
    return {
        "handle": a["handle"],
        "name": a["name"],
        "email": a["email"],
        "mode": a["mode"],
        "agent_name": a["agent_name"],
        "agent_status": _agent_status(a),
        "agent_seen_at": a["agent_seen_at"],
        "stand_in_connected": a["id"] in hallway.presence.connected,
        "privacy_check_passed": bool(a["privacy"].get("passed")),
        "persona": bool(a["is_persona"]),
        "pass": {"status": p["status"], "spent_usd": p["spent_usd"], "cap_usd": p["cap_usd"], "runs": p["runs"]}
        if p
        else None,
        "joined_at": a["created_at"],
    }


@app.get("/api/admin/attendees")
async def admin_attendees(request: Request) -> list[dict]:
    """Everyone who joined, oldest first, with their mode and their agent's status."""
    _require_admin(request)
    return [_admin_attendee_view(a) for a in hallway.store.attendees()]


@app.delete("/api/admin/attendees/{handle}")
async def admin_remove_attendee(handle: str, request: Request) -> dict:
    """Remove an attendee: their Switch stand-in and Mattermost user are cleaned
    up where possible, their rows deleted, and their invite spot freed."""
    _require_admin(request)
    a = _admin_attendee(handle)
    async with hallway._lock:  # not while a registration is counting spots
        cleanup = await hallway.remove_attendee(a)
    return {"removed": a["handle"], **cleanup, "spots_left": _spots_left()}


class AdminPass(BaseModel):
    paused: bool | None = None
    cap_usd: float | None = None


@app.post("/api/admin/attendees/{handle}/pass")
async def admin_pass(handle: str, body: AdminPass, request: Request) -> dict:
    """Pause or resume an attendee's day pass, and/or set its budget."""
    _require_admin(request)
    if body.paused is None and body.cap_usd is None:
        raise HallwayError("Send paused and/or cap_usd.")
    a = _admin_attendee(handle)
    p = hallway.admin_set_pass(a, paused=body.paused, cap_usd=body.cap_usd)
    return {"handle": a["handle"], "status": p["status"], "cap_usd": p["cap_usd"], "spent_usd": p["spent_usd"]}


def _invite_view() -> dict:
    code = _invite_code()
    return {
        "invite_only": bool(code),
        "invite_code": code or None,
        "invite_url": f"{settings.public_url}/?invite={code}" if code else None,
        "invite_limit": settings.invite_limit or None,
        "joined": _joined(),
        "spots_left": _spots_left(),
    }


@app.get("/api/admin/invite")
async def admin_invite(request: Request) -> dict:
    """The invite code and link, and how many spots are left."""
    _require_admin(request)
    return _invite_view()


class RotateInvite(BaseModel):
    code: str | None = None


@app.post("/api/admin/invite/rotate")
async def admin_rotate_invite(body: RotateInvite, request: Request) -> dict:
    """A new invite code (random, or the one sent). The old link stops working
    for new sign-ups; people who already joined are unaffected. Kept in the
    database, so it outlives a restart and overrides INVITE_CODE."""
    _require_admin(request)
    code = (body.code or "").strip() or secrets.token_urlsafe(9)
    if not (4 <= len(code) <= 64) or not all(c.isalnum() or c in "-_" for c in code):
        raise HallwayError("An invite code is 4-64 letters, digits, - or _.")
    hallway.store.kv_set(INVITE_CODE_KEY, code)
    log.info("invite code rotated by the organiser")
    return _invite_view()


def _csv(rows: list[list]) -> str:
    """CSV that a spreadsheet won't run: a cell starting with = + - @ is prefixed with '."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in rows:
        w.writerow([f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v for v in row])
    return buf.getvalue()


def _csv_response(body: str, name: str) -> Response:
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/admin/export/attendees.csv")
async def admin_export_attendees(request: Request) -> Response:
    """Attendees as CSV: public profile, email, mode, agent and day pass. No private contact details."""
    _require_admin(request)
    rows: list[list] = [
        [
            "handle",
            "name",
            "email",
            "role",
            "company",
            "mode",
            "agent_name",
            "agent_status",
            "agent_seen_at",
            "pass_status",
            "pass_spent_usd",
            "pass_cap_usd",
            "persona",
            "joined_at",
        ]
    ]
    for a in hallway.store.attendees():
        v = _admin_attendee_view(a)
        p = v["pass"] or {}
        rows.append(
            [
                a["handle"],
                a["name"],
                a["email"],
                a["profile"].get("role", ""),
                a["profile"].get("company", ""),
                a["mode"],
                a["agent_name"],
                v["agent_status"],
                _iso(a["agent_seen_at"]),
                p.get("status", ""),
                p.get("spent_usd", ""),
                p.get("cap_usd", ""),
                int(v["persona"]),
                _iso(a["created_at"]),
            ]
        )
    return _csv_response(_csv(rows), "hallway-attendees.csv")


@app.get("/api/admin/export/meetings.csv")
async def admin_export_meetings(request: Request) -> Response:
    """Every meeting proposed, with both sides, status and room."""
    _require_admin(request)
    sql = (
        "SELECT m.*, p.handle AS p_handle, p.name AS p_name, i.handle AS i_handle, i.name AS i_name, r.name AS room "
        "FROM meetings m JOIN attendees p ON p.id = m.proposer_id JOIN attendees i ON i.id = m.invitee_id "
        "LEFT JOIN rooms r ON r.id = m.room_id ORDER BY m.created_at"
    )
    rows: list[list] = [
        [
            "id",
            "proposer_handle",
            "proposer_name",
            "invitee_handle",
            "invitee_name",
            "time",
            "place",
            "why",
            "status",
            "room",
            "created_at",
        ]
    ]
    for m in hallway.store.all(sql):
        rows.append(
            [
                m["id"],
                m["p_handle"],
                m["p_name"],
                m["i_handle"],
                m["i_name"],
                m["time"],
                m["place"],
                m["why"],
                m["status"],
                m["room"] or "",
                _iso(m["created_at"]),
            ]
        )
    return _csv_response(_csv(rows), "hallway-meetings.csv")


class Announcement(BaseModel):
    text: str


@app.post("/api/admin/announce")
async def admin_announce(body: Announcement, request: Request) -> dict:
    """Post in the lobby as the host agent (Hallway's own voice in every room)."""
    _require_admin(request)
    return await hallway.announce(body.text)


# ----------------------------------------------------------- day pass worker
def _worker(request: Request) -> None:
    if not _key_matches(request.headers.get("x-hallway-worker"), config.worker_key(settings)):
        raise HTTPException(401, "bad worker key")


@app.get("/internal/daypass/due")
async def daypass_due(request: Request, every_s: int | None = None, busy: str = "", wait: float = 0) -> list[dict]:
    """Passes whose agent should run now, with what the worker needs to run them.
    `token` is a fresh MCP token for this run only (the attendee's own key is
    stored hashed): it expires within the hour and is revoked when the run reports."""
    _worker(request)
    out = []
    running = frozenset(x for x in busy.split(",") if x)  # the worker's agents mid-run
    period = settings.daypass_every_s if every_s is None else every_s
    deadline = time.time() + max(0.0, min(wait, EVENTS_WAIT_MAX_S))  # long-poll, like /agent/events
    while not (woken := await hallway.woken_passes(period, running)) and time.time() < deadline:
        await hallway.until_poked(deadline)
    for p in woken:
        out.append(
            {
                "attendee_id": p["attendee_id"],
                "name": p["name"],
                "handle": p["handle"],
                "mcp_url": _mcp_url(),
                "token": hallway.store.worker_token(p["attendee_id"]),
                "runs": p["runs"],
                "remaining_usd": _remaining_usd(p),
                "here_for": p["profile"].get("here_for", ""),
                "note": p["note"],
                "lobby": bool(hallway.store.need_attendee(id=p["attendee_id"])["lobby_agent"]),
                "privacy_passed": bool(hallway.store.need_attendee(id=p["attendee_id"])["privacy"].get("passed")),
                "events": p.get("events", []),  # what woke it, if anything did
            }
        )
    return out


class RunReport(BaseModel):
    engine: str
    cost_usd: float
    tool_calls: int = 0
    summary: str = ""
    ok: bool = True


@app.post("/internal/daypass/{attendee_id}/runs")
async def daypass_report(attendee_id: str, body: RunReport, request: Request) -> dict:
    _worker(request)
    p = hallway.record_run(attendee_id, body.engine, body.cost_usd, body.tool_calls, body.summary, body.ok)
    hallway.store.revoke_worker_tokens(attendee_id)
    return {"status": p["status"], "spent_usd": p["spent_usd"], "remaining_usd": _remaining_usd(p)}


# --------------------------------------------------------------- SPA + MCP
VERSIONED = ("app.js", "app.css", "theme.js")


@functools.cache
def _index_html() -> str:
    """index.html with ?v=<content hash> on the app's script and stylesheet. A
    CDN may keep /static/app.js for hours whatever Cache-Control says (Cloudflare
    did); a new URL per deploy means visitors get the new app at once."""
    html = (STATIC / "index.html").read_text()
    for name in VERSIONED:
        v = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
    return html


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(_index_html())


@app.get("/health")
async def health(request: Request) -> dict:
    """Liveness, for Docker and uptime monitors: public, touches nothing, says
    nothing. The guard's counters (a stream of bad_key would tell anyone the
    guard is being bypassed) only go to the organiser: admin key or a dev box."""
    if _metrics_caller(request):
        return {"ok": True, "guard": guard_stats.as_dict()}
    return {"ok": True}


READY_CACHE_S = 5.0  # /ready is public: at most one Switch probe per this many seconds
_ready_cache: tuple[float, dict[str, str]] = (0.0, {})


async def _readiness() -> dict[str, str]:
    global _ready_cache
    at, checks = _ready_cache
    if checks and time.monotonic() - at < READY_CACHE_S:
        return checks
    checks = {}
    try:
        hallway.store.db.execute("SELECT 1 FROM kv LIMIT 1").fetchall()
        checks["db"] = "ok"
    except sqlite3.Error as e:
        log.error("ready: database: %s", e)
        checks["db"] = "error"
    try:
        await asyncio.wait_for(hallway.switch.health(), timeout=4)
        checks["switch"] = "ok"
    except Exception as e:
        log.warning("ready: Switch: %r", e)
        checks["switch"] = "error"
    checks["started"] = "ok" if hallway.host and hallway.lobby else "error"
    _ready_cache = (time.monotonic(), checks)
    return checks


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: the DB answers, Switch answers, startup finished. 503 if not.
    Only ok/error per check; the reasons go to the log."""
    checks = await _readiness()
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)


def _metrics_caller(request: Request) -> bool:
    """The organiser (x-hallway-admin, or the same key as a bearer token, which
    is what Prometheus' `authorization` scrape setting sends), or a dev box."""
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    return _is_admin(request) or _key_matches(bearer, config.admin_key(settings)) or _local_dev_caller(request)


@app.get("/metrics")
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus text format. Needs the admin key (header or bearer); a
    localhost caller needs none only on a dev box."""
    if not _metrics_caller(request):
        raise HTTPException(403, "send the admin key in x-hallway-admin or as a bearer token")
    return PlainTextResponse(metrics.REGISTRY.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


# Read at scrape time. hallway.store is looked up on every scrape: tests swap it.
_STARTED = time.time()
GUARD_COUNTERS = {
    "checks": "Privacy guard checks answered.",
    "errors": "Guard checks that failed internally and answered BLOCKED.",
    "rate_limited": "Guard checks over RATE_LIMIT_GUARD, answered BLOCKED.",
    "slow": "Guard checks slower than 1 s: Switch may have delivered the message unchecked.",
    "bad_key": "Guard calls with a wrong key (if from Switch, every message goes through unchecked).",
    "log_errors": "Privacy log writes that failed (the verdict still went out).",
}


def _guard_counter(field: str) -> metrics.Collected:
    return metrics.REGISTRY.collected(
        f"hallway_guard_{field}_total", GUARD_COUNTERS[field], lambda: [({}, getattr(guard_stats, field))], "counter"
    )


for _field in GUARD_COUNTERS:
    _guard_counter(_field)


def _rows(sql: str) -> list[sqlite3.Row]:
    return hallway.store.db.execute(sql).fetchall()


metrics.REGISTRY.collected(
    "hallway_daypass_spend_usd_total",
    "Model spend reported by day pass runs, in USD.",
    lambda: [({}, _rows("SELECT COALESCE(SUM(spent_usd), 0) FROM passes")[0][0])],
    "counter",
)
metrics.REGISTRY.collected(
    "hallway_daypass_cap_usd",
    "Sum of all day passes' budgets, in USD.",
    lambda: [({}, _rows("SELECT COALESCE(SUM(cap_usd), 0) FROM passes")[0][0])],
)
metrics.REGISTRY.collected(
    "hallway_daypass_passes",
    "Day passes by status (active, paused, spent).",
    lambda: [({"status": r[0]}, r[1]) for r in _rows("SELECT status, COUNT(*) FROM passes GROUP BY status")],
)
metrics.REGISTRY.collected(
    "hallway_daypass_runs_total",
    "Day pass agent runs reported, by whether they succeeded.",
    lambda: [
        ({"ok": "true" if r[0] else "false"}, r[1]) for r in _rows("SELECT ok, COUNT(*) FROM pass_runs GROUP BY ok")
    ],
    "counter",
)
metrics.REGISTRY.collected(
    "hallway_presence_sockets",
    "Stand-in agents' WebSockets to Switch: connected (confirmed by Switch) or connecting.",
    lambda: [({"state": k}, v) for k, v in hallway.presence.counts().items()],
)
metrics.REGISTRY.collected(
    "hallway_attendees",
    "Attendees, by mode.",
    lambda: [({"mode": r[0]}, r[1]) for r in _rows("SELECT mode, COUNT(*) FROM attendees GROUP BY mode")],
)
metrics.REGISTRY.collected(
    "hallway_agents_active",
    "Attendees whose agent called Hallway in the last 15 minutes.",
    lambda: [({}, _rows(f"SELECT COUNT(*) FROM attendees WHERE agent_seen_at > {time.time() - AGENT_ACTIVE_S}")[0][0])],
)
metrics.REGISTRY.collected(
    "hallway_process_start_time_seconds", "When this Hallway process started (Unix time).", lambda: [({}, _STARTED)]
)


app.mount("/", mcp_app)  # serves /mcp; registered last so the routes above win
