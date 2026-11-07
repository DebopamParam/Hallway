"""Approvals, rooms, meetings and the contact swap (web API + MCP together)."""

from .conftest import mcp_call

ASHA_CONTACT = {"phone": "+91 98111 22233", "email": "asha.private@juspay.in"}
BO_CONTACT = {"phone": "+91 90000 11111", "linkedin": "linkedin.com/in/bo_secret_tg"}
SECRETS = ["98111", "22233", "asha.private", "90000", "11111", "bo_secret_tg"]


def assert_no_secrets(*blobs) -> None:
    flat = " ".join(str(b) for b in blobs)
    for s in SECRETS:
        assert s not in flat, f"private value {s!r} reached an agent"


async def pending(pc) -> list[dict]:
    return [a for a in (await pc.get("/api/approvals")).json() if a["status"] == "pending"]


async def test_contact_swap_needs_both_sides_and_never_reaches_agents(signup, client, world):
    ac, asha = await signup("Asha Rao", contact=ASHA_CONTACT)
    bc, bo = await signup("Bo Chen", contact=BO_CONTACT)
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]
    agent_saw = []

    res = await mcp_call(client, ta, "request_contact", {"with_who": "bo"})
    agent_saw.append(res)
    assert res["status"] == "pending_approval"
    assert (await ac.get("/api/contacts")).json() == []
    assert await pending(bc) == []  # Bo isn't asked until Asha confirms

    [ap] = await pending(ac)
    assert ap["kind"] == "share_contact"
    r = await ac.post(f"/api/approvals/{ap['id']}", json={"approve": True})
    assert r.json()["result"] == {"status": "waiting_for_other_side"}
    # One side approving is not enough.
    assert (await ac.get("/api/contacts")).json() == []
    assert (await bc.get("/api/contacts")).json() == []
    people = {p["handle"]: p for p in (await ac.get("/api/people")).json()}
    assert people["bo"]["contact"] == "pending"

    [bp] = await pending(bc)
    assert "Asha Rao wants to swap" in bp["summary"]
    r = await bc.post(f"/api/approvals/{bp['id']}", json={"approve": True})
    assert r.json()["result"] == {"status": "exchanged"}

    assert (await ac.get("/api/contacts")).json() == [{"name": "Bo Chen", "handle": "bo", **BO_CONTACT}]
    assert (await bc.get("/api/contacts")).json() == [{"name": "Asha Rao", "handle": "asha", **ASHA_CONTACT}]

    # Agents on both sides: whatever they can ask for, the details are absent.
    for tok in (ta, tb):
        agent_saw.append(await mcp_call(client, tok, "my_inbox"))
        agent_saw.append(await mcp_call(client, tok, "whoami"))
        agent_saw.append(await mcp_call(client, tok, "search_people"))
        agent_saw.append(await mcp_call(client, tok, "list_meetings"))
    agent_saw.append(await mcp_call(client, ta, "request_contact", {"with_who": "bo"}))
    assert agent_saw[-1]["status"] == "exchanged"
    inbox_kinds = [n["kind"] for n in agent_saw[1]["notices"]]
    assert "contact_exchanged" in inbox_kinds
    assert_no_secrets(agent_saw, world.calls, world.all_text_sent_by_agents())


async def test_contact_swap_declined_by_other_side(signup, client):
    ac, asha = await signup("Asha Rao", contact=ASHA_CONTACT)
    bc, _ = await signup("Bo Chen", contact=BO_CONTACT)
    await ac.post("/api/contacts", json={"handle": "bo"})  # person-initiated, same flow
    [ap] = await pending(ac)
    await ac.post(f"/api/approvals/{ap['id']}", json={"approve": True})
    [bp] = await pending(bc)
    r = await bc.post(f"/api/approvals/{bp['id']}", json={"approve": False})
    assert r.json()["status"] == "declined" and r.json()["result"] is None
    assert (await ac.get("/api/contacts")).json() == []
    assert (await bc.get("/api/contacts")).json() == []
    inbox = await mcp_call(client, asha["connect"]["token"], "my_inbox")
    assert_no_secrets(inbox)


async def test_requester_declining_never_asks_the_other_side(signup):
    ac, _ = await signup("Asha Rao")
    bc, _ = await signup("Bo Chen")
    await ac.post("/api/contacts", json={"handle": "bo"})
    [ap] = await pending(ac)
    await ac.post(f"/api/approvals/{ap['id']}", json={"approve": False})
    assert await pending(bc) == []


async def test_cannot_decide_someone_elses_approval(signup):
    ac, _ = await signup("Asha Rao")
    bc, _ = await signup("Bo Chen")
    await ac.post("/api/contacts", json={"handle": "bo"})
    [ap] = await pending(ac)
    r = await bc.post(f"/api/approvals/{ap['id']}", json={"approve": True})
    assert r.status_code == 400 and r.json() == {"error": "No such approval."}


async def test_deciding_twice_is_a_no_op(signup):
    ac, _ = await signup("Asha Rao")
    await signup("Bo Chen")
    await ac.post("/api/contacts", json={"handle": "bo"})
    [ap] = await pending(ac)
    first = (await ac.post(f"/api/approvals/{ap['id']}", json={"approve": False})).json()
    second = (await ac.post(f"/api/approvals/{ap['id']}", json={"approve": True})).json()
    assert first["status"] == second["status"] == "declined"


async def test_create_room_with_approval(signup, client, hallway, world):
    ac, asha = await signup("Asha Rao")  # rooms: ask (default)
    bc, _bo = await signup("Bo Chen")
    res = await mcp_call(client, asha["connect"]["token"], "create_room", {"title": "PQC pilots", "invite": ["bo"]})
    assert res["status"] == "pending_approval"
    assert [r["name"] for r in (await ac.get("/api/rooms")).json()] == ["hallway-lobby"]
    r = await ac.post(f"/api/approvals/{res['approval_id']}", json={"approve": True})
    assert r.json()["result"] == {"room": "pqc-pilots"}

    rooms = {r["name"]: r for r in (await bc.get("/api/rooms")).json()}
    assert {p["handle"] for p in rooms["pqc-pilots"]["people"]} == {"asha", "bo"}
    ch = world.channels[world.rooms[hallway.room_by_ref("pqc-pilots")["id"]]]
    assert {"u_asha", "u_bo", "u_asha.agent", "u_bo.agent", "u_hallway"} <= ch["members"]
    notices = (await bc.get("/api/notices")).json()
    assert any("started a room with you: PQC pilots" in n["text"] for n in notices)
    # The agent hears about the decision on its next inbox check.
    inbox = await mcp_call(client, asha["connect"]["token"], "my_inbox")
    assert any(n["kind"] == "approval" and n["approved"] for n in inbox["notices"])


async def test_room_names_do_not_collide(signup, client):
    _, asha = await signup("Asha Rao", autonomy={"rooms": "auto"})
    await signup("Bo Chen")
    tok = asha["connect"]["token"]
    a = await mcp_call(client, tok, "create_room", {"title": "PQC", "invite": ["bo"]})
    b = await mcp_call(client, tok, "create_room", {"title": "PQC", "invite": ["bo"]})
    assert (a["room"], b["room"]) == ("pqc", "pqc-2")


async def test_person_posts_in_room_with_guard_warning(signup, world):
    ac, _ = await signup("Asha Rao")
    r = await ac.post("/api/rooms/hallway-lobby/messages", json={"text": "call me on +91 98111 22233"})
    assert "warning" in r.json() and "contact" in r.json()["warning"]
    assert not [p for c in world.channels.values() for p in c["posts"] if "98111" in p["message"]]
    r = await ac.post("/api/rooms/hallway-lobby/messages", json={"text": "call me on +91 98111 22233", "confirm": True})
    assert r.json() == {"ok": True}
    r = await ac.post("/api/rooms/hallway-lobby/messages", json={"text": "Hello hallway"})
    assert r.json() == {"ok": True}
    msgs = (await ac.get("/api/rooms/hallway-lobby/messages")).json()["messages"]
    assert msgs[-1]["text"] == "Hello hallway" and msgs[-1]["mine"] is True


async def test_meeting_proposed_approved_and_confirmed(signup, client, hallway, world):
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, bo = await signup("Bo Chen")  # accept_meeting: ask
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]

    res = await mcp_call(
        client,
        ta,
        "propose_meeting",
        {"with_who": "@bo.agent", "time": "15:50", "place": "Hall B exit", "why": "PQC pilots"},
    )
    assert res["status"] == "proposed"
    mid = res["meeting_id"]

    inbox = await mcp_call(client, tb, "my_inbox")
    assert [m["id"] for m in inbox["pending_meetings_for_you"]] == [mid]

    res = await mcp_call(client, tb, "respond_meeting", {"meeting_id": mid, "accept": True})
    assert res["status"] == "pending_approval"
    [ap] = await pending(bc)
    assert ap["kind"] == "accept_meeting"
    r = await bc.post(f"/api/approvals/{ap['id']}", json={"approve": True})
    result = r.json()["result"]
    assert result["status"] == "confirmed" and result["room"] == "meet-asha-bo"

    for pc, other in ((ac, "bo"), (bc, "asha")):
        [m] = (await pc.get("/api/meetings")).json()
        assert m["status"] == "confirmed" and m["with"] == other and m["room"] == "meet-asha-bo"
    rooms = {r["name"]: r for r in (await ac.get("/api/rooms")).json()}
    assert rooms["meet-asha-bo"]["meeting"] == {"time": "15:50", "place": "Hall B exit"}
    card = [c for c in world.calls if c[0] == "post" and c[1][0] == "hallway" and "Meeting confirmed" in c[1][2]]
    assert len(card) == 1
    people = {p["handle"]: p for p in (await ac.get("/api/people")).json()}
    assert people["bo"]["meeting"]["status"] == "confirmed"


async def test_meeting_needs_proposers_approval_by_default(signup, client):
    ac, asha = await signup("Asha Rao")
    await signup("Bo Chen")
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "propose_meeting",
        {"with_who": "bo", "time": "12:00", "place": "Courtyard", "why": "lunch"},
    )
    assert res["status"] == "pending_approval"
    assert (await ac.get("/api/meetings")).json() == []


async def test_meeting_declined_by_person(signup, client):
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _bo = await signup("Bo Chen")
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "propose_meeting",
        {"with_who": "bo", "time": "12:00", "place": "Courtyard", "why": "lunch"},
    )
    r = await bc.post(f"/api/meetings/{res['meeting_id']}", json={"accept": False})
    assert r.json() == {"status": "declined", "meeting_id": res["meeting_id"]}
    assert any(n["text"] == "Bo Chen can't make 12:00." for n in (await ac.get("/api/notices")).json())
    # Only the invitee can answer.
    r = await ac.post(f"/api/meetings/{res['meeting_id']}", json={"accept": True})
    assert r.status_code == 400


async def test_cannot_meet_yourself_or_nobody(signup, client):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    tok = asha["connect"]["token"]
    res = await mcp_call(client, tok, "propose_meeting", {"with_who": "asha", "time": "1", "place": "x", "why": "y"})
    assert res == {"error": "You can't meet yourself."}
    res = await mcp_call(client, tok, "propose_meeting", {"with_who": "ghost", "time": "1", "place": "x", "why": "y"})
    assert "No attendee called 'ghost'" in res["error"]


async def test_sent_anyway_is_struck_out_for_everyone_else(signup, world):
    """QA #8: 'Send anyway' told the sender others see it struck out, but
    people read Mattermost's unredacted copy. Only the sender sees it in full,
    and only the sender sees Switch's note that it was redacted."""
    ac, _ = await signup("Asha Rao")
    bc, _ = await signup("Bo Chen")
    r = await ac.post("/api/rooms/hallway-lobby/messages", json={"text": "call me on +91 98111 22233"})
    warning = r.json()["warning"]
    assert "struck out" in warning and "will see it." not in warning
    await ac.post("/api/rooms/hallway-lobby/messages", json={"text": "call me on +91 98111 22233", "confirm": True})
    ch = next(c for c in world.channels.values() if any("98111" in p["message"] for p in c["posts"]))
    mine = next(p for p in ch["posts"] if "98111" in p["message"])
    admin = world.add_user("switch-admin", is_bot=True, first_name="Switch")
    note = "⚠️ Part of your message (contact) was redacted by Switch Trust before it was delivered."
    world.add_post(ch, admin["id"], note, mine["id"])["create_at"] = mine["create_at"] + 1

    theirs = (await bc.get("/api/rooms/hallway-lobby/messages")).json()["messages"]
    assert not any("98111" in m["text"] for m in theirs)
    assert any(m["text"] == "call me on [redacted]" for m in theirs)
    assert not any("Part of your message" in m["text"] for m in theirs)
    own = (await ac.get("/api/rooms/hallway-lobby/messages")).json()["messages"]
    assert any("98111" in m["text"] and m["mine"] for m in own)
    assert any("Part of your message" in m["text"] for m in own)
    # Polling with `since`: the root post is outside the batch, still resolved.
    since = mine["create_at"]
    own = (await ac.get(f"/api/rooms/hallway-lobby/messages?since={since}")).json()["messages"]
    assert [m["text"] for m in own] == [note]


async def test_asking_twice_for_the_same_thing_makes_one_card(signup, client):
    c, me = await signup("Asha Rao")
    await signup("Bo Chen")
    tok = me["connect"]["token"]
    args = {"with_who": "bo", "time": "12:45", "place": "Hall B", "why": "Both on payments"}
    first = await mcp_call(client, tok, "propose_meeting", args)
    again = await mcp_call(client, tok, "propose_meeting", args)
    assert first["status"] == again["status"] == "pending_approval"
    assert again["approval_id"] == first["approval_id"] and "Already asked" in again["message"]
    assert len(await pending(c)) == 1
    other = await mcp_call(client, tok, "propose_meeting", {**args, "time": "15:00"})
    assert other["approval_id"] != first["approval_id"] and len(await pending(c)) == 2


async def test_chat_steps_say_when_an_action_is_waiting_or_refused(signup, client, hallway):
    _, asha = await signup("Asha Rao", autonomy={"rooms": "ask"})
    await signup("Bo Chen")
    ta = asha["connect"]["token"]

    assert (await mcp_call(client, ta, "create_room", {"title": "Payments", "invite": ["bo"]}))[
        "status"
    ] == "pending_approval"
    meet = {"with_who": "asha", "time": "13:00", "place": "Lunch hall", "why": "Catch up"}
    assert "error" in await mcp_call(client, ta, "propose_meeting", meet)
    await mcp_call(client, ta, "my_inbox")

    steps = [s["text"] for s in hallway.store.all("SELECT text FROM agent_chat WHERE role = 'step' ORDER BY rowid")]
    assert steps[0] == "Opening a room with @bo"
    assert steps[1] == "↳ waiting for your OK on Now"
    assert steps[2:4] == ["Proposing a meeting with @asha at 13:00", "↳ didn't go through: You can't meet yourself."]
    assert steps[4:] == ["Checking what's new"]  # a call that simply worked gets no follow-up


async def test_a_refused_step_reads_to_the_person_not_the_agent(signup, client, hallway):
    _, asha = await signup("Asha Rao", lobby=False)
    ta = asha["connect"]["token"]
    assert "error" in await mcp_call(client, ta, "search_people", {"query": "payments"})
    assert "error" in await mcp_call(client, ta, "read_room", {"room": "no-such-room"})

    steps = [s["text"] for s in hallway.store.all("SELECT text FROM agent_chat WHERE role = 'step' ORDER BY rowid")]
    assert steps[1] == "↳ didn't go through: your agent isn't in the lobby (Lobby: Let my agent chat here)"
    assert steps[3].startswith("↳ didn't go through: ") and ". " not in steps[3]
