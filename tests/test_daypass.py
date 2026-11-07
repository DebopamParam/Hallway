"""Day pass ledger: the worker API (/internal/daypass/*), spend cap, pause."""

import functools
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
    # Just ran: not due again inside the window. Past it, a round runs only
    # when there's something to do: nothing new, no LLM call.
    assert (await client.get("/internal/daypass/due", headers=W)).json() == []
    assert (await client.get("/internal/daypass/due?every_s=0", headers=W)).json() == []
    hallway.set_pass_note(hallway.store.need_attendee(id=aid), "Find me security engineers")
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


async def test_idle_pass_skips_rounds_until_someone_new_arrives(client, hallway, daypass, signup):
    aid = await attendee_id(hallway, "sana")
    hallway.store.update("attendees", aid, lobby_agent=1)
    await report(client, aid, 0.01)
    assert (await client.get("/internal/daypass/due?every_s=0", headers=W)).json() == []
    await signup("Bo Chen")  # someone new to look at
    assert [p["handle"] for p in (await client.get("/internal/daypass/due?every_s=0", headers=W)).json()] == ["sana"]


def load_worker():
    spec = importlib.util.spec_from_file_location(
        "daypass_worker", Path(__file__).parent.parent / "daypass" / "worker.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


@pytest.fixture
def priced_worker(monkeypatch):
    """The worker on a model PydanticAI can't price, at the production rates,
    with Hallway's MCP tools swapped for a local propose_shortlist."""
    pytest.importorskip("pydantic_ai")  # the worker's dependency, not the app's
    from pydantic_ai.toolsets import FunctionToolset

    def propose_shortlist(goal: str) -> str:
        return "Shortlist is in the app."

    worker = load_worker()
    monkeypatch.setattr(worker, "PRICE_PER_MTOK", "0.30,1.20")
    monkeypatch.setattr("pydantic_ai.mcp.MCPToolset", lambda *a, **kw: FunctionToolset([propose_shortlist]))
    return worker


async def test_priced_run_gives_each_reply_what_the_budget_has_room_for(priced_worker):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    sent = []

    def model(messages, info: AgentInfo) -> ModelResponse:
        sent.append((info.model_settings or {}).get("max_tokens"))
        return ModelResponse(parts=[TextPart("Your shortlist is in the app.")])

    p = {"name": "Riya Kapoor", "handle": "riya", "runs": 1, "mcp_url": "http://hallway.invalid/mcp", "token": "t"}
    run = await priced_worker.run_pydantic_ai(p, 0.10, model=FunctionModel(model))
    assert sent == [8192] and run.reply == "Your shortlist is in the app."
    # $0.0051 left is just above the $0.005 at which Hallway marks a pass spent:
    # the run still goes ahead, with a reply that can't pass the budget.
    sent.clear()
    run = await priced_worker.run_pydantic_ai(p, 0.0051, model=FunctionModel(model))
    assert run.reply == "Your shortlist is in the app."
    assert sent[0] >= 1024 and 4000 * 0.30e-6 + sent[0] * 1.20e-6 <= 0.0051
    # An operator can enter a free output price; then only the prompt counts.
    for price in ("0.30,0", "0,0"):
        priced_worker.PRICE_PER_MTOK = price
        sent.clear()
        run = await priced_worker.run_pydantic_ai(p, 0.0051, model=FunctionModel(model))
        assert run.reply == "Your shortlist is in the app." and sent == [8192]


async def test_failed_run_still_charges_the_pass_for_its_requests(hallway, daypass, make_client, priced_worker):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.usage import RequestUsage

    calls = 0

    def cut_off(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[ToolCallPart("propose_shortlist", '{"goal": "Who Riya should')],
            usage=RequestUsage(input_tokens=5000, output_tokens=2500),
            finish_reason="length",
        )

    priced_worker.ENGINES["pydantic-ai"] = functools.partial(
        priced_worker.run_pydantic_ai, model=FunctionModel(cut_off)
    )
    http = make_client()
    http.headers["x-hallway-worker"] = WORKER_KEY
    [p] = (await http.get("/internal/daypass/due?wait=0")).json()
    await priced_worker.run_one(http, p, "pydantic-ai")

    pc, _ = daypass
    assert (await pc.get("/api/me/chat")).json()["messages"][-1]["text"] == priced_worker.FAILED_REPLY
    ledger = hallway.store.one("SELECT spent_usd, runs FROM passes WHERE attendee_id = ?", p["attendee_id"])
    assert calls >= 1 and ledger["runs"] == 1
    assert ledger["spent_usd"] == pytest.approx(calls * (5000 * 0.30 + 2500 * 1.20) / 1e6, abs=1e-6)
