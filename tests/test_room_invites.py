"""Starting a room with someone is a request: they (and their agent) join once
they accept, unless they let rooms in without asking. Badges open from anywhere."""

from .conftest import mcp_call

AUTO = {"rooms": "auto"}


async def test_room_invite_waits_for_a_yes(signup, client, hallway):
    ca, asha = await signup("Asha Rao", autonomy=AUTO)
    cb, bo = await signup("Bo Chen", rooms_open=False)
    ta, tb = asha["connect"]["token"], bo["connect"]["token"]
    res = await mcp_call(client, ta, "create_room", {"title": "Payments chat", "invite": ["bo"]})
    room = res["room"]
    assert res["waiting_for"] == ["Bo Chen"]
    assert "not in" in str(await mcp_call(client, tb, "read_room", {"room": room}))  # not in yet
    rooms = {r["name"]: r for r in (await ca.get("/api/rooms")).json()}
    assert rooms[room]["waiting_for"] == [{"name": "Bo Chen", "handle": "bo"}]

    inv = (await cb.get("/api/invites")).json()
    assert [(i["room"], i["from"]) for i in inv] == [(room, "asha")]
    assert (await mcp_call(client, tb, "my_inbox"))["room_invites_for_you"][0]["id"] == inv[0]["id"]

    # Bo's agent can't take them in on its own: their rule is Ask. The invite
    # card already asks Bo, so asking (twice, even) adds no card of its own.
    for _ in range(2):
        res = await mcp_call(client, tb, "respond_invite", {"invite_id": inv[0]["id"], "accept": True})
        assert res["status"] == "pending_approval" and "invite card" in res["message"]
    assert (await cb.get("/api/approvals")).json() == []
    assert (await cb.get("/api/invites")).json() == inv
    assert (await cb.post(f"/api/invites/{inv[0]['id']}", json={"accept": True})).json()["status"] == "joined"
    assert "error" not in str(await mcp_call(client, tb, "read_room", {"room": room}))
    assert (await cb.get("/api/invites")).json() == []
    kinds = [
        n["kind"]
        for n in hallway.store.all(
            "SELECT kind FROM notices WHERE attendee_id = ?", hallway.store.need_attendee(handle="asha")["id"]
        )
    ]
    assert "room_joined" in kinds


async def test_room_invite_declined_and_open_rooms(signup, client, hallway):
    ca, _ = await signup("Asha Rao", autonomy=AUTO)
    cb, _ = await signup("Bo Chen", rooms_open=False)
    _, cy = await signup("Cy Park")  # lets rooms in without asking
    r = await ca.post("/api/rooms", json={"title": "Coffee", "invite": ["bo", "cy"]})
    assert r.json()["waiting_for"] == ["Bo Chen"]
    room = r.json()["name"]
    assert "error" not in str(await mcp_call(client, cy["connect"]["token"], "read_room", {"room": room}))
    inv = (await cb.get("/api/invites")).json()[0]
    assert (await cb.post(f"/api/invites/{inv['id']}", json={"accept": False})).json()["status"] == "declined"
    assert (await cb.post(f"/api/invites/{inv['id']}", json={"accept": True})).json()["status"] == "declined"
    rooms = {x["name"]: x for x in (await ca.get("/api/rooms")).json()}
    assert rooms[room]["waiting_for"] == []


async def test_meeting_rooms_need_no_invite(signup, client, hallway):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    cb, _ = await signup("Bo Chen", rooms_open=False)
    mid = (
        await mcp_call(
            client,
            asha["connect"]["token"],
            "propose_meeting",
            {"with_who": "bo", "time": "15:00", "place": "Atrium", "why": "x"},
        )
    )["meeting_id"]
    res = (await cb.post(f"/api/meetings/{mid}", json={"accept": True})).json()
    assert res["status"] == "confirmed"
    assert (await cb.get("/api/invites")).json() == []
    assert res["room"] in [x["name"] for x in (await cb.get("/api/rooms")).json()]


async def test_profile_opens_from_anywhere_but_private_stays_private(signup, client):
    ca, _ = await signup(
        "Asha Rao",
        autonomy=AUTO,
        profile={
            "role": "PM",
            "company": "Juspay",
            "bio": "Builds checkout",
            "here_for": "meet designers",
            "interests": ["payments", "design"],
            "talks": [],
        },
    )
    cb, _ = await signup("Bo Chen")
    p = (await cb.get("/api/people/asha")).json()
    assert p["name"] == "Asha Rao" and p["bio"] == "Builds checkout" and p["company"] == "Juspay"
    assert "contact" in p and "agent_active" in p and p["rooms_together"] == []
    assert "phone" not in str(p)

    await ca.put("/api/me", json={"listed": False})
    assert (await cb.get("/api/people/asha")).status_code == 404
    assert (await ca.get("/api/people/asha")).status_code == 200  # your own
    await ca.post("/api/rooms", json={"title": "Hi", "invite": ["bo"]})  # now they deal with each other
    p = (await cb.get("/api/people/asha")).json()
    assert p["rooms_together"] and p["name"] == "Asha Rao"
    assert (await cb.get("/api/people/nobody")).status_code == 404
