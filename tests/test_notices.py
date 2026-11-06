"""Structured notices (#20): each notice carries its kind, actor, subject ids
and payload, so neither the app nor agents parse the `text` back. Old text-only
notices are converted by migration 4 where possible, else shown as text."""

import json
import sqlite3
import time

from app import store as store_mod
from app.core import agent_notice
from app.store import MIGRATIONS, Actor, Store, migrate

from .conftest import mcp_call
from .test_meeting_accept import propose
from .test_store import raw_db


async def test_meeting_notices_carry_actor_subject_and_payload(signup, client, hallway):
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, bo = await signup("Bo Chen")
    mid = await propose(client, asha)

    [n] = [n for n in (await bc.get("/api/notices")).json() if n["kind"] == "meeting_proposed"]
    assert n["structured"] is True
    assert n["actor"] == {"kind": "person", "handle": "asha", "name": "Asha Rao"}
    assert n["subject"] == {"meeting_id": mid}
    assert n["payload"]["time"] == "15:50" and n["payload"]["place"] == "Hall B exit"
    assert n["payload"]["why"] == "PQC pilots"
    assert n["text"] == "Asha Rao would like to meet you at 15:50, Hall B exit: PQC pilots"
    assert "actor_id" not in n  # attendee ids stay internal

    # The agent's inbox: a readable line plus the same fields.
    inbox = await mcp_call(client, bo["connect"]["token"], "my_inbox", {})
    [m] = [m for m in inbox["notices"] if m["kind"] == "meeting_proposed"]
    assert m["text"] == n["text"]
    assert m["actor"] == n["actor"] and m["meeting_id"] == mid and m["with_handle"] == "asha"
    assert m["time"] == "15:50"

    assert (await bc.post(f"/api/meetings/{mid}", json={"accept": True})).status_code == 200
    [c] = [n for n in (await ac.get("/api/notices")).json() if n["kind"] == "meeting_confirmed"]
    assert c["actor"]["handle"] == "bo"
    assert c["subject"]["meeting_id"] == mid and c["subject"]["room"] == "meet-asha-bo" and c["subject"]["room_id"]
    assert c["payload"]["proposer_name"] == "Asha Rao" and c["payload"]["invitee_name"] == "Bo Chen"


async def test_the_meeting_card_in_the_room_comes_as_fields(signup, client, hallway):
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _ = await signup("Bo Chen")
    mid = await propose(client, asha)
    assert (await bc.post(f"/api/meetings/{mid}", json={"accept": True})).status_code == 200
    msgs = (await ac.get("/api/rooms/meet-asha-bo/messages")).json()["messages"]
    [card] = [m["card"] for m in msgs if "card" in m]
    assert card == {
        "kind": "meeting_confirmed",
        "meeting_id": mid,
        "proposer": {"name": "Asha Rao", "handle": "asha"},
        "invitee": {"name": "Bo Chen", "handle": "bo"},
        "time": "15:50",
        "place": "Hall B exit",
        "why": "PQC pilots",
    }
    lobby = (await ac.get("/api/rooms/lobby/messages")).json()["messages"]
    assert not any("card" in m for m in lobby)


async def test_system_notices_have_no_actor(signup, hallway):
    ac, _ = await signup("Asha Rao")
    [w] = [n for n in (await ac.get("/api/notices")).json() if n["kind"] == "welcome"]
    assert w["actor"] is None and w["subject"] == {} and w["structured"] is True
    assert w["payload"]["event"] and w["text"].startswith(f"Welcome to {w['payload']['event']}!")


async def test_presence_mentions_are_structured(signup, hallway):
    ac, _ = await signup("Asha Rao")
    await signup("Bo Chen")
    asha = hallway.store.need_attendee(handle="asha")
    lobby = hallway.store.one("SELECT id FROM rooms WHERE kind = 'lobby'")
    on = hallway.presence._on_addressed
    on(asha, {"room_id": lobby["id"], "payload": {"id": "e1", "sender_name": "bo.agent", "body": "@asha.agent hi"}})
    on(asha, {"payload": {"id": "e2", "sender_name": "bo", "body": "hello from Bo"}})
    on(asha, {"payload": {"id": "e3", "sender_name": "stranger-bot", "body": "yo"}})
    on(asha, {"payload": {"id": "e3", "sender_name": "stranger-bot", "body": "yo"}})  # replayed: once only
    mentions = [n for n in (await ac.get("/api/notices")).json() if n["kind"] == "mention"][::-1]
    assert [(n["actor"], n["payload"]["body"]) for n in mentions] == [
        ({"kind": "agent", "handle": "bo.agent", "name": "Bo Chen"}, "@asha.agent hi"),
        ({"kind": "person", "handle": "bo", "name": "Bo Chen"}, "hello from Bo"),
        ({"kind": "agent", "handle": "stranger-bot", "name": "stranger-bot"}, "yo"),
    ]
    assert mentions[0]["subject"] == {"room_id": lobby["id"], "room": "hallway-lobby"}
    # The text keeps its old wording, for agents and anything still reading it.
    assert [n["text"] for n in mentions] == [
        "Bo Chen (agent) → your agent: @asha.agent hi",
        "Bo Chen → your agent: hello from Bo",
        "stranger-bot → your agent: yo",
    ]


def test_store_refuses_an_unknown_actor_kind(tmp_path):
    s = Store(str(tmp_path / "h.db"))
    try:
        s.notice("a1", "x", "t", actor=Actor("robot"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    s.db.close()


def test_agent_notice_flattens_but_keeps_kind_and_text():
    n = {
        "kind": "approval",
        "text": "Asha approved: hi",
        "actor": {"kind": "person", "handle": "asha", "name": "Asha"},
        "subject": {"approval_id": "ap1"},
        "payload": {"approved": True, "kind": "sneaky", "text": "nope"},
    }
    assert agent_notice(n) == {
        "kind": "approval",
        "text": "Asha approved: hi",
        "actor": {"kind": "person", "handle": "asha", "name": "Asha"},
        "approval_id": "ap1",
        "approved": True,
    }


# ------------------------------------------------------------- migration 4
def _old_notice(db: sqlite3.Connection, kind: str, text: str, audience: str = "person", **payload) -> int:
    cur = db.execute(
        "INSERT INTO notices (attendee_id, audience, kind, text, payload, created_at) VALUES ('a2', ?, ?, ?, ?, ?)",
        (audience, kind, text, json.dumps(payload), time.time()),
    )
    return int(cur.lastrowid or 0)


def test_migration_structures_old_text_notices(tmp_path):
    path = tmp_path / "h.db"
    db = raw_db(path, 3)
    for aid, name, handle in (("a1", "Asha Rao", "asha"), ("a2", "Bo Chen", "bo")):
        db.execute(
            "INSERT INTO attendees (id, name, email, handle, agent_name, mcp_token_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (aid, name, f"{handle}@x.io", handle, f"{handle}.agent", "h" + aid),
        )
    db.execute("INSERT INTO rooms (id, name, title, kind, created_at) VALUES ('r1', 'meet-asha-bo', 'Meet', 'meet', 1)")
    db.execute(
        "INSERT INTO meetings (id, proposer_id, invitee_id, time, place, why, status, room_id, created_at) "
        "VALUES ('m1', 'a1', 'a2', '12:00', 'Cafe', 'PQC', 'confirmed', 'r1', 1)"
    )
    ids = {
        "mention": _old_notice(db, "mention", "Asha Rao (agent) → your agent: @bo.agent hi\nthere"),
        "mention_person": _old_notice(db, "mention", "Asha Rao → your agent: hey"),
        "welcome": _old_notice(db, "welcome", "Welcome to PQC Day! Connect your agent from the Connect tab."),
        "room": _old_notice(db, "room", "Asha Rao started a room with you: PQC pilots", "both", room="meet-asha-bo"),
        "proposed": _old_notice(db, "meeting_proposed", "…", "both", meeting_id="m1", with_handle="asha"),
        "confirmed": _old_notice(db, "meeting_confirmed", "…", "both", meeting_id="m1", room="meet-asha-bo"),
        "gone": _old_notice(db, "meeting_declined", "Someone can't make 9.", "both", meeting_id="nope"),
        "swap": _old_notice(db, "contact_exchanged", "Contact details swapped with Asha Rao. They're in the app."),
        "privacy": _old_notice(db, "privacy_check", "Privacy check failed", passed=False, leaks=[{"q": 1}]),
        "odd": _old_notice(db, "something_new", "A kind this code never wrote"),
    }
    db.close()

    s = Store(str(path))
    assert store_mod.db_version(s.db) == len(MIGRATIONS)
    got = {k: n for n in s.notices("a2") for k, i in ids.items() if n["id"] == i}
    assert got["mention"]["actor"] == {"kind": "agent", "handle": "asha.agent", "name": "Asha Rao"}
    assert got["mention"]["payload"] == {"body": "@bo.agent hi\nthere"}
    assert got["mention_person"]["actor"] == {"kind": "person", "handle": "asha", "name": "Asha Rao"}
    assert got["welcome"]["payload"] == {"event": "PQC Day"} and got["welcome"]["actor"] is None
    assert got["room"]["subject"] == {"room_id": "r1", "room": "meet-asha-bo"}
    assert got["room"]["payload"]["title"] == "PQC pilots"
    assert got["proposed"]["actor"]["handle"] == "asha" and got["proposed"]["subject"] == {"meeting_id": "m1"}
    assert got["proposed"]["payload"] == {
        "meeting_id": "m1",
        "with_handle": "asha",
        "time": "12:00",
        "place": "Cafe",
        "why": "PQC",
    }
    assert got["confirmed"]["subject"] == {"room_id": "r1", "room": "meet-asha-bo", "meeting_id": "m1"}
    assert got["confirmed"]["payload"]["proposer_name"] == "Asha Rao"
    assert got["confirmed"]["payload"]["invitee_name"] == "Bo Chen"
    assert got["swap"]["actor"]["name"] == "Asha Rao"
    assert got["privacy"]["payload"]["leak_count"] == 1
    assert all(got[k]["structured"] for k in ids if k not in ("gone", "odd"))
    # What can't be converted stays as it was, shown as its text.
    for k in ("gone", "odd"):
        assert got[k]["structured"] is False and got[k]["actor"] is None
    assert got["odd"]["text"] == "A kind this code never wrote"
    s.db.close()


def test_migration_4_leaves_the_row_factory_alone(tmp_path):
    db = sqlite3.connect(tmp_path / "h.db", isolation_level=None)
    db.create_function("sha256_hex", 1, store_mod.token_hash)
    migrate(db)
    assert db.row_factory is None
    s = Store(str(tmp_path / "h2.db"))
    assert s.db.row_factory is sqlite3.Row
    s.db.close()
