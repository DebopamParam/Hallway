"""Rooms created at the same moment all get made (QA #7). Every room is created
by the one host agent, and Switch allows one create in flight per agent."""

import asyncio

from app import core
from app.switch import SwitchError

from .conftest import mcp_call


async def test_rooms_created_at_once_from_the_app_all_succeed(signup):
    people = [await signup(f"Qafix Conc{i}") for i in range(3)]
    await signup("Bo Chen")
    rs = await asyncio.gather(*(pc.post("/api/rooms", json={"title": "Conc", "invite": ["bo"]}) for pc, _ in people))
    assert [r.status_code for r in rs] == [200, 200, 200]
    assert sorted(r.json()["name"] for r in rs) == ["conc", "conc-2", "conc-3"]


async def test_meetings_accepted_at_once_each_get_their_room(signup, client, hallway):
    pairs = []
    for i in range(2):
        _, p = await signup(f"Qafix Prop{i}", autonomy={"propose_meeting": "auto"})
        ic, _ = await signup(f"Qafix Inv{i}")
        invitee = hallway.store.need_attendee(name=f"Qafix Inv{i}")["handle"]
        res = await mcp_call(
            client,
            p["connect"]["token"],
            "propose_meeting",
            {"with_who": invitee, "time": "17:00", "place": "B", "why": "x"},
        )
        pairs.append((ic, res["meeting_id"]))
    rs = await asyncio.gather(*(ic.post(f"/api/meetings/{mid}", json={"accept": True}) for ic, mid in pairs))
    assert [r.status_code for r in rs] == [200, 200], [r.text for r in rs]
    assert all(r.json()["status"] == "confirmed" for r in rs)


async def test_parallel_create_room_tools_all_succeed(signup, client):
    tokens = [(await signup(f"Qafix Tool{i}", autonomy={"rooms": "auto"}))[1]["connect"]["token"] for i in range(3)]
    await signup("Bo Chen")
    res = await asyncio.gather(
        *(mcp_call(client, t, "create_room", {"title": f"T{i}", "invite": ["bo"]}) for i, t in enumerate(tokens))
    )
    assert [r.get("status") for r in res] == ["created"] * 3, res


async def test_a_switch_that_stays_busy_gives_a_clear_error_not_a_500(signup, client, hallway, monkeypatch):
    monkeypatch.setattr(core, "HOST_CREATE_BACKOFF_S", 0)

    async def busy(*_a, **_k):
        raise SwitchError(400, '{"detail":"You are already creating a room. Wait for it to finish, then try again."}')

    monkeypatch.setattr(hallway.switch, "create_room", busy)
    pc, asha = await signup("Asha Rao", autonomy={"rooms": "auto"})
    await signup("Bo Chen")
    r = await pc.post("/api/rooms", json={"title": "Busy", "invite": ["bo"]})
    assert r.status_code == 400 and "try again" in r.json()["error"]
    res = await mcp_call(client, asha["connect"]["token"], "create_room", {"title": "Busy", "invite": ["bo"]})
    assert "try again" in res["error"] and "Switch answered" not in res["error"]
