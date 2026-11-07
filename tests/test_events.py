"""The event feed (/agent/events): what a watcher or the day pass worker waits
on instead of an LLM polling my_inbox. Also switching Connect <-> day pass."""

import dataclasses
import importlib.util
import time
from pathlib import Path

from app import core
from app.store import Actor

from .conftest import WORKER_KEY

W = {"x-hallway-worker": WORKER_KEY}


async def feed(client, token, after=""):
    r = await client.get(f"/agent/events?after={after}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def lobby_channel(hallway, world):
    room = hallway.store.one("SELECT id FROM rooms WHERE kind = 'lobby'")
    return world.channel_for_room(room["id"])


async def test_feed_relays_what_is_for_the_agent_and_nothing_else(signup, client, hallway, world):
    _, asha = await signup("Asha Rao")
    await signup("Bo Chen")
    tok = asha["connect"]["token"]
    a = hallway.store.need_attendee(handle="asha")
    bo = hallway.store.need_attendee(handle="bo")
    await hallway.tail_once()  # rooms start from now

    first = await feed(client, tok)
    assert first["events"] == []  # no cursor yet: nothing, and a cursor for now

    lobby = lobby_channel(hallway, world)
    world.add_post(lobby, bo["mm_user_id"], "Morning all")  # lobby chatter: not for Asha's agent
    world.add_post(lobby, bo["mm_user_id"], f"@{a['agent_name']} coffee at 3?")
    world.add_post(lobby, f"u_{a['agent_name']}", f"@{a['agent_name']} talking to myself")  # its own
    hallway.store.notice(a["id"], "meeting", "Bo wants to meet at 15:00", audience="both")
    hallway.store.notice(a["id"], "posted", "Your agent posted", actor=Actor.agent(a))  # its own doing
    hallway.store.notice(a["id"], "fyi", "Only for Asha", audience="person")
    await hallway.tail_once()

    got = await feed(client, tok, first["after"])
    assert got["events"] == [
        {"kind": "meeting", "text": "Bo wants to meet at 15:00"},
        {
            "kind": "message",
            "room": lobby["name"],
            "from": "bo",
            "text": f"@{a['agent_name']} coffee at 3?",
            "mentions_you": True,
        },
    ]
    assert (await feed(client, tok, got["after"]))["events"] == []
    # Reading the feed marks nothing read: my_inbox still has the notice.
    assert (
        hallway.store.one("SELECT agent_seen FROM notices WHERE text = 'Bo wants to meet at 15:00'")["agent_seen"] == 0
    )


async def test_feed_auth_and_cursor(signup, client):
    _, asha = await signup("Asha Rao")
    assert (await client.get("/agent/events")).status_code == 401
    assert (await client.get("/agent/events", headers={"Authorization": "Bearer hw_nope"})).status_code == 401
    tok = asha["connect"]["token"]
    r = await client.get("/agent/events?after=junk", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400
    r = await client.get(f"/agent/events?key={tok}&wait=0")
    assert r.status_code == 200 and "." in r.json()["after"]


async def test_day_pass_wakes_on_events_not_just_the_timer(signup, client, hallway):
    await signup("Sana Qureshi", mode="daypass")
    s = hallway.store.need_attendee(handle="sana")
    r = await client.post(f"/internal/daypass/{s['id']}/runs", headers=W, json={"engine": "t", "cost_usd": 0.01})
    assert r.status_code == 200
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []  # just ran

    hallway.store.notice(s["id"], "meeting", "Bo wants to meet")
    # Within PASS_WAKE_GAP_S of the last run: it waits, and keeps the event.
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []
    hallway.store.db.execute(
        "UPDATE passes SET last_run_at = ? WHERE attendee_id = ?", (time.time() - core.PASS_WAKE_GAP_S - 1, s["id"])
    )
    [due] = (await client.get("/internal/daypass/due", headers=W)).json()
    assert due["handle"] == "sana" and due["events"] == [{"kind": "meeting", "text": "Bo wants to meet"}]
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []  # handed over once


async def test_switch_modes_keeps_the_pass_budget(signup, client, hallway, monkeypatch):
    c, _ = await signup("Asha Rao")
    me = (await c.post("/api/me/mode", json={"mode": "daypass"})).json()
    assert me["mode"] == "daypass" and me["pass"]["status"] == "active"
    a = hallway.store.need_attendee(handle="asha")
    [due] = (await client.get("/internal/daypass/due", headers=W)).json()
    assert due["handle"] == "asha"
    await client.post(f"/internal/daypass/{a['id']}/runs", headers=W, json={"engine": "t", "cost_usd": 0.2})

    me = (await c.post("/api/me/mode", json={"mode": "connect"})).json()
    assert me["mode"] == "connect" and me["pass"] is None
    assert (await client.get("/internal/daypass/due?every_s=0", headers=W)).json() == []

    me = (await c.post("/api/me/mode", json={"mode": "daypass"})).json()
    assert me["pass"]["spent_usd"] == 0.2  # switching back doesn't reset the budget

    assert (await c.post("/api/me/mode", json={"mode": "live"})).status_code == 400
    monkeypatch.setattr(core, "settings", dataclasses.replace(core.settings, daypass_enabled=False))
    await c.post("/api/me/mode", json={"mode": "connect"})
    r = await c.post("/api/me/mode", json={"mode": "daypass"})
    assert r.status_code == 400 and "aren't available" in r.json()["error"]


def test_worker_prompt_relays_events():
    spec = importlib.util.spec_from_file_location(
        "daypass_worker", Path(__file__).parent.parent / "daypass" / "worker.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    p = {
        "name": "Sana Qureshi",
        "here_for": "payments",
        "runs": 2,
        "note": "",
        "events": [{"kind": "message", "room": "lobby", "from": "bo", "text": "@sana.agent hi", "mentions_you": True}],
    }
    _, task = worker.prompts(p)
    assert "Something new just arrived for Sana" in task and 'bo in #lobby: "@sana.agent hi"' in task
    _, task = worker.prompts({**p, "events": []})
    assert "Something new" not in task
