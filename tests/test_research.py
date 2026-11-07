"""research_people, propose_shortlist and profile visibility. Exa is faked: no
test reaches the network."""

import dataclasses

import pytest

from app import research

from .conftest import PROFILE, mcp_call

GTM = {"role": "Head of GTM", "company": "Zeta", "bio": "Built sales teams", "interests": ["GTM", "sales"]}


@pytest.fixture
def exa(monkeypatch):
    """Web lookups on, with canned pages; `exa.queries` records what was searched."""
    monkeypatch.setattr(research, "settings", dataclasses.replace(research.settings, exa_api_key="test-key"))
    queries: list[str] = []
    pages: dict[str, list[dict]] = {}

    async def fake(query: str) -> list[dict]:
        queries.append(query)
        name = query.split()[0]
        return pages.get(
            name, [{"title": f"{name} profile", "url": f"https://example.org/{name}", "summary": "An engineer."}]
        )

    monkeypatch.setattr(research, "exa_search", fake)
    fake.queries, fake.pages = queries, pages
    return fake


async def test_research_ranks_on_web_pages_and_scrubs_them(signup, client, exa):
    _, me = await signup("Asha Rao")
    await signup("Bo Chen", profile=GTM)
    await signup("Cy Das", profile={"role": "Engineer", "company": "Food Co"}, contact={"phone": "+91 90000 11111"})
    exa.pages["Cy"] = [
        {
            "title": "Cy Das: GTM advisor",
            "url": "https://cy.example/about",
            "summary": "Cy leads GTM for startups. Reach Cy at +91 90000 11111 or cy@cy.example.",
        }
    ]
    tok = me["connect"]["token"]
    res = await mcp_call(client, tok, "research_people", {"query": "GTM sales leaders"})
    handles = [p["handle"] for p in res["people"]]
    assert handles == ["bo", "cy"]  # Bo on profile; Cy only through the web page
    cy = res["people"][1]
    assert cy["web"][0]["url"] == "https://cy.example/about"
    flat = str(res)
    assert "90000" not in flat and "cy@cy.example" not in flat  # the guard redacts web text too
    assert "asha" not in handles

    # Cached: a second search costs no lookups.
    n = len(exa.queries)
    await mcp_call(client, tok, "research_people", {"query": "GTM"})
    assert len(exa.queries) == n


async def test_research_skips_private_opted_out_and_personas(signup, client, exa, hallway):
    asha_c, me = await signup("Asha Rao")
    bo_c, _ = await signup("Bo Chen", profile=GTM)
    cy_c, _ = await signup("Cy Das", profile=GTM)
    await signup("Di Roy", profile=GTM)
    assert (await bo_c.put("/api/me", json={"listed": False})).json()["listed"] is False
    assert (await cy_c.put("/api/me", json={"web_lookup": False})).json()["web_lookup"] is False
    di = hallway.store.need_attendee(handle="di")
    hallway.store.update("attendees", di["id"], is_persona=1)

    tok = me["connect"]["token"]
    res = await mcp_call(client, tok, "research_people", {"query": "GTM"})
    by = {p["handle"]: p for p in res["people"]}
    assert "bo" not in by  # private: not in anyone's results
    assert by["cy"]["web"] == [] and "opted out" in by["cy"]["web_note"]
    assert by["di"]["web"] == [] and "persona" in by["di"]["web_note"]
    assert exa.queries == []
    assert "bo" not in {p["handle"] for p in await mcp_call(client, tok, "search_people")}
    assert "bo" not in {p["handle"] for p in (await asha_c.get("/api/people")).json()}


async def test_research_daily_budget(signup, client, exa, monkeypatch):
    monkeypatch.setattr(research, "settings", dataclasses.replace(research.settings, research_attendee_cap=1))
    _, me = await signup("Asha Rao")
    await signup("Bo Chen", profile=GTM)
    await signup("Cy Das", profile=GTM)
    res = await mcp_call(client, me["connect"]["token"], "research_people", {"query": "GTM"})
    assert len(exa.queries) == 1
    assert any("used today's web lookups" in n for n in res["notes"])
    assert {p["handle"] for p in res["people"]} == {"bo", "cy"}  # still ranked on profiles


async def test_research_without_key_is_profile_only(signup, client):
    _, me = await signup("Asha Rao")
    await signup("Bo Chen", profile=GTM)
    res = await mcp_call(client, me["connect"]["token"], "research_people", {"query": "GTM"})
    assert [p["handle"] for p in res["people"]] == ["bo"]
    assert "aren't set up" in res["notes"][0]


async def test_shortlist_pick_then_reach_out_without_asking(signup, client, exa, hallway):
    c, me = await signup("Asha Rao", profile={**PROFILE, "here_for": "hire GTM people"})
    for name in ("Bo Chen", "Cy Das", "Di Roy"):
        await signup(name, profile=GTM)
    tok = me["connect"]["token"]
    await mcp_call(client, tok, "research_people", {"query": "GTM"})
    cands = [{"handle": h, "why": f"{h} built a GTM team"} for h in ("bo", "cy", "di")]
    res = await mcp_call(client, tok, "propose_shortlist", {"goal": "Hire a GTM lead", "candidates": cands, "pick": 2})
    assert res["status"] == "pending_approval"

    cards = (await c.get("/api/approvals")).json()
    card = next(a for a in cards if a["id"] == res["approval_id"])
    assert card["kind"] == "shortlist" and card["payload"]["pick"] == 2
    assert [x["handle"] for x in card["payload"]["candidates"]] == ["bo", "cy", "di"]
    assert card["payload"]["candidates"][0]["sources"][0]["url"].startswith("https://")

    url = f"/api/approvals/{res['approval_id']}"
    assert (await c.post(url, json={"approve": True, "selected": []})).status_code == 400
    assert (await c.post(url, json={"approve": True, "selected": ["bo", "cy", "di"]})).status_code == 400
    r = await c.post(url, json={"approve": True, "selected": ["bo", "di", "zed"]})
    assert r.status_code == 200, r.text
    assert [x["handle"] for x in r.json()["result"]["selected"]] == ["bo", "di"]

    # Picked: meetings with them go straight out; anyone else still asks.
    args = {"time": "15:00", "place": "Atrium", "why": "GTM hiring"}
    assert (await mcp_call(client, tok, "propose_meeting", {"with_who": "bo", **args}))["status"] != "pending_approval"
    assert (await mcp_call(client, tok, "propose_meeting", {"with_who": "cy", **args}))["status"] == "pending_approval"
    inbox = await mcp_call(client, tok, "my_inbox")
    assert "bo" in str(inbox) and "Say hi" in str(inbox)


async def test_shortlist_rejects_bad_input(signup, client):
    _, me = await signup("Asha Rao")
    await signup("Bo Chen", profile=GTM)
    tok = me["connect"]["token"]
    one = [{"handle": "bo", "why": "x"}]
    assert "2 to 10" in (await mcp_call(client, tok, "propose_shortlist", {"goal": "GTM", "candidates": one}))["error"]
    two = [*one, {"handle": "asha", "why": "me"}]  # yourself doesn't count
    res = await mcp_call(client, tok, "propose_shortlist", {"goal": "GTM", "candidates": two})
    assert "at least 2" in res["error"]
