"""Hallway web server: the attendee app (SPA + JSON API), the MCP endpoint at
/mcp, and the privacy guard Switch calls at /guardrails/check."""
import contextlib
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import guard, mcp_server
from .config import AGENDA, settings
from .core import AUTONOMY_KEYS, LOCKED_ASK, Hallway, HallwayError, public_profile
from .switch import GatewayClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hallway")
logging.getLogger("httpx").setLevel(logging.WARNING)
STATIC = Path(__file__).parent / "static"
hallway = Hallway()
mcp_server.hallway = hallway
mcp_app = mcp_server.mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    await hallway.start()
    async with mcp_server.mcp.session_manager.run():
        yield
    await hallway.stop()


app = FastAPI(title="Hallway", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HallwayError)
async def _hallway_error(_req: Request, exc: HallwayError):
    return JSONResponse({"error": str(exc)}, status_code=400)


# ------------------------------------------------------------------ session
def _session(hw_session: str | None) -> tuple[dict | None, str | None]:
    """(attendee, pending_email). Either may be None."""
    if not hw_session:
        return None, None
    a = hallway.store.attendee(session_token=hw_session)
    if a:
        return a, a["email"]
    return None, hallway.store.kv_get(f"pre:{hw_session}")


def _me(hw_session: str | None) -> dict:
    a, _ = _session(hw_session)
    if not a:
        raise HTTPException(401, "Sign in first.")
    return a


def _mcp_url() -> str:
    return f"{settings.public_url}/mcp"


def _pass_view(a: dict) -> dict | None:
    p = hallway.day_pass(a)
    if not p:
        return None
    runs = hallway.store.all(
        "SELECT cost_usd, tool_calls, summary, ok, created_at FROM pass_runs WHERE attendee_id = ? ORDER BY id DESC LIMIT 8", a["id"])
    return {"cap_usd": p["cap_usd"], "spent_usd": p["spent_usd"], "runs": p["runs"], "status": p["status"],
            "last_run_at": p["last_run_at"], "every_s": settings.daypass_every_s, "recent": runs}


def _me_view(a: dict) -> dict:
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
            "token": a["mcp_token"],
            "claude_code": f'claude mcp add --transport http hallway {_mcp_url()} --header "Authorization: Bearer {a["mcp_token"]}"',
            "codex": (f'[mcp_servers.hallway]\nurl = "{_mcp_url()}"\n'
                      f'http_headers = {{ "Authorization" = "Bearer {a["mcp_token"]}" }}'),
            "url_with_key": f"{_mcp_url()}?key={a['mcp_token']}",
            "kickoff": (f"You are my agent at {settings.event_name}. Use the hallway MCP tools: call whoami, do the privacy "
                        "check, then work the lobby for me. Check my_inbox every few minutes."),
        },
    }


class StartAuth(BaseModel):
    email: str


@app.get("/api/event")
async def event():
    return {"name": settings.event_name, "daypass_enabled": settings.daypass_enabled,
            "daypass": {"price": settings.daypass_price, "cap_usd": settings.daypass_cap_usd} if settings.daypass_enabled else None,
            "max_attendees": settings.max_attendees, "lobby": settings.lobby_room_name, "attendees": len(hallway.store.attendees()), "agenda": AGENDA}


@app.post("/api/auth/start")
async def auth_start(body: StartAuth):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HallwayError("Enter a valid email.")
    token = secrets.token_urlsafe(24)
    hallway.store.kv_set(f"magic:{token}", {"email": email, "at": time.time()})
    link = f"{settings.public_url}/auth/magic?t={token}"
    log.info("magic link for %s: %s", email, link)
    # Production would email this link (it arrives with the RSVP). Dev mode hands it back.
    return {"sent": True, "dev_link": link if settings.dev_magic_links else None}


@app.get("/auth/magic")
async def auth_magic(t: str):
    rec = hallway.store.kv_get(f"magic:{t}")
    if not rec or time.time() - rec["at"] > 3600:
        return RedirectResponse("/#/?expired=1")
    a = hallway.store.attendee(email=rec["email"])
    if a:
        session, target = a["session_token"], "/#/home"
    else:
        session, target = secrets.token_urlsafe(32), "/#/onboard"
        hallway.store.kv_set(f"pre:{session}", rec["email"])
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("hw_session", session, httponly=True, samesite="lax", max_age=7 * 86400)
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("hw_session")
    return resp


@app.get("/api/me")
async def me(hw_session: str | None = Cookie(None)):
    a, email = _session(hw_session)
    if a:
        return _me_view(a)
    if email:
        return {"onboarding": True, "email": email}
    raise HTTPException(401, "Sign in first.")


class Register(BaseModel):
    name: str
    profile: dict = {}
    contact: dict = {}
    autonomy: dict = {}
    mode: str = "connect"
    live_agent_name: str | None = None


@app.post("/api/register")
async def register(body: Register, hw_session: str | None = Cookie(None)):
    a, email = _session(hw_session)
    if a:
        return _me_view(a)
    if not email:
        raise HTTPException(401, "Open the sign-in link from your email first.")
    a = await hallway.register(
        name=body.name, email=email, profile=body.profile, contact=body.contact, autonomy=body.autonomy,
        mode=body.mode, live_agent_name=body.live_agent_name,
    )
    hallway.store.update("attendees", a["id"], session_token=hw_session)
    return _me_view(hallway.store.attendee(id=a["id"]))


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
            out.append({"name": room["name"], "title": room["title"], "kind": room["kind"],
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
    return hallway.search_people(_me(hw_session), q, limit=100)


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
    if settings.guard_api_key and request.headers.get("x-flintai-api-key") != settings.guard_api_key:
        raise HTTPException(401, "bad guard key")
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
    instance and turn off agents' join greetings. Localhost, or the guard key
    in `x-hallway-admin` (from outside a container)."""
    local = request.client and request.client.host in ("127.0.0.1", "::1", "localhost")
    keyed = settings.guard_api_key and request.headers.get("x-hallway-admin") == settings.guard_api_key
    if not (local or keyed):
        raise HTTPException(403, "local only, or send x-hallway-admin")
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
    key = settings.daypass_worker_key or settings.guard_api_key
    if not key or not secrets.compare_digest(request.headers.get("x-hallway-worker", ""), key):
        raise HTTPException(401, "bad worker key")


@app.get("/internal/daypass/due")
async def daypass_due(request: Request, every_s: int | None = None):
    """Passes whose agent should run now, with what the worker needs to run them."""
    _worker(request)
    out = []
    for p in hallway.due_passes(settings.daypass_every_s if every_s is None else every_s):
        out.append({"attendee_id": p["attendee_id"], "name": p["name"], "handle": p["handle"],
                    "mcp_url": _mcp_url(), "token": p["mcp_token"], "runs": p["runs"],
                    "remaining_usd": round(p["cap_usd"] - p["spent_usd"], 6), "here_for": p["profile"].get("here_for", "")})
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
    return {"status": p["status"], "spent_usd": p["spent_usd"], "remaining_usd": round(p["cap_usd"] - p["spent_usd"], 6)}


# --------------------------------------------------------------- SPA + MCP
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


app.mount("/", mcp_app)  # serves /mcp; registered last so the routes above win
