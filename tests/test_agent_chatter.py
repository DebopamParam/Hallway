"""Two agents thanking and confirming each other forever: posts between the same
two agents in a room are debounced until a person speaks, and acknowledge lets an
agent end an exchange without posting."""

from .conftest import mcp_call

AUTO = {"rooms": "auto", "chat": "auto"}


async def test_agents_going_back_and_forth_are_held_until_a_person_posts(signup, client, hallway):
    ca, asha = await signup("Asha Rao", autonomy=AUTO)
    _, bo = await signup("Bo Chen", autonomy=AUTO)
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]
    room = (await mcp_call(client, ta, "create_room", {"title": "Payments chat", "invite": ["bo"]}))["room"]

    said = []
    for i in range(3):
        said.append(await mcp_call(client, ta, "post", {"room": room, "text": f"@bo.agent noted, 12:15 works ({i})"}))
        said.append(await mcp_call(client, tb, "post", {"room": room, "text": f"@asha.agent great, confirmed ({i})"}))
    assert [r["status"] for r in said] == ["posted"] * 4 + ["held", "held"]
    assert "acknowledge" in said[-1]["why"] and said[-1]["posted"] is False
    # A message that tags no other agent isn't part of the exchange.
    assert (await mcp_call(client, ta, "post", {"room": room, "text": "Notes for later."}))["status"] == "posted"

    # A person speaking opens it up again.
    assert (await ca.post(f"/api/rooms/{room}/messages", json={"text": "Bo, one more thing?"})).status_code == 200
    assert (await mcp_call(client, tb, "post", {"room": room, "text": "@asha.agent sure, go ahead"}))[
        "status"
    ] == "posted"


async def test_acknowledge_posts_nothing(signup, client, hallway):
    _, asha = await signup("Asha Rao")
    res = await mcp_call(client, asha["connect"]["token"], "acknowledge", {"about": "Bo's thanks"})
    assert res == {"status": "acknowledged", "posted": False, "about": "Bo's thanks"}
    steps = hallway.store.all("SELECT text FROM agent_chat WHERE role = 'step'")
    assert any("no reply needed" in s["text"] for s in steps)
