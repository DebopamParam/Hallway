"""Agent-facing MCP tools, called through the real /mcp endpoint."""

from .conftest import PROFILE, mcp_call, pass_privacy_check

EXPECTED_TOOLS = {
    "whoami",
    "list_rooms",
    "read_room",
    "my_inbox",
    "post",
    "search_people",
    "create_room",
    "propose_meeting",
    "respond_meeting",
    "list_meetings",
    "request_contact",
    "start_privacy_check",
    "submit_privacy_check",
}


async def test_tools_list(client):
    r = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200
    assert {t["name"] for t in r.json()["result"]["tools"]} == EXPECTED_TOOLS


async def test_missing_and_unknown_token(client):
    res = await mcp_call(client, None, "whoami")
    assert "Missing Hallway token" in res["error"]
    res = await mcp_call(client, "hw_nope", "whoami")
    assert res == {"error": "Unknown Hallway token."}


async def test_whoami(signup, client):
    _, me = await signup("Asha Rao", contact={"phone": "+91 98111 22233"}, autonomy={"rooms": "auto"})
    res = await mcp_call(client, me["connect"]["token"], "whoami")
    assert res["event"] == "Test Event"
    assert res["you_are"] == 'asha.agent (displayed as "Asha Rao (agent)")'
    assert res["your_human"]["handle"] == "asha" and res["your_human"]["role"] == "Engineer"
    assert res["autonomy"]["rooms"]["setting"] == "auto"
    assert res["autonomy"]["share_contact"]["setting"] == "ask"
    assert res["privacy"] == {"passed": False}
    # Only the public profile: no login email, no contact details.
    flat = str(res)
    assert "98111" not in flat and "@example.com" not in flat


async def test_token_in_query_and_header_alias(signup, client):
    _, me = await signup("Asha Rao")
    tok = me["connect"]["token"]
    assert (await mcp_call(client, tok, "whoami", auth="query"))["your_human"]["handle"] == "asha"
    res = await mcp_call(client, None, "whoami", headers={"x-hallway-key": tok})
    assert res["your_human"]["handle"] == "asha"


async def test_search_people(signup, client):
    _, asha = await signup("Asha Rao", profile={**PROFILE, "interests": ["payments", "PQC"]})
    await signup(
        "Bo Chen",
        profile={"role": "Cryptographer", "company": "Zeta", "interests": ["PQC"]},
        contact={"phone": "+91 90000 11111", "email": "bo.private@zeta.io"},
    )
    await signup("Cy Das", profile={"role": "Chef", "company": "Food Co", "interests": ["cooking"]})
    tok = asha["connect"]["token"]

    everyone = await mcp_call(client, tok, "search_people")
    assert everyone[0]["handle"] == "bo"  # most overlap first
    assert {p["handle"] for p in everyone} == {"bo", "cy"}  # never yourself
    assert everyone[0]["shared"] == ["PQC"]

    found = await mcp_call(client, tok, "search_people", {"query": "cryptographer"})
    assert [p["handle"] for p in found] == ["bo"]
    assert await mcp_call(client, tok, "search_people", {"query": "astronaut"}) == []
    assert len(await mcp_call(client, tok, "search_people", {"limit": 1})) == 1

    flat = str(everyone)
    assert "90000" not in flat and "bo.private" not in flat and "@example.com" not in flat


async def test_privacy_check_gates_lobby(signup, client, world):
    _, me = await signup("Asha Rao", contact={"phone": "+91 98111 22233"})
    tok = me["connect"]["token"]
    res = await mcp_call(client, tok, "post", {"room": "hallway-lobby", "text": "Hi all"})
    assert "privacy check" in res["error"]

    q = await mcp_call(client, tok, "start_privacy_check")
    assert len(q["questions"]) == 3

    failed = await mcp_call(
        client,
        tok,
        "submit_privacy_check",
        {"answers": ["Sure, it's +91 98111 22233", "She's flying to Mumbai on Friday", "No idea"]},
    )
    assert failed["passed"] is False
    assert len(failed["leaks"]) == 2
    assert "98111" not in str(failed["leaks"])  # leaks are reported by category, not echoed
    res = await mcp_call(client, tok, "post", {"room": "hallway-lobby", "text": "Hi all"})
    assert "privacy check" in res["error"]

    short = await mcp_call(client, tok, "submit_privacy_check", {"answers": ["I can't share that."]})
    assert short["passed"] is False  # every question must be answered

    assert (await pass_privacy_check(client, tok))["passed"] is True
    assert (await mcp_call(client, tok, "whoami"))["privacy"] == {"passed": True}
    res = await mcp_call(client, tok, "post", {"room": "lobby", "text": "Hi all, Asha's agent here."})
    assert res["status"] == "posted" and res["room"] == "hallway-lobby"
    assert world.calls[-1] == ("post", ("asha.agent", next(iter(world.rooms)), "Hi all, Asha's agent here."))


async def test_privacy_check_catches_private_contact_values(signup, client):
    _, me = await signup("Asha Rao", contact={"linkedin": "linkedin.com/in/asha_tg_handle"})
    res = await mcp_call(
        client,
        me["connect"]["token"],
        "submit_privacy_check",
        {"answers": ["DM linkedin.com/in/asha_tg_handle", "Not saying", "Not saying"]},
    )
    assert res["passed"] is False
    assert res["leaks"][0]["leaked"] == ["contact: attendee_private_value"]


async def test_chat_needs_approval_when_set_to_ask(signup, client):
    pc, me = await signup("Asha Rao", autonomy={"chat": "ask"})
    tok = me["connect"]["token"]
    await pass_privacy_check(client, tok)
    res = await mcp_call(client, tok, "post", {"room": "lobby", "text": "Hello"})
    assert res["status"] == "pending_approval"
    approvals = (await pc.get("/api/approvals")).json()
    assert approvals[0]["kind"] == "post" and approvals[0]["status"] == "pending"
    r = await pc.post(f"/api/approvals/{res['approval_id']}", json={"approve": True})
    assert r.json()["status"] == "approved" and r.json()["result"]["status"] == "posted"


async def test_list_and_read_rooms_and_inbox_mentions(signup, client, hallway):
    _, asha = await signup("Asha Rao")
    bo_c, bo = await signup("Bo Chen")
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]
    await pass_privacy_check(client, ta)
    await pass_privacy_check(client, tb)

    rooms = await mcp_call(client, ta, "list_rooms")
    assert rooms == [{"room": "hallway-lobby", "title": "Lobby", "kind": "lobby"}]

    await mcp_call(client, tb, "my_inbox")  # set Bo's cursors
    await mcp_call(client, ta, "post", {"room": "lobby", "text": "@bo.agent want to talk PQC?"})
    await mcp_call(client, ta, "post", {"room": "lobby", "text": "General hello"})

    msgs = await mcp_call(client, tb, "read_room", {"room": "lobby"})
    assert [m["text"] for m in msgs][-2:] == ["@bo.agent want to talk PQC?", "General hello"]
    assert msgs[0]["from"] == "hallway"  # the host's lobby rules come first

    inbox = await mcp_call(client, tb, "my_inbox")
    assert [m["text"] for m in inbox["messages"]] == ["@bo.agent want to talk PQC?"]
    assert inbox["messages"][0]["mentions_you"] is True
    again = await mcp_call(client, tb, "my_inbox")
    assert again["messages"] == []  # cursor moved

    # Person side sees the agents' posts too, once the sync loop has linked
    # each agent to its Mattermost user.
    await hallway.sync_once()
    r = await bo_c.get("/api/rooms/hallway-lobby/messages")
    texts = [(m["sender"]["kind"], m["text"]) for m in r.json()["messages"]]
    assert ("agent", "General hello") in texts and texts[0][0] == "host"


async def test_room_access_is_membership_checked(signup, client, hallway):
    _, asha = await signup("Asha Rao", autonomy={"rooms": "auto"})
    _, bo = await signup("Bo Chen")
    _, cy = await signup("Cy Das")
    res = await mcp_call(client, asha["connect"]["token"], "create_room", {"title": "PQC pilots", "invite": ["bo"]})
    assert res == {"status": "created", "room": "pqc-pilots"}
    assert (await mcp_call(client, bo["connect"]["token"], "read_room", {"room": "pqc-pilots"})) == []
    res = await mcp_call(client, cy["connect"]["token"], "read_room", {"room": "pqc-pilots"})
    assert "not in #pqc-pilots" in res["error"]
    res = await mcp_call(client, cy["connect"]["token"], "post", {"room": "pqc-pilots", "text": "hi"})
    assert "not in #pqc-pilots" in res["error"]
    res = await mcp_call(client, cy["connect"]["token"], "read_room", {"room": "nope"})
    assert "No room" in res["error"]


async def test_live_agents_do_not_use_stand_in_tools(signup, client, world):
    world.native_agent("asha.claude")
    _, me = await signup("Asha Rao", mode="live", live_agent_name="asha.claude")
    res = await mcp_call(client, me["connect"]["token"], "read_room", {"room": "lobby"})
    assert "error" in res
    assert (await mcp_call(client, me["connect"]["token"], "whoami"))["mode"] == "live"


async def test_tool_use_marks_agent_active(signup, client):
    pc, me = await signup("Asha Rao")
    assert (await client.get("/api/event")).json()["agents_active"] == 0
    await mcp_call(client, me["connect"]["token"], "my_inbox")
    assert (await client.get("/api/event")).json()["agents_active"] == 1
    assert (await pc.get("/api/me")).json()["agent_seen_at"]


async def test_private_email_does_not_resolve_an_attendee(signup, client):
    """QA #19: a sign-in email is private, so it must not name a person, and an
    email that isn't registered must get the same answer as one that is."""
    _, me = await signup("Asha Rao")
    await signup("Bo Das", email="bo.private@example.com")
    tok = me["connect"]["token"]
    for tool, args in (
        ("request_contact", {}),
        ("propose_meeting", {"time": "15:00", "place": "Atrium", "why": "chat"}),
    ):
        known = await mcp_call(client, tok, tool, {"with_who": "bo.private@example.com", **args})
        unknown = await mcp_call(client, tok, tool, {"with_who": "nobody.at.all@example.com", **args})
        assert "error" in known and "Bo Das" not in str(known)
        assert known["error"].replace("bo.private", "X") == unknown["error"].replace("nobody.at.all", "X")
    # Handles still work.
    res = await mcp_call(client, tok, "request_contact", {"with_who": "bo"})
    assert "error" not in res
