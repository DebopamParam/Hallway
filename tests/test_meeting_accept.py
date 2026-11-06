"""Accepting a meeting is idempotent and safe when several answers race.

The live demo once answered an accept with a 500: `UNIQUE constraint failed:
rooms.id` in create_room. Switch had made the room, and while Hallway waited
for its Mattermost channel, the sync loop found the room on Switch, didn't know
it yet, and adopted it into `rooms` under Switch's id; create_room's own INSERT
then hit that id. The same accept can also arrive several ways at once (the
person in the app, their agent over MCP, an approval), each of which used to
make its own room."""

import asyncio

import pytest

from app import core
from app.switch import SwitchError

from .conftest import mcp_call, register


async def propose(client, asha, **kw) -> str:
    res = await mcp_call(
        client,
        asha["connect"]["token"],
        "propose_meeting",
        {"with_who": "bo", "time": "15:50", "place": "Hall B exit", "why": "PQC pilots", **kw},
    )
    assert res["status"] == "proposed", res
    return res["meeting_id"]


def meet_rooms_created(world) -> list:
    return [c for c in world.calls if c[0] == "create_room" and c[1]["name"].startswith("meet-")]


def cards(world) -> list:
    return [c for c in world.calls if c[0] == "post" and "Meeting confirmed" in c[1][2]]


async def test_sync_loop_adopting_the_new_room_does_not_break_the_accept(signup, client, hallway, world, monkeypatch):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _ = await signup("Bo Chen")
    mid = await propose(client, asha)

    wait_channel = core.Hallway._wait_channel

    async def sync_runs_meanwhile(self, name, created_after_ms, tries=20):
        ch = await wait_channel(self, name, created_after_ms, tries)
        await self.sync_once()  # the 5 s sync loop ticks while we wait for the channel
        return ch

    monkeypatch.setattr(core.Hallway, "_wait_channel", sync_runs_meanwhile)
    r = await bc.post(f"/api/meetings/{mid}", json={"accept": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "confirmed", "meeting_id": mid, "room": "meet-asha-bo"}
    [room] = hallway.store.all("SELECT * FROM rooms WHERE name = 'meet-asha-bo'")
    # create_room's details win over the sync loop's guesses.
    assert room["kind"] == "meet" and room["title"] == "Meet: Asha + Bo"
    assert room["created_by"] == hallway.store.need_attendee(handle="asha")["id"]
    assert room["mm_channel_id"]
    assert len(cards(world)) == 1


async def test_sync_loop_leaves_a_room_being_created_alone(signup, hallway, world, monkeypatch):
    _, asha = await signup("Asha Rao")
    await signup("Bo Chen")
    me = hallway.store.need_attendee(handle=asha["handle"])
    wait_channel = core.Hallway._wait_channel
    seen = []

    async def sync_runs_meanwhile(self, name, created_after_ms, tries=20):
        ch = await wait_channel(self, name, created_after_ms, tries)
        await self.sync_once()
        seen.append(self.store.one("SELECT * FROM rooms WHERE name = ?", name))
        return ch

    monkeypatch.setattr(core.Hallway, "_wait_channel", sync_runs_meanwhile)
    room = await hallway.create_room(me, "Payments chat", ["bo"])
    assert seen == [None]  # not adopted while create_room was still working on it
    assert room["created_by"] == me["id"] and room["kind"] == "group"


async def test_concurrent_accepts_make_one_room(signup, client, make_client, hallway, world):
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, bo = await signup("Bo Chen", autonomy={"accept_meeting": "auto"})
    mid = await propose(client, asha)
    bc2 = make_client()
    bc2.cookies = bc.cookies  # Bo on a second device

    results = await asyncio.gather(
        bc.post(f"/api/meetings/{mid}", json={"accept": True}),
        bc2.post(f"/api/meetings/{mid}", json={"accept": True}),
        mcp_call(make_client(), bo["connect"]["token"], "respond_meeting", {"meeting_id": mid, "accept": True}),
        mcp_call(make_client(), bo["connect"]["token"], "respond_meeting", {"meeting_id": mid, "accept": True}),
    )
    bodies = [r.json() for r in results[:2]] + list(results[2:])
    assert all(b == {"status": "confirmed", "meeting_id": mid, "room": "meet-asha-bo"} for b in bodies), bodies
    assert len(meet_rooms_created(world)) == 1
    assert len(cards(world)) == 1
    assert len(hallway.store.all("SELECT * FROM rooms WHERE kind = 'meet'")) == 1
    confirmed = [n for n in (await ac.get("/api/notices")).json() if n["kind"] == "meeting_confirmed"]
    assert len(confirmed) == 1
    assert len(hallway._serial) == 0  # the per-meeting locks are dropped afterwards


async def test_concurrent_approvals_of_the_same_accept_make_one_room(signup, client, hallway, world):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, bo = await signup("Bo Chen")  # accept_meeting: ask
    mid = await propose(client, asha)
    for _ in range(2):  # the agent asked twice
        res = await mcp_call(client, bo["connect"]["token"], "respond_meeting", {"meeting_id": mid, "accept": True})
        assert res["status"] == "pending_approval"
    aps = [a for a in (await bc.get("/api/approvals")).json() if a["status"] == "pending"]
    assert len(aps) == 2

    results = await asyncio.gather(
        *(bc.post(f"/api/approvals/{a['id']}", json={"approve": True}) for a in aps),
        bc.post(f"/api/meetings/{mid}", json={"accept": True}),
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert [r.json()["result"]["room"] for r in results[:2]] == ["meet-asha-bo"] * 2
    assert len(meet_rooms_created(world)) == 1 and len(cards(world)) == 1


async def test_accept_then_decline_keeps_the_meeting(signup, client, world):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _ = await signup("Bo Chen")
    mid = await propose(client, asha)
    yes, no = await asyncio.gather(
        bc.post(f"/api/meetings/{mid}", json={"accept": True}),
        bc.post(f"/api/meetings/{mid}", json={"accept": False}),
    )
    assert yes.json()["status"] == no.json()["status"] == "confirmed"
    [m] = (await bc.get("/api/meetings")).json()
    assert m["status"] == "confirmed" and m["room"] == "meet-asha-bo"


async def test_repeat_accept_returns_the_same_room(signup, client, world):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _ = await signup("Bo Chen")
    mid = await propose(client, asha)
    first = (await bc.post(f"/api/meetings/{mid}", json={"accept": True})).json()
    again = (await bc.post(f"/api/meetings/{mid}", json={"accept": True})).json()
    assert first == again == {"status": "confirmed", "meeting_id": mid, "room": "meet-asha-bo"}
    assert len(meet_rooms_created(world)) == 1


async def test_card_post_failure_still_confirms_and_a_retry_makes_no_second_room(
    signup, client, hallway, world, monkeypatch
):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _ = await signup("Bo Chen")
    mid = await propose(client, asha)
    post = type(hallway.switch).post

    async def card_fails(self, agent, room_id, content):
        if "Meeting confirmed" in content:
            raise SwitchError(502, "gateway hiccup")
        return await post(self, agent, room_id, content)

    monkeypatch.setattr(type(hallway.switch), "post", card_fails)
    r = await bc.post(f"/api/meetings/{mid}", json={"accept": True})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    r = await bc.post(f"/api/meetings/{mid}", json={"accept": True})
    assert r.json()["room"] == "meet-asha-bo"
    assert len(meet_rooms_created(world)) == 1


async def test_failed_room_creation_leaves_the_meeting_open_to_retry(signup, client, hallway, world, monkeypatch):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, bo = await signup("Bo Chen")  # accept_meeting: ask
    mid = await propose(client, asha)
    await mcp_call(client, bo["connect"]["token"], "respond_meeting", {"meeting_id": mid, "accept": True})
    [ap] = [a for a in (await bc.get("/api/approvals")).json() if a["status"] == "pending"]
    create_room = type(hallway.switch).create_room
    fail = [True]

    async def flaky(self, *a, **kw):
        if fail.pop() if fail else False:
            raise SwitchError(503, "Switch restarting")
        return await create_room(self, *a, **kw)

    monkeypatch.setattr(type(hallway.switch), "create_room", flaky)
    with pytest.raises(SwitchError):
        await bc.post(f"/api/approvals/{ap['id']}", json={"approve": True})
    # Nothing happened, so nothing is marked done: the approval can be given again.
    [still] = [a for a in (await bc.get("/api/approvals")).json() if a["id"] == ap["id"]]
    assert still["status"] == "pending"
    assert hallway.store.need("SELECT status FROM meetings WHERE id = ?", mid)["status"] == "proposed"
    r = await bc.post(f"/api/approvals/{ap['id']}", json={"approve": True})
    assert r.json()["result"] == {"status": "confirmed", "meeting_id": mid, "room": "meet-asha-bo"}
    assert len(hallway._serial) == 0


async def test_concurrent_rooms_with_one_title_get_distinct_names(signup, hallway, world):
    _, asha = await signup("Asha Rao")
    _, bo = await signup("Bo Chen")
    a = hallway.store.need_attendee(handle=asha["handle"])
    b = hallway.store.need_attendee(handle=bo["handle"])
    r1, r2 = await asyncio.gather(hallway.create_room(a, "Lunch", ["bo"]), hallway.create_room(b, "Lunch", ["asha"]))
    assert {r1["name"], r2["name"]} == {"lunch", "lunch-2"}
    assert hallway._creating_rooms == set()


async def test_concurrent_registrations_with_one_first_name_get_distinct_accounts(make_client, hallway, world):
    one, two = await asyncio.gather(
        register(make_client(), "Steve Jobs", email="steve.j@example.com"),
        register(make_client(), "Steve Wozniak", email="steve.w@example.com"),
    )
    assert {one["handle"], two["handle"]} == {"steve", "steve2"}
    rows = hallway.store.attendees()
    assert len({r["mm_user_id"] for r in rows}) == len(rows)
    assert hallway._reserved_names == set()


async def test_double_submitted_registration_makes_one_attendee(make_client, hallway):
    one, two = await asyncio.gather(
        register(make_client(), "Asha Rao", email="asha@example.com"),
        register(make_client(), "Asha Rao", email="asha@example.com"),
    )
    assert one["handle"] == two["handle"] == "asha"
    assert len(hallway.store.attendees()) == 1
