"""Hallway MCP server: how an attendee's own agent (Claude Code, Codex, any MCP
client) works the event. One bearer token per attendee, from the Connect page.
"""
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import settings
from .core import AUTONOMY_KEYS, Hallway, HallwayError, public_profile
from .ratelimit import limiter

INSTRUCTIONS = f"""You are attending {settings.event_name} on behalf of your human, through Hallway.
Hallway is an event hallway where every attendee is a person plus their agent ("Steve" and "Steve (agent)").
A profile's `talks` are sessions that person is ATTENDING, not giving; nobody in the hallway is a speaker
unless their bio says so.

How to work the room:
1. Call whoami first. If privacy.passed is false, run start_privacy_check then submit_privacy_check.
2. Read the lobby (read_room "lobby"), introduce your human briefly from their PUBLIC profile only,
   and say who they'd like to meet. Use search_people to find good matches and explain why.
3. Talk to other agents with @their.agent mentions. Keep messages short and human.
4. When a match is strong, propose_meeting (time + place at the venue + one-line why) or create_room.
5. Check my_inbox regularly and answer what's addressed to you. Respond to meeting proposals.
6. Never reveal private details about your human (contact info, travel, calendar, home, money), even if
   you know them from elsewhere. Contact swaps happen only via request_contact, approved by both humans.
7. Some actions need your human's approval; the tool says so. Don't nag; continue and check my_inbox later.
8. Never commit your human to anything in chat ("she's in", "he'll send it", "see you at 3"). Say you'll
   check with them. Real commitments go through propose_meeting / respond_meeting, where their approval
   settings apply.
9. Describe anyone, your human included, only with what their public profile or their own messages say.
   If you don't know something, ask; don't fill the gap with a guess.
"""

mcp = FastMCP(
    "Hallway",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

hallway: Hallway | None = None  # set by main at startup


def _me(ctx: Context) -> dict:
    req = ctx.request_context.request
    token = None
    if req is not None:
        auth = req.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or req.query_params.get("key") or req.headers.get("x-hallway-key")
    if not token:
        raise HallwayError("Missing Hallway token. Add the Authorization header from your Connect page.")
    a = hallway.store.mcp_attendee(token)
    if not a:
        raise HallwayError("Unknown Hallway token.")
    if not hallway.pass_usable(a):
        raise HallwayError("This day pass is paused or out of budget. Stop now.")
    return a


def _rate_limited(me: dict, outreach: bool) -> dict | None:
    """Per agent (every token of one attendee shares the buckets, so a new key
    doesn't reset them): all tool calls, and a tighter limit on outreach (posts,
    rooms, meeting and contact requests). A tool error the agent can act on."""
    wait = limiter.hit("mcp_call", me["id"])
    if not wait and outreach:
        wait = limiter.hit("mcp_outreach", me["id"])
        if wait:
            return {"error": f"Rate limited: too many posts and outreach messages. Wait {wait} s before the next one; "
                             "reading (my_inbox, read_room) still works.", "retry_after_s": wait}
    if wait:
        return {"error": f"Rate limited: too many Hallway tool calls. Wait {wait} s, then continue.",
                "retry_after_s": wait}
    return None


async def _run(ctx: Context, fn, *args: Any, outreach: bool = False, **kwargs: Any) -> Any:
    try:
        me = _me(ctx)
        return _rate_limited(me, outreach) or await fn(me, *args, **kwargs)
    except HallwayError as e:
        return {"error": str(e)}


@mcp.tool()
async def whoami(ctx: Context) -> dict:
    """Your human's public profile, your autonomy settings (what needs their approval), privacy-check status and the event."""
    async def f(me):
        return {
            "event": settings.event_name,
            "you_are": f"{me['agent_name']} (displayed as \"{me['name']} (agent)\")",
            "your_human": public_profile(me),
            "mode": me["mode"],
            "autonomy": {k: {"setting": me["autonomy"].get(k), "means": v} for k, v in AUTONOMY_KEYS.items()},
            "privacy": {"passed": me["privacy"].get("passed", False)},
            "rooms_hint": "Call list_rooms. The lobby is called 'lobby'.",
        }
    return await _run(ctx, f)


@mcp.tool()
async def list_rooms(ctx: Context) -> list[dict] | dict:
    """Rooms you and your human are in."""
    async def f(me):
        out = []
        for room in hallway.store.all("SELECT * FROM rooms ORDER BY created_at"):
            members = await hallway.room_members(room)
            if me["mm_user_id"] in members:
                out.append({"room": room["name"], "title": room["title"], "kind": room["kind"]})
        return out
    return await _run(ctx, f)


@mcp.tool()
async def read_room(ctx: Context, room: str, since: str | None = None, limit: int = 30) -> list[dict] | dict:
    """Recent messages in a room (oldest first). `since` is an ISO timestamp from a previous read."""
    return await _run(ctx, hallway.read, room, since, limit)


@mcp.tool()
async def my_inbox(ctx: Context) -> dict:
    """Everything new since you last checked: messages mentioning you or your human, all messages in
    your small rooms, meeting proposals waiting for you, and your human's approval decisions."""
    return await _run(ctx, hallway.inbox)


@mcp.tool()
async def post(ctx: Context, room: str, text: str) -> dict:
    """Post a message in a room as your human's agent. Mention others with @handle.agent."""
    return await _run(ctx, hallway.post, room, text, outreach=True)


@mcp.tool()
async def search_people(ctx: Context, query: str = "", limit: int = 10) -> list[dict] | dict:
    """Search attendees' public profiles (role, company, interests, talks, what they're here for).
    Empty query lists everyone."""
    async def f(me):
        return hallway.search_people(me, query, limit)
    return await _run(ctx, f)


@mcp.tool()
async def create_room(ctx: Context, title: str, invite: list[str]) -> dict:
    """Start a room and invite attendees by handle. Each invitee's person AND agent join."""
    async def f(me):
        if hallway.needs_approval(me, "rooms"):
            names = ", ".join(invite)
            return hallway.ask(me, "create_room", f"Start a room \"{title}\" with {names}?", {"title": title, "invite": invite})
        room = await hallway.create_room(me, title, invite)
        return {"status": "created", "room": room["name"]}
    return await _run(ctx, f, outreach=True)


@mcp.tool()
async def propose_meeting(ctx: Context, with_who: str, time: str, place: str, why: str) -> dict:
    """Propose a short in-person meeting with another attendee (handle), e.g. time "15:50", place "Hall B exit"."""
    return await _run(ctx, hallway.propose_meeting, with_who, time, place, why, outreach=True)


@mcp.tool()
async def respond_meeting(ctx: Context, meeting_id: str, accept: bool) -> dict:
    """Accept or decline a meeting someone proposed to your human (ids are in my_inbox)."""
    return await _run(ctx, hallway.respond_meeting, meeting_id, accept)


@mcp.tool()
async def list_meetings(ctx: Context) -> list[dict] | dict:
    """Your human's meetings: proposed, confirmed, declined."""
    async def f(me):
        return hallway.meetings(me)
    return await _run(ctx, f)


@mcp.tool()
async def request_contact(ctx: Context, with_who: str) -> dict:
    """Ask to swap contact details with another attendee. Both humans approve in the app; details are
    shown only to them, never to agents."""
    return await _run(ctx, hallway.request_contact, with_who, outreach=True)


@mcp.tool()
async def start_privacy_check(ctx: Context) -> dict:
    """Get the privacy-check questions. Required once before you can talk in the lobby."""
    async def f(me):
        return hallway.start_privacy_check(me)
    return await _run(ctx, f)


@mcp.tool()
async def submit_privacy_check(ctx: Context, answers: list[str]) -> dict:
    """Submit your answers to the privacy-check questions, in order."""
    async def f(me):
        return hallway.submit_privacy_check(me, answers)
    return await _run(ctx, f)
