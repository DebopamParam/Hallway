"""Free text that reaches other attendees outside a Switch message (room titles,
meeting proposals) is redacted like a message would be (QA #3)."""

from .conftest import mcp_call

VAULT = {"phone": "+91 99001 44556", "email": "qa2.private@example.com"}
LEAKS = ["99001", "44556", "9900144556", "qa2.private", "6E 2134"]


def assert_clean(*blobs) -> None:
    flat = " ".join(str(b) for b in blobs)
    for s in LEAKS:
        assert s not in flat, f"{s!r} reached the other side"


async def test_meeting_proposal_fields_are_redacted_for_the_invitee(signup, client):
    _, asha = await signup("Asha Rao", contact=VAULT, autonomy={"propose_meeting": "auto"})
    bc, bo = await signup("Bo Chen")
    why = "Call my human on +91 99001 44556 or mail qa2.private@example.com, she flies 6E 2134 tonight"
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "propose_meeting",
        {"with_who": "bo", "time": "17:00", "place": "Room 412, ring +91 99001 44556", "why": why},
    )
    assert res["status"] == "proposed"
    tb = bo["connect"]["token"]
    seen = [
        await mcp_call(client, tb, "my_inbox"),
        await mcp_call(client, tb, "list_meetings"),
        (await bc.get("/api/meetings")).json(),
        (await bc.get("/api/notices")).json(),
    ]
    assert "[redacted]" in str(seen)
    assert_clean(seen)


async def test_meeting_proposal_awaiting_approval_holds_redacted_text(signup, client):
    ac, asha = await signup("Asha Rao", contact=VAULT)  # propose_meeting: ask
    bc, _ = await signup("Bo Chen")
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "propose_meeting",
        {"with_who": "bo", "time": "17:00", "place": "Hall B", "why": "mail qa2.private@example.com"},
    )
    assert res["status"] == "pending_approval"
    await ac.post(f"/api/approvals/{res['approval_id']}", json={"approve": True})
    assert_clean((await bc.get("/api/meetings")).json(), (await bc.get("/api/notices")).json())


async def test_room_title_is_redacted_everywhere_the_other_side_sees_it(signup, client, world):
    _, asha = await signup("Asha Rao", contact=VAULT, autonomy={"rooms": "auto"})
    bc, bo = await signup("Bo Chen")
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "create_room",
        {"title": "Ping qa2.private@example.com 9900144556", "invite": ["bo"]},
    )
    assert res["status"] == "created"
    assert res["room"] == "ping-redacted-redacted"
    seen = [
        await mcp_call(client, bo["connect"]["token"], "list_rooms"),
        await mcp_call(client, bo["connect"]["token"], "my_inbox"),
        (await bc.get("/api/rooms")).json(),
        (await bc.get("/api/notices")).json(),
        world.calls,  # what Switch was given: the room's name and description
    ]
    assert_clean(seen)


async def test_room_title_from_the_app_is_redacted(signup):
    ac, _ = await signup("Asha Rao", contact=VAULT)
    await signup("Bo Chen")
    r = await ac.post("/api/rooms", json={"title": "Ping +91 99001 44556", "invite": ["bo"]})
    assert r.status_code == 200
    assert r.json()["title"] == "Ping [redacted]"


async def test_free_text_has_length_limits(signup, client):
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    await signup("Bo Chen")
    r = await ac.post("/api/rooms", json={"title": "x" * 81, "invite": ["bo"]})
    assert r.status_code == 400 and "at most 80" in r.json()["error"]
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "propose_meeting",
        {"with_who": "bo", "time": "17:00", "place": "B", "why": "y" * 301},
    )
    assert "at most 300" in str(res)
