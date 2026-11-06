"""Hallway web server: the attendee app (SPA + JSON API), the MCP endpoint at
/mcp, and the privacy guard Switch calls at /guardrails/check."""
import contextlib
import ipaddress
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import guard, mailer, mcp_server, security
from .ratelimit import RateLimited, limiter
from . import config
from .config import AGENDA, check_keys, check_signin, settings
from .core import AUTONOMY_KEYS, LOCKED_ASK, Hallway, HallwayError, public_profile
from .store import token_hash
from .switch import GatewayClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hallway")
logging.getLogger("httpx").setLevel(logging.WARNING)


class _RedactSecrets(logging.Filter):
    """Access logs print the full path: hide sign-in tokens (?t=) and MCP keys (?key=)."""
    PATTERN = re.compile(r"([?&](?:t|key)=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.PATTERN.sub(r"\1[redacted]", a) if isinstance(a, str) else a for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactSecrets())

STATIC = Path(__file__).parent / "static"
hallway = Hallway()
mcp_server.hallway = hallway
mcp_app = mcp_server.mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    check_signin(settings)
    for warning in check_keys(settings):
        log.warning(warning)
    if "DEV_MAGIC_LINKS" in os.environ and "SIGNIN_MODE" not in os.environ:
        log.warning("DEV_MAGIC_LINKS is deprecated: set SIGNIN_MODE=%s instead.", settings.signin_mode)
    log.info("sign-in mode: %s%s", settings.signin_mode,
             f" (email via {settings.email_sender})" if settings.signin_mode == "email" else "")
    await hallway.start()
    async with mcp_server.mcp.session_manager.run():
        yield
    await hallway.stop()


async def _session_write_limit(request: Request) -> None:
    """Cookie-authenticated writes, per session (every route; the organiser's
    admin key is exempt)."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if (cookie and request.method in security.UNSAFE_METHODS and request.url.path.startswith("/api/")
            and not _is_admin(request)):
        limiter.check("session_write", token_hash(cookie))


app = FastAPI(title="Hallway", lifespan=lifespan, dependencies=[Depends(_session_write_limit)])
# Security headers on every response (the SPA, the API, /mcp) and the Origin
# check on cookie-authenticated writes. Reads `settings` per request.
app.add_middleware(security.SecurityMiddleware, get_settings=lambda: settings)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(RateLimited)
async def _rate_limited(_req: Request, exc: RateLimited):
    return JSONResponse({"error": f"Too many requests. Wait {exc.retry_after} s and try again.",
                         "code": "rate_limited", "retry_after": exc.retry_after},
                        status_code=429, headers={"Retry-After": str(exc.retry_after)})


@app.exception_handler(HallwayError)
async def _hallway_error(_req: Request, exc: HallwayError):
    return JSONResponse({"error": str(exc)}, status_code=400)


# ------------------------------------------------------------------ session
# The hw_session cookie is a random token; the server keeps only its sha256:
# a row in `sessions` once the person has an account, or `pre:<hash>` -> email
# between opening the magic link and finishing onboarding.
SESSION_COOKIE = "hw_session"


def _lookup(hw_session: str | None) -> tuple[dict | None, str | None, dict | None]:
    """(attendee, pending_email, session row). Any may be None."""
    if not hw_session:
        return None, None, None
    s = hallway.store.session(hw_session)
    a = hallway.store.attendee(id=s["attendee_id"]) if s else None
    if a:
        return a, a["email"], s
    return None, hallway.store.kv_get(f"pre:{token_hash(hw_session)}"), None


def _session(hw_session: str | None) -> tuple[dict | None, str | None]:
    """(attendee, pending_email). Either may be None."""
    return _lookup(hw_session)[:2]


def _set_session_cookie(resp: Response, token: str, max_age: int) -> None:
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=max(0, int(max_age)),
                    secure=settings.public_url.startswith("https://"))


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


def _agent_active(a: dict) -> bool:
    return bool(a.get("agent_seen_at") and time.time() - a["agent_seen_at"] < AGENT_ACTIVE_S)


def _mcp_url() -> str:
    return f"{settings.public_url}/mcp"


def _remaining_usd(p: dict) -> float:
    """What's left of a pass's budget. Never negative: one run can overshoot the cap."""
    return max(0.0, round(p["cap_usd"] - p["spent_usd"], 6))


def _pass_view(a: dict) -> dict | None:
    p = hallway.day_pass(a)
    if not p:
        return None
    runs = hallway.store.all(
        "SELECT cost_usd, tool_calls, summary, ok, created_at FROM pass_runs WHERE attendee_id = ? ORDER BY id DESC LIMIT 8", a["id"])
    return {"cap_usd": p["cap_usd"], "spent_usd": p["spent_usd"], "remaining_usd": _remaining_usd(p),
            "runs": p["runs"], "status": p["status"],
            "last_run_at": p["last_run_at"], "every_s": settings.daypass_every_s, "recent": runs}


KEY_PLACEHOLDER = "<your-hallway-key>"


def _me_view(a: dict, token: str | None = None) -> dict:
    """`token` is the plain MCP key, passed only right after it was issued
    (registration or rotation): it is stored hashed and can't be shown again."""
    key = token or KEY_PLACEHOLDER
    return {
        **public_profile(a),
        "email": a["email"],
        "profile": a["profile"],
        "contact": a["contact"],
        "autonomy": a["autonomy"],
        "autonomy_labels": AUTONOMY_KEYS,
        "locked": sorted(LOCKED_ASK),
        "privacy": a["privacy"],
        "agent_seen_at": a["agent_seen_at"],
        "pass": _pass_view(a),
        "connect": {
            "mcp_url": _mcp_url(),
            "token": token,
            "token_hint": a["mcp_token_hint"],
            "claude_code": f'claude mcp add --transport http hallway {_mcp_url()} --header "Authorization: Bearer {key}"',
            "codex": (f'[mcp_servers.hallway]\nurl = "{_mcp_url()}"\n'
                      f'http_headers = {{ "Authorization" = "Bearer {key}" }}'),
            "url_with_key": f"{_mcp_url()}?key={key}",
            "kickoff": (f"You are my agent at {settings.event_name}. Use the hallway MCP tools: call whoami, do the privacy "
                        "check, then work the lobby for me. Check my_inbox every few minutes."),
        },
    }


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
    return (settings.hallway_env == "dev" and config.is_local_url(settings)
            and bool(request.client) and request.client.host in ("127.0.0.1", "::1", "localhost"))


def _joined() -> int:
    """People who joined as attendees. Seeded personas don't take a demo spot."""
    return hallway.store.one("SELECT COUNT(*) AS n FROM attendees WHERE is_persona = 0")["n"]


def _spots_left() -> int | None:
    return max(0, settings.invite_limit - _joined()) if settings.invite_limit > 0 else None


class JoinRefused(Exception):
    def __init__(self, code: str):
        self.code = code


INVITE_ONLY = "Hallway's live demo is invite only. Open the invite link you were sent."


def _demo_full_message() -> str:
    return f"This demo is full: all {settings.invite_limit} spots are taken."


def _check_invite(invite: str | None) -> None:
    if settings.invite_code and not secrets.compare_digest((invite or "").strip().encode(), settings.invite_code.encode()):
        raise JoinRefused("invite_only")


def _check_spots() -> None:
    if _spots_left() == 0:
        raise JoinRefused("demo_full")


@app.exception_handler(JoinRefused)
async def _join_refused(_req: Request, exc: JoinRefused):
    if exc.code == "invite_only":
        return JSONResponse({"error": INVITE_ONLY, "code": "invite_only"}, status_code=403)
    return JSONResponse({"error": _demo_full_message(), "code": "demo_full",
                         "demo_video_url": settings.demo_video_url or None}, status_code=409)


@app.get("/api/event")
async def event():
    people = hallway.store.attendees()
    return {"name": settings.event_name, "signin_mode": settings.signin_mode, "daypass_enabled": settings.daypass_enabled,
            "daypass": {"price": settings.daypass_price, "cap_usd": settings.daypass_cap_usd} if settings.daypass_enabled else None,
            "max_attendees": settings.max_attendees, "lobby": settings.lobby_room_name, "attendees": len(people),
            "agents_active": sum(_agent_active(a) for a in people), "agenda": AGENDA,
            "invite_only": bool(settings.invite_code), "invite_limit": settings.invite_limit or None,
            "spots_left": _spots_left(), "demo_video_url": settings.demo_video_url or None}


ALREADY_JOINED = "This email already joined. Open Hallway on the device you signed up with."


def _issue_link(email: str) -> str:
    """A fresh single-use sign-in link. Only the fact that one was issued is logged."""
    now = time.time()
    hallway.store.kv_purge("magic:", now - settings.magic_link_ttl_s)
    token = secrets.token_urlsafe(24)
    hallway.store.kv_set(f"magic:{token}", {"email": email, "at": now})
    log.info("sign-in link issued for %s (%s mode)", mailer.mask_email(email), settings.signin_mode)
    return f"{settings.public_url}/auth/magic?t={token}"


def _signin_email(link: str) -> tuple[str, str]:
    minutes = max(1, settings.magic_link_ttl_s // 60)
    return (f"Your sign-in link for {settings.event_name}",
            f"Open this link to sign in to Hallway at {settings.event_name}:\n\n{link}\n\n"
            f"It works once, for the next {minutes} minutes. If you didn't ask for it, ignore this email.")


def _refusal_email(code: str) -> tuple[str, str]:
    why = INVITE_ONLY if code == "invite_only" else _demo_full_message()
    video = f"\n\nWatch the demo instead: {settings.demo_video_url}" if settings.demo_video_url else ""
    return (f"About signing in to {settings.event_name}",
            f"Someone asked to join Hallway at {settings.event_name} with this email.\n\n{why}{video}\n\n"
            "If it wasn't you, ignore this email.")


@app.post("/api/auth/start")
async def auth_start(body: StartAuth, request: Request):
    email = body.email.strip().lower()
    admin = _is_admin(request)
    if not admin:
        limiter.check("auth_start_ip", client_ip(request))
    if "@" not in email:
        raise HallwayError("Enter a valid email.")
    if admin:
        # Organiser tooling (scripts/personas.py): a link for any email, in any mode.
        return {"sent": True, "dev_link": _issue_link(email)}
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
            subject, text = _signin_email(_issue_link(email))
        except JoinRefused as refused:
            subject, text = _refusal_email(refused.code)
        try:
            await mailer.make_sender(settings).send(email, subject, text)
        except mailer.SendError as e:
            log.error("sign-in email to %s failed: %s", mailer.mask_email(email), e)
            raise HTTPException(503, "We couldn't send the email just now. Try again in a minute.")
        return {"sent": True, "dev_link": None}
    if mode == "demo" and existing:
        # No email provider, so a link on screen for an existing account would
        # be a sign-in as somebody else. They're already signed in on their device.
        return {"sent": False, "dev_link": None, "already_joined": True, "message": ALREADY_JOINED}
    if not existing:
        _check_invite(body.invite)
        _check_spots()
    return {"sent": True, "dev_link": _issue_link(email)}


@app.get("/auth/magic")
async def auth_magic(t: str, request: Request):
    limiter.check("magic_ip", client_ip(request))
    rec = hallway.store.kv_pop(f"magic:{t}")  # single use: gone on first open
    if not rec or time.time() - rec["at"] > settings.magic_link_ttl_s:
        return RedirectResponse("/#/?expired=1")
    a = hallway.store.attendee(email=rec["email"])
    if a:  # every sign-in is a new session; other devices stay signed in
        session, target = hallway.store.create_session(a["id"], _ua(request)), "/#/home"
    else:
        session, target = secrets.token_urlsafe(32), "/#/onboard"
        hallway.store.kv_set(f"pre:{token_hash(session)}", rec["email"])
    resp = RedirectResponse(target, status_code=303)
    _set_session_cookie(resp, session, settings.session_ttl_s)
    return resp


@app.post("/api/auth/logout")
async def logout(hw_session: str | None = Cookie(None)):
    """Ends this session on the server too: a copied cookie stops working."""
    if hw_session:
        hallway.store.revoke_session(hw_session)
        hallway.store.kv_pop(f"pre:{token_hash(hw_session)}")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/auth/logout-all")
async def logout_all(hw_session: str | None = Cookie(None)):
    """Sign out everywhere: every session of this attendee, this one included."""
    a = _me(hw_session)
    n = hallway.store.revoke_sessions(a["id"])
    log.info("signed out everywhere: %s (%d sessions)", a["handle"], n)
    resp = JSONResponse({"ok": True, "sessions": n})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/me")
async def me(response: Response, hw_session: str | None = Cookie(None)):
    a, email, s = _lookup(hw_session)
    if a:
        # The cookie slides with the server-side expiry (the app loads /api/me on open).
        _set_session_cookie(response, hw_session, s["expires_at"] - time.time())
        return _me_view(a)
    if email:
        return {"onboarding": True, "email": email}
    raise HTTPException(401, "Sign in first.")


@app.post("/api/me/agent-key")
async def rotate_agent_key(hw_session: str | None = Cookie(None)):
    """A new MCP key, shown once in this response. The old one stops working now."""
    a = _me(hw_session)
    token = hallway.store.set_mcp_token(a["id"])
    log.info("agent key rotated: %s", a["handle"])
    return _me_view(hallway.store.attendee(id=a["id"]), token=token)


class Register(BaseModel):
    name: str
    profile: dict = {}
    contact: dict = {}
    autonomy: dict = {}
    mode: str = "connect"
    live_agent_name: str | None = None


@app.post("/api/register")
async def register(body: Register, request: Request, response: Response, hw_session: str | None = Cookie(None)):
    persona = _is_admin(request)
    if not persona:
        limiter.check("register_ip", client_ip(request))
    a, email = _session(hw_session)
    if a:
        return _me_view(a)
    if not email:
        raise HTTPException(401, "Open the sign-in link from your email first.")
    # Checked again here, one registration at a time when there is a limit:
    # links issued while spots were left must not let more than INVITE_LIMIT in.
    async with hallway._lock if settings.invite_limit > 0 else contextlib.nullcontext():
        if not persona and not hallway.store.attendee(email=email):
            _check_spots()
        a = await hallway.register(
            name=body.name, email=email, profile=body.profile, contact=body.contact, autonomy=body.autonomy,
            mode=body.mode, live_agent_name=body.live_agent_name, is_persona=persona,
        )
    # The onboarding token is retired; the new account gets a fresh session.
    hallway.store.kv_pop(f"pre:{token_hash(hw_session)}")
    _set_session_cookie(response, hallway.store.create_session(a["id"], _ua(request)), settings.session_ttl_s)
    return _me_view(hallway.store.attendee(id=a["id"]), token=a.get("mcp_token"))


class UpdateMe(BaseModel):
    profile: dict | None = None
    contact: dict | None = None
    autonomy: dict | None = None


@app.put("/api/me")
async def update_me(body: UpdateMe, hw_session: str | None = Cookie(None)):
    a = hallway.update_settings(_me(hw_session), profile=body.profile, contact=body.contact, autonomy=body.autonomy)
    return _me_view(a)


class PassState(BaseModel):
    paused: bool


@app.post("/api/me/pass")
async def pass_state(body: PassState, hw_session: str | None = Cookie(None)):
    a = _me(hw_session)
    hallway.set_pass_paused(a, body.paused)
    return _me_view(a)


# -------------------------------------------------------------------- rooms
@app.get("/api/rooms")
async def rooms(hw_session: str | None = Cookie(None)):
    a = _me(hw_session)
    out = []
    for room in hallway.store.all("SELECT * FROM rooms ORDER BY created_at"):
        members = await hallway.room_members(room)
        if a["mm_user_id"] in members:
            people = [x for x in hallway.store.attendees() if x["mm_user_id"] in members]
            meet = hallway.store.one("SELECT time, place FROM meetings WHERE room_id = ?", room["id"]) if room["kind"] == "meet" else None
            out.append({"name": room["name"], "title": room["title"], "kind": room["kind"], "meeting": meet,
                        "people": [{"name": x["name"], "handle": x["handle"]} for x in people]})
    return out


@app.get("/api/rooms/{name}/messages")
async def room_messages(name: str, since: int | None = None, hw_session: str | None = Cookie(None)):
    a = _me(hw_session)
    room = hallway.room_by_ref(name)
    return {"room": {"name": room["name"], "title": room["title"], "kind": room["kind"]},
            "messages": await hallway.messages_for_person(a, room, since)}


class PostBody(BaseModel):
    text: str
    confirm: bool = False


@app.post("/api/rooms/{name}/messages")
async def room_post(name: str, body: PostBody, hw_session: str | None = Cookie(None)):
    a = _me(hw_session)
    room = hallway.room_by_ref(name)
    outcome, findings = guard.scan(body.text)
    if outcome == "redacted" and not body.confirm:
        return {"warning": "This looks like private info (" + ", ".join(sorted({f.category for f in findings}))
                + "). Everyone in the room, people and agents, will see it. Send anyway?"}
    await hallway.person_post(a, room, body.text)
    return {"ok": True}


class NewRoom(BaseModel):
    title: str
    invite: list[str]


@app.post("/api/rooms")
async def new_room(body: NewRoom, hw_session: str | None = Cookie(None)):
    room = await hallway.create_room(_me(hw_session), body.title, body.invite)
    return {"name": room["name"], "title": room["title"]}


# ------------------------------------------------- approvals, meetings, people
@app.get("/api/approvals")
async def approvals(hw_session: str | None = Cookie(None)):
    a = _me(hw_session)
    return hallway.store.all(
        "SELECT id, kind, summary, status, created_at, decided_at FROM approvals WHERE attendee_id = ? "
        "ORDER BY status = 'pending' DESC, created_at DESC LIMIT 50", a["id"],
    )


class Decide(BaseModel):
    approve: bool


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: Decide, hw_session: str | None = Cookie(None)):
    return await hallway.decide(_me(hw_session), approval_id, body.approve)


@app.get("/api/meetings")
async def meetings(hw_session: str | None = Cookie(None)):
    return hallway.meetings(_me(hw_session))


class Respond(BaseModel):
    accept: bool


@app.post("/api/meetings/{meeting_id}")
async def respond(meeting_id: str, body: Respond, hw_session: str | None = Cookie(None)):
    return await hallway.respond_meeting(_me(hw_session), meeting_id, body.accept, by_person=True)


@app.get("/api/people")
async def people(q: str = "", hw_session: str | None = Cookie(None)):
    """Public profiles, plus what the app needs to show where you stand with each
    person: is their agent active, is a meeting in play, have you swapped contacts."""
    a = _me(hw_session)
    found = hallway.search_people(a, q, limit=100)
    by_handle = {x["handle"]: x for x in hallway.store.attendees()}
    meetings = {m["with"]: m for m in reversed(hallway.meetings(a)) if m["status"] != "declined"}
    contacts = {}
    for c in hallway.store.all("SELECT * FROM contacts WHERE requester_id = ? OR target_id = ?", a["id"], a["id"]):
        other = c["target_id"] if c["requester_id"] == a["id"] else c["requester_id"]
        contacts[other] = c["status"]
    for p in found:
        other = by_handle.get(p["handle"]) or {}
        m = meetings.get(p["handle"])
        p["agent_active"] = _agent_active(other)
        p["meeting"] = {k: m[k] for k in ("time", "place", "status", "room")} if m else None
        p["contact"] = contacts.get(other.get("id"))
    return found


class ContactReq(BaseModel):
    handle: str


@app.post("/api/contacts")
async def contact_request(body: ContactReq, hw_session: str | None = Cookie(None)):
    return await hallway.request_contact(_me(hw_session), body.handle)


@app.get("/api/contacts")
async def contacts(hw_session: str | None = Cookie(None)):
    return hallway.contacts(_me(hw_session))


@app.get("/api/notices")
async def notices(hw_session: str | None = Cookie(None)):
    a = _me(hw_session)
    return hallway.store.all(
        "SELECT id, kind, text, created_at FROM notices WHERE attendee_id = ? AND audience IN ('person','both') "
        "ORDER BY id DESC LIMIT 30", a["id"],
    )


@app.get("/api/privacy-log")
async def privacy_log(hw_session: str | None = Cookie(None)):
    _me(hw_session)
    return hallway.store.all("SELECT outcome, categories, excerpt, created_at FROM guard_log ORDER BY id DESC LIMIT 30")


# ------------------------------------------------------------------ guard
@app.post("/guardrails/check")
async def guardrails_check(request: Request):
    # Unset only on a localhost dev box (check_keys refuses a public start without it).
    if settings.guard_api_key and not _key_matches(request.headers.get("x-flintai-api-key"), settings.guard_api_key):
        raise HTTPException(401, "bad guard key")
    # Generous: only Switch calls this. Switch fails open on errors, so a 429
    # here lets a message through unchecked; keep RATE_LIMIT_GUARD high.
    limiter.check("guard", client_ip(request))
    body = await request.json()
    content = "\n".join(m.get("content", "") for m in body.get("messages", []))
    outcome, findings = guard.scan(content, hallway.private_values())
    if outcome != "ok":
        excerpt = guard.redact(content, findings) if outcome == "redacted" else content
        hallway.store.insert(
            "guard_log", room_id=request.headers.get("x-agent-session-id"), role=(body.get("messages") or [{}])[0].get("role"),
            outcome=outcome, categories=", ".join(sorted({f.category for f in findings})), excerpt=excerpt[:240],
            created_at=time.time(),
        )
    return guard.wire_response(outcome, findings)


@app.post("/api/admin/enable-guard")
async def enable_guard(request: Request):
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


# ----------------------------------------------------------- day pass worker
def _worker(request: Request) -> None:
    if not _key_matches(request.headers.get("x-hallway-worker"), config.worker_key(settings)):
        raise HTTPException(401, "bad worker key")


@app.get("/internal/daypass/due")
async def daypass_due(request: Request, every_s: int | None = None):
    """Passes whose agent should run now, with what the worker needs to run them.
    `token` is a fresh MCP token for this run only (the attendee's own key is
    stored hashed): it expires within the hour and is revoked when the run reports."""
    _worker(request)
    out = []
    for p in hallway.due_passes(settings.daypass_every_s if every_s is None else every_s):
        out.append({"attendee_id": p["attendee_id"], "name": p["name"], "handle": p["handle"],
                    "mcp_url": _mcp_url(), "token": hallway.store.worker_token(p["attendee_id"]), "runs": p["runs"],
                    "remaining_usd": _remaining_usd(p), "here_for": p["profile"].get("here_for", "")})
    return out


class RunReport(BaseModel):
    engine: str
    cost_usd: float
    tool_calls: int = 0
    summary: str = ""
    ok: bool = True


@app.post("/internal/daypass/{attendee_id}/runs")
async def daypass_report(attendee_id: str, body: RunReport, request: Request):
    _worker(request)
    p = hallway.record_run(attendee_id, body.engine, body.cost_usd, body.tool_calls, body.summary, body.ok)
    hallway.store.revoke_worker_tokens(attendee_id)
    return {"status": p["status"], "spent_usd": p["spent_usd"], "remaining_usd": _remaining_usd(p)}


# --------------------------------------------------------------- SPA + MCP
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


app.mount("/", mcp_app)  # serves /mcp; registered last so the routes above win
