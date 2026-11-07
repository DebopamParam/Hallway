"""Sign-in (magic links + session cookie) and onboarding (/api/register)."""

import dataclasses
import time
from urllib.parse import urlparse

import httpx

from app import core, main

from .conftest import register, sign_in


async def test_magic_link_flow_to_registered(client, world):
    r = await client.post("/api/auth/start", json={"email": "  Asha@Example.COM "})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    link = urlparse(body["dev_link"])
    assert link.path == "/auth/magic"

    assert (await client.get("/api/me")).status_code == 401
    r = await client.get(f"{link.path}?{link.query}")
    assert r.status_code == 303
    assert r.headers["location"] == "/#/onboard"
    cookie = r.headers["set-cookie"].lower()
    assert "hw_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie

    me = (await client.get("/api/me")).json()
    assert me == {"onboarding": True, "email": "asha@example.com"}

    r = await client.post(
        "/api/register",
        json={"name": "Asha Rao", "profile": {"role": "Designer"}, "contact": {"phone": "+91 98111 22233"}},
    )
    assert r.status_code == 200
    me = r.json()
    assert me["handle"] == "asha" and me["agent"] == "asha.agent" and me["email"] == "asha@example.com"
    assert me["contact"] == {"phone": "+91 98111 22233"}
    assert me["connect"]["token"].startswith("hw_")
    assert me["connect"]["mcp_url"] == "http://testserver/mcp"
    assert me["pass"] is None

    # The MCP key is shown once: afterwards only its first characters.
    again = (await client.get("/api/me")).json()
    assert again["handle"] == "asha" and again["connect"]["token"] is None
    assert again["connect"]["token_hint"] == me["connect"]["token"][:7] + "…"
    assert me["connect"]["token"] not in again["connect"]["claude_code"]
    # Connect mode registers a stand-in agent in Switch and joins the lobby.
    assert ("register_agent", "asha.agent") in world.calls


async def test_returning_attendee_lands_on_home(make_client):
    first = make_client()
    await register(first, "Asha Rao", email="asha@example.com")
    second = make_client()
    r = await sign_in(second, "asha@example.com")
    assert r.headers["location"] == "/#/home"
    assert (await second.get("/api/me")).json()["handle"] == "asha"


async def test_auth_start_rejects_non_email(client):
    r = await client.post("/api/auth/start", json={"email": "not-an-email"})
    assert r.status_code == 400
    assert r.json() == {"error": "Enter a valid email."}


async def test_unknown_or_expired_magic_link(client, hallway):
    r = await client.get("/auth/magic?t=nope")
    assert r.status_code in (302, 307) and r.headers["location"] == "/#/?expired=1"
    hallway.store.kv_set("magic:old", {"email": "x@example.com", "at": time.time() - 15 * 60 - 1})
    r = await client.get("/auth/magic?t=old")
    assert r.headers["location"] == "/#/?expired=1"
    assert "set-cookie" not in r.headers


async def test_email_mode_hides_link(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, signin_mode="email"))
    r = await client.post("/api/auth/start", json={"email": "a@example.com"})
    assert r.json() == {"sent": True, "dev_link": None}


async def test_bogus_cookie_is_401(client):
    client.cookies.set("hw_session", "forged")
    assert (await client.get("/api/me")).status_code == 401
    assert (await client.get("/api/people")).status_code == 401


async def test_logout_clears_cookie(client):
    await register(client, "Asha Rao")
    r = await client.post("/api/auth/logout")
    assert r.json() == {"ok": True}
    assert "hw_session=" in r.headers["set-cookie"]
    assert (await client.get("/api/me")).status_code == 401


async def test_magic_link_single_use(make_client):
    """A sign-in link should stop working once used (it sits in mail, history, logs)."""
    a = make_client()
    r = await a.post("/api/auth/start", json={"email": "asha@example.com"})
    link = urlparse(r.json()["dev_link"])
    await a.get(f"{link.path}?{link.query}")
    await a.post("/api/register", json={"name": "Asha Rao"})
    b = make_client()
    r = await b.get(f"{link.path}?{link.query}")
    assert r.headers["location"] == "/#/?expired=1"
    assert "set-cookie" not in r.headers


async def test_logout_revokes_session(make_client):
    a = make_client()
    await register(a, "Asha Rao")
    stolen = a.cookies.get("hw_session")
    await a.post("/api/auth/logout")
    b = make_client()
    b.cookies.set("hw_session", stolen)
    assert (await b.get("/api/me")).status_code == 401


# ------------------------------------------------------------ registration
async def test_register_requires_magic_link(client):
    r = await client.post("/api/register", json={"name": "Asha Rao"})
    assert r.status_code == 401


async def test_register_requires_name(client):
    await sign_in(client, "a@example.com")
    r = await client.post("/api/register", json={"name": "   "})
    assert r.status_code == 400
    assert "required" in r.json()["error"]


async def test_register_rejects_unknown_mode(client):
    await sign_in(client, "a@example.com")
    r = await client.post("/api/register", json={"name": "Asha", "mode": "telepathy"})
    assert r.status_code == 400


async def test_register_live_needs_valid_agent_name(client, world):
    world.native_agent("asha.claude")
    await sign_in(client, "a@example.com")
    for bad in (None, "Steve Claude", "-x", "UPPER"):
        r = await client.post("/api/register", json={"name": "Asha", "mode": "live", "live_agent_name": bad})
        assert r.status_code == 400, bad
    r = await client.post("/api/register", json={"name": "Asha", "mode": "live", "live_agent_name": "asha.claude"})
    assert r.status_code == 200
    me = r.json()
    assert me["mode"] == "live" and me["agent"] == "asha.claude"
    # Live agents are the attendee's own: Hallway registers no stand-in for them.
    assert not [c for c in world.calls if c[0] == "register_agent" and c[1] == "asha.claude"]


async def live_signup(make_client, world, name: str, agent: str, email: str) -> tuple[httpx.AsyncClient, dict]:
    c = make_client()
    await sign_in(c, email)
    r = await c.post("/api/register", json={"name": name, "mode": "live", "live_agent_name": agent})
    return c, r.json() if r.status_code == 200 else {"status": r.status_code, **r.json()}


async def test_live_mode_refuses_an_agent_switch_does_not_know(make_client, world, hallway):
    """QA #2 part B: a made-up agent name used to register fine."""
    _, r = await live_signup(make_client, world, "Gus Ghost", "ghost987.claude", "gus@example.com")
    assert r["status"] == 400 and "no agent called" in r["error"]
    assert hallway.store.attendee(email="gus@example.com") is None


async def test_live_agent_is_not_bound_until_it_posts_its_code(make_client, signup, hallway, world):
    """QA #2: claiming someone else's Switch agent (here a native agent already
    in a private room) pulled the claimant into that agent's rooms on the next
    sync. Now the claim proves nothing until the agent posts a one-time code,
    which only its owner can make it do."""
    vc, _ = await signup("Vic Owner", autonomy={"rooms": "auto"})
    _, fr = await signup("Fran Friend")
    victim_agent = world.native_agent("vic.claude")
    room = (await vc.post("/api/rooms", json={"title": "Board secrets", "invite": [fr["handle"]]})).json()["name"]
    channel = await hallway.mm.channel_by_name(room)
    channel["members"].add(victim_agent["id"])  # the victim's own agent works in that room

    sq, me = await live_signup(make_client, world, "Sam Squatter", "vic.claude", "sam@example.com")
    assert me["agent_verified"] is False and me["live_proof"]["code"].startswith("hallway-verify-")
    proof = me["live_proof"]
    await hallway.sync_once()
    await hallway.sync_once()
    rooms = {r["name"] for r in (await sq.get("/api/rooms")).json()}
    assert room not in rooms and proof["room"] not in rooms
    assert (await sq.get(f"/api/rooms/{room}/messages")).status_code == 400
    # A post by someone else, or without the code, proves nothing.
    proof_channel = await hallway.mm.channel_by_name(proof["room"])
    world.add_post(proof_channel, victim_agent["id"], "hi, what's this room?")
    assert (await sq.post("/api/me/verify-agent")).json()["agent_verified"] is False


async def test_live_agent_that_posts_its_code_is_verified_and_paired(make_client, hallway, world):
    agent = world.native_agent("lee.claude")
    lc, me = await live_signup(make_client, world, "Lee Park", "lee.claude", "lee@example.com")
    proof = me["live_proof"]
    lobby = await hallway.mm.channel_by_name("hallway-lobby")
    assert agent["id"] not in lobby["members"]  # not invited anywhere yet
    world.add_post(await hallway.mm.channel_by_name(proof["room"]), agent["id"], f"my code: {proof['code']}")
    r = (await lc.post("/api/me/verify-agent")).json()
    assert r["agent_verified"] is True and r["live_proof"] is None
    await hallway.sync_once()
    assert agent["id"] in lobby["members"]
    assert any(n["kind"] == "agent_verified" for n in (await lc.get("/api/notices")).json())


async def test_live_mode_rejects_taken_agent_name(make_client, hallway, world):
    """Claiming another attendee's agent name should be a clean 400, not a crash."""
    await register(make_client(), "Steve Jobs")  # steve.agent
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
        base_url="http://testserver",
        headers={"origin": "http://testserver"},
    )
    try:
        await sign_in(c, "mallory@example.com")
        r = await c.post("/api/register", json={"name": "Mallory", "mode": "live", "live_agent_name": "steve.agent"})
    finally:
        await c.aclose()
    assert r.status_code == 400 and "taken" in r.json()["error"]
    # Refused before anything was created on Mattermost.
    assert ("mm_create_user", "mallory") not in world.calls


async def test_live_mode_cannot_claim_host_agent_name(client):
    await sign_in(client, "mallory@example.com")
    r = await client.post("/api/register", json={"name": "Mallory", "mode": "live", "live_agent_name": "hallway"})
    assert r.status_code == 400


async def test_live_mode_cannot_claim_a_handle(make_client):
    await register(make_client(), "Priya Shah")  # handle priya
    c = make_client()
    await sign_in(c, "mallory@example.com")
    for taken in ("priya", "mallory"):  # someone else's handle, or the one Mallory is about to get
        r = await c.post("/api/register", json={"name": "Mallory", "mode": "live", "live_agent_name": taken})
        assert r.status_code == 400, taken


async def test_handle_never_takes_the_host_agent_name(signup):
    _, me = await signup("Hallway Fan")
    assert me["handle"] == "hallway2" and me["agent"] == "hallway2.agent"


async def test_register_daypass(client):
    await sign_in(client, "a@example.com")
    r = await client.post("/api/register", json={"name": "Asha", "mode": "daypass"})
    assert r.status_code == 200
    p = r.json()["pass"]
    assert p["status"] == "active" and p["cap_usd"] == 0.5 and p["spent_usd"] == 0 and p["remaining_usd"] == 0.5


async def test_register_daypass_disabled(client, monkeypatch):
    monkeypatch.setattr(core, "settings", dataclasses.replace(core.settings, daypass_enabled=False))
    await sign_in(client, "a@example.com")
    r = await client.post("/api/register", json={"name": "Asha", "mode": "daypass"})
    assert r.status_code == 400 and "no day pass" in r.json()["error"]


async def test_register_event_full(make_client, monkeypatch):
    monkeypatch.setattr(core, "settings", dataclasses.replace(core.settings, max_attendees=1))
    await register(make_client(), "Asha Rao")
    c = make_client()
    await sign_in(c, "b@example.com")
    r = await c.post("/api/register", json={"name": "Bo"})
    assert r.status_code == 400 and "full" in r.json()["error"]


async def test_register_is_idempotent_per_session(client):
    me1 = await register(client, "Asha Rao")
    r = await client.post("/api/register", json={"name": "Someone Else"})
    assert r.json()["handle"] == me1["handle"] and r.json()["name"] == "Asha Rao"


async def test_handles_and_agent_names_are_unique(signup):
    _, a = await signup("Asha Rao")
    _, b = await signup("Asha Menon")
    assert (a["handle"], a["agent"]) == ("asha", "asha.agent")
    assert (b["handle"], b["agent"]) == ("asha2", "asha2.agent")


async def test_autonomy_defaults_and_share_contact_locked(client):
    me = await register(
        client,
        "Asha Rao",
        autonomy={"rooms": "auto", "share_contact": "auto", "chat": "bogus", "unknown": "auto"},
        rooms_open=False,
    )
    assert me["autonomy"] == {
        "chat": "auto",
        "rooms": "auto",
        "propose_meeting": "ask",
        "accept_meeting": "ask",
        "share_contact": "ask",
        "join_rooms": "ask",
    }
    assert me["locked"] == ["share_contact"]


async def test_update_me(client):
    await register(client, "Asha Rao", contact={"phone": "+91 98111 22233"})
    r = await client.put(
        "/api/me",
        json={
            "profile": {"company": "Juspay"},
            "contact": {"email": "a@juspay.in"},
            "autonomy": {"share_contact": "auto", "propose_meeting": "auto"},
        },
    )
    me = r.json()
    assert me["profile"]["company"] == "Juspay" and me["profile"]["role"] == "Engineer"  # merged
    assert me["contact"] == {"email": "a@juspay.in"}  # replaced
    assert me["autonomy"]["share_contact"] == "ask" and me["autonomy"]["propose_meeting"] == "auto"


async def test_event_endpoint(client, signup):
    await signup("Asha Rao")
    ev = (await client.get("/api/event")).json()
    assert ev["name"] == "Test Event" and ev["attendees"] == 1 and ev["agents_active"] == 0
    assert ev["daypass_enabled"] is True and ev["lobby"] == "hallway-lobby" and ev["agenda"]
