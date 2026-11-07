"""Day pass ledger: the worker API (/internal/daypass/*), spend cap, pause."""

import importlib.util
import time
from pathlib import Path

import pytest

from .conftest import WORKER_KEY, mcp_call

W = {"x-hallway-worker": WORKER_KEY}


@pytest.fixture
async def daypass(signup):
    pc, me = await signup("Sana Qureshi", mode="daypass", profile={"here_for": "payments partners"})
    return pc, me


async def report(client, attendee_id, cost, **kw):
    return await client.post(
        f"/internal/daypass/{attendee_id}/runs",
        headers=W,
        json={"engine": "test", "cost_usd": cost, "summary": "did things", **kw},
    )


async def attendee_id(hallway, handle):
    return hallway.store.attendee(handle=handle)["id"]


async def test_due_requires_worker_header(client, daypass):
    assert (await client.get("/internal/daypass/due")).status_code == 401
    r = await client.get("/internal/daypass/due", headers={"x-hallway-worker": "wrong"})
    assert r.status_code == 401
    r = await client.post("/internal/daypass/x/runs", json={"engine": "t", "cost_usd": 0})
    assert r.status_code == 401


async def test_due_lists_active_passes(client, daypass, signup):
    _, me = daypass
    await signup("Not Daypass")
    r = await client.get("/internal/daypass/due", headers=W)
    assert r.status_code == 200
    [due] = r.json()
    # A run token for the worker, not the attendee's own (hashed, unrecoverable) key.
    assert due["handle"] == "sana" and due["token"].startswith("hwp_") and due["token"] != me["connect"]["token"]
    assert due["mcp_url"] == "http://testserver/mcp"
    assert due["remaining_usd"] == 0.5 and due["runs"] == 0 and due["here_for"] == "payments partners"


async def test_run_report_updates_ledger_and_due_window(client, hallway, daypass):
    pc, _ = daypass
    aid = await attendee_id(hallway, "sana")
    r = await report(client, aid, 0.12, tool_calls=5)
    assert r.json() == {"status": "active", "spent_usd": 0.12, "remaining_usd": 0.38}
    # Just ran: not due again inside the window, due with every_s=0.
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []
    assert len((await client.get("/internal/daypass/due?every_s=0", headers=W)).json()) == 1

    p = (await pc.get("/api/me")).json()["pass"]
    assert p["runs"] == 1 and p["spent_usd"] == 0.12 and p["remaining_usd"] == 0.38
    assert p["recent"][0]["tool_calls"] == 5 and p["recent"][0]["summary"] == "did things"


async def test_negative_cost_is_ignored(client, hallway, daypass):
    aid = await attendee_id(hallway, "sana")
    r = await report(client, aid, -5)
    assert r.json()["spent_usd"] == 0 and r.json()["remaining_usd"] == 0.5


async def test_overshoot_marks_spent_and_remaining_never_negative(client, hallway, daypass):
    pc, _me = daypass
    aid = await attendee_id(hallway, "sana")
    await report(client, aid, 0.40)
    r = await report(client, aid, 0.35)  # one run overshoots the $0.50 cap
    assert r.json() == {"status": "spent", "spent_usd": 0.75, "remaining_usd": 0.0}
    p = (await pc.get("/api/me")).json()["pass"]
    assert p["status"] == "spent" and p["remaining_usd"] == 0.0
    assert (await client.get("/internal/daypass/due?every_s=0", headers=W)).json() == []
    notices = (await pc.get("/api/notices")).json()
    assert sum("used its whole budget" in n["text"] for n in notices) == 1


async def test_spent_pass_refuses_tools(client, hallway, daypass):
    pc, me = daypass
    tok = me["connect"]["token"]
    assert "your_human" in await mcp_call(client, tok, "whoami")
    await report(client, await attendee_id(hallway, "sana"), 0.50)
    res = await mcp_call(client, tok, "whoami")
    assert "paused or out of budget" in res["error"]
    # ...and the person can't un-spend it by resuming.
    r = await pc.post("/api/me/pass", json={"paused": False})
    assert r.status_code == 400 and "whole budget" in r.json()["error"]


async def test_paused_pass_refuses_tools_until_resumed(client, daypass):
    pc, me = daypass
    tok = me["connect"]["token"]
    r = await pc.post("/api/me/pass", json={"paused": True})
    assert r.status_code == 200
    assert (await pc.get("/api/me")).json()["pass"]["status"] == "paused"
    assert "paused or out of budget" in (await mcp_call(client, tok, "search_people"))["error"]
    assert (await client.get("/internal/daypass/due?every_s=0", headers=W)).json() == []

    await pc.post("/api/me/pass", json={"paused": False})
    assert (await pc.get("/api/me")).json()["pass"]["status"] == "active"
    assert isinstance(await mcp_call(client, tok, "search_people"), list)


async def test_pause_without_pass(signup):
    pc, _ = await signup("Asha Rao")
    r = await pc.post("/api/me/pass", json={"paused": True})
    assert r.status_code == 400 and "don't have a day pass" in r.json()["error"]


async def test_report_for_unknown_pass(client):
    r = await report(client, "nobody", 0.1)
    assert r.status_code == 400 and r.json() == {"error": "No such pass."}


async def test_due_orders_never_run_first(client, hallway, signup, daypass):
    await signup("Tara Iyer", mode="daypass")
    await report(client, await attendee_id(hallway, "sana"), 0.01)
    hallway.store.db.execute("UPDATE passes SET last_run_at = ?", (time.time() - 3600,))
    hallway.store.db.execute(
        "UPDATE passes SET last_run_at = NULL WHERE attendee_id = ?", (await attendee_id(hallway, "tara"),)
    )
    due = (await client.get("/internal/daypass/due", headers=W)).json()
    assert [d["handle"] for d in due] == ["tara", "sana"]


async def test_note_reaches_the_worker_and_runs_early(client, hallway, daypass):
    pc, _ = daypass
    aid = await attendee_id(hallway, "sana")
    await report(client, aid, 0.01)
    hallway.store.db.execute("UPDATE passes SET last_run_at = ?", (time.time() - 300,))
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []  # ran 5 min ago: not due

    r = await pc.post("/api/me/pass", json={"note": "  Find me   DevOps people.  "})
    assert r.status_code == 200
    p = r.json()["pass"]
    assert p["note"] == "Find me DevOps people." and p["note_at"] and p["status"] == "active"
    [due] = (await client.get("/internal/daypass/due", headers=W)).json()
    assert due["note"] == "Find me DevOps people."


async def test_note_nudges_at_most_every_two_minutes(client, hallway, daypass):
    pc, _ = daypass
    await report(client, await attendee_id(hallway, "sana"), 0.01)  # ran seconds ago
    await pc.post("/api/me/pass", json={"note": "Find me DevOps people."})
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []


async def test_note_limits_and_pause_untouched(signup, daypass):
    pc, _ = daypass
    r = await pc.post("/api/me/pass", json={"note": "x" * 281})
    assert r.status_code == 400 and "280" in r.json()["error"]
    r = await pc.post("/api/me/pass", json={"note": ""})
    assert r.status_code == 200 and r.json()["pass"]["note"] == "" and r.json()["pass"]["status"] == "active"
    other, _ = await signup("Asha Rao")
    r = await other.post("/api/me/pass", json={"note": "hi"})
    assert r.status_code == 400 and "don't have a day pass" in r.json()["error"]


def test_worker_prompt_carries_the_note():
    spec = importlib.util.spec_from_file_location(
        "daypass_worker", Path(__file__).parent.parent / "daypass" / "worker.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    p = {"name": "Sana Qureshi", "here_for": "payments partners", "runs": 3, "note": "Find me DevOps people."}
    system, task = worker.prompts(p)
    assert "Sana's latest request for you" in system and "Find me DevOps people." in system
    assert "left you a request" in task
    system, _ = worker.prompts({**p, "note": ""})
    assert "latest request" not in system
