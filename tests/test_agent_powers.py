"""Anything the person can do in Hallway, their agent can do when asked: call off
a meeting, leave a room, change settings and the badge. Loosening needs the
person's word (a recent app message) or their approval."""

from .conftest import mcp_call

AUTO = {"rooms": "auto", "propose_meeting": "auto", "accept_meeting": "auto"}


async def test_agent_calls_off_a_meeting(signup, client, hallway):
    _, asha = await signup("Asha Rao", autonomy=AUTO)
    cb, bo = await signup("Bo Chen", autonomy=AUTO)
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]
    m = await mcp_call(
        client, ta, "propose_meeting", {"with_who": "bo", "time": "15:00", "place": "Atrium", "why": "agents"}
    )
    mid = m["meeting_id"]

    res = await mcp_call(client, tb, "cancel_meeting", {"meeting_id": mid})
    assert "respond_meeting" in res["error"]  # theirs to decline, not to cancel
    res = await mcp_call(client, ta, "cancel_meeting", {"meeting_id": mid})
    assert res["status"] == "cancelled"
    assert (await mcp_call(client, ta, "cancel_meeting", {"meeting_id": mid}))["status"] == "cancelled"
    notes = hallway.store.all(
        "SELECT kind FROM notices WHERE attendee_id = ?", hallway.store.need_attendee(handle="bo")["id"]
    )
    assert "meeting_cancelled" in [n["kind"] for n in notes]
    assert (await cb.get("/api/people?q=Asha")).json()[0]["meeting"] is None

    # A confirmed one: either side can call it off.
    mid = (
        await mcp_call(
            client, ta, "propose_meeting", {"with_who": "bo", "time": "16:00", "place": "Hall B", "why": "x"}
        )
    )["meeting_id"]
    assert (await cb.post(f"/api/meetings/{mid}", json={"accept": True})).json()["status"] == "confirmed"
    assert (await mcp_call(client, tb, "cancel_meeting", {"meeting_id": mid}))["status"] == "cancelled"
    _, carl = await signup("Carl Diaz")
    assert (
        "No meeting"
        in (await mcp_call(client, carl["connect"]["token"], "cancel_meeting", {"meeting_id": mid}))["error"]
    )


async def test_agent_leaves_a_room_and_stays_out(signup, client, hallway):
    _, asha = await signup("Asha Rao", autonomy=AUTO)
    _, bo = await signup("Bo Chen")
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]
    room = (await mcp_call(client, ta, "create_room", {"title": "Payments chat", "invite": ["bo"]}))["room"]
    assert "error" not in str(await mcp_call(client, tb, "read_room", {"room": room}))

    assert "lobby" in (await mcp_call(client, tb, "leave_room", {"room": "lobby"}))["error"]
    assert (await mcp_call(client, tb, "leave_room", {"room": room})) == {"status": "left", "room": room}
    await hallway.sync_once()  # doesn't bring them back
    assert "not in" in str(await mcp_call(client, tb, "read_room", {"room": room}))
    assert room not in [r["room"] for r in await mcp_call(client, tb, "list_rooms")]
    assert "error" not in str(await mcp_call(client, ta, "read_room", {"room": room}))  # the others stay


async def test_settings_on_the_persons_word_or_approval(signup, client, hallway):
    c, asha = await signup("Asha Rao", lobby=False)
    tok = asha["connect"]["token"]

    # Nobody asked: turning the lobby on becomes a card.
    res = await mcp_call(client, tok, "change_settings", {"lobby": True})
    assert res["status"] == "pending_approval"
    assert (await c.get("/api/me")).json()["lobby_agent"] is False
    assert (await c.post(f"/api/approvals/{res['approval_id']}", json={"approve": True})).status_code == 200
    assert (await c.get("/api/me")).json()["lobby_agent"] is True

    # Tightening always goes through.
    assert (await mcp_call(client, tok, "change_settings", {"public": False}))["status"] == "done"
    assert (await c.get("/api/me")).json()["listed"] is False

    # Just after they messaged it, loosening goes through too.
    assert (await c.post("/api/me/chat", json={"text": "go public, and start rooms without asking"})).status_code == 200
    res = await mcp_call(
        client, tok, "change_settings", {"public": True, "rules": {"rooms": "auto"}, "badge": {"role": "CTO"}}
    )
    assert res["status"] == "done", res
    me = (await c.get("/api/me")).json()
    assert me["listed"] is True and me["autonomy"]["rooms"] == "auto" and me["profile"]["role"] == "CTO"

    assert (
        "always asks" in (await mcp_call(client, tok, "change_settings", {"rules": {"share_contact": "auto"}}))["error"]
    )
    assert "Unknown rule" in (await mcp_call(client, tok, "change_settings", {"rules": {"pay": "auto"}}))["error"]
    assert "Not a badge field" in (await mcp_call(client, tok, "change_settings", {"badge": {"phone": "1"}}))["error"]
    assert (await mcp_call(client, tok, "change_settings", {"public": True}))["status"] == "unchanged"


async def test_day_pass_agent_proposes_meetings_only_when_asked(signup, client, hallway):
    pc, sana = await signup("Sana Qureshi", mode="daypass", autonomy=AUTO)
    await signup("Bo Chen")
    tok = sana["connect"]["token"]
    args = {"with_who": "bo", "time": "15:00", "place": "Atrium", "why": "payments"}
    res = await mcp_call(client, tok, "propose_meeting", args)
    assert "only when Sana asks" in res["error"]
    assert hallway.store.all("SELECT id FROM meetings") == []

    assert (await pc.post("/api/me/chat", json={"text": "Set up a meeting with Bo at 3"})).status_code == 200
    assert (await mcp_call(client, tok, "propose_meeting", args))["status"] == "proposed"
