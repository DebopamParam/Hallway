"""Agent-made cards (show_card, a shortlist's own page) and the setup prompt."""

from .conftest import PROFILE, mcp_call

PAGE = "<h1>Today</h1><p>Call me on +91 98111 22233</p><script>document.title='x'</script>"


async def test_show_card_is_sandboxed_and_only_for_its_owner(signup):
    c, me = await signup("Asha Rao", contact={"phone": "+91 98111 22233"})
    other, _ = await signup("Bo Chen")
    res = await mcp_call(c, me["connect"]["token"], "show_card", {"title": "Your day", "html": PAGE})
    assert res["status"] == "shown"
    cards = (await c.get("/api/cards")).json()
    assert [(x["id"], x["title"]) for x in cards] == [(res["card_id"], "Your day")]

    r = await c.get(f"/cards/{res['card_id']}")
    assert r.status_code == 200 and "<script>" in r.text
    assert "98111" not in r.text  # the guard redacts private values in cards too
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert "connect-src" not in csp and "default-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert r.headers["x-frame-options"] == "SAMEORIGIN"

    assert (await other.get(f"/cards/{res['card_id']}")).status_code == 404
    assert (await other.get("/api/cards")).json() == []
    # The app itself may frame same-origin pages only.
    assert "frame-src 'self'" in (await c.get("/")).headers["content-security-policy"]


async def test_show_card_limits(signup):
    c, me = await signup("Asha Rao")
    tok = me["connect"]["token"]
    assert "needs some HTML" in (await mcp_call(c, tok, "show_card", {"title": "x", "html": "  "}))["error"]
    assert "too big" in (await mcp_call(c, tok, "show_card", {"title": "x", "html": "a" * 100_001}))["error"]


async def test_shortlist_with_own_page(signup):
    c, me = await signup("Asha Rao", profile={**PROFILE, "here_for": "hire"})
    await signup("Bo Chen")
    await signup("Cy Das")
    cands = [{"handle": "bo", "why": "a"}, {"handle": "cy", "why": "b"}]
    res = await mcp_call(
        c, me["connect"]["token"], "propose_shortlist", {"goal": "Hire", "candidates": cands, "html": "<p>compare</p>"}
    )
    ap = next(a for a in (await c.get("/api/approvals")).json() if a["id"] == res["approval_id"])
    card = ap["payload"]["card"]
    assert (await c.get(f"/cards/{card}")).text == "<p>compare</p>"
    assert [x["approval_id"] for x in (await c.get("/api/cards")).json()] == [res["approval_id"]]


async def test_setup_prompts_carry_the_key_once_issued(signup):
    c, me = await signup("Asha Rao")
    tok = me["connect"]["token"]
    cc, cx = me["connect"]["setup"]["cc"], me["connect"]["setup"]["cx"]
    assert tok in cc and "claude mcp add --transport http hallway" in cc and "claude mcp remove hallway" in cc
    assert tok in cx and "[mcp_servers.hallway]" in cx and "codex resume --last" in cx
    assert "call whoami" in cc and "call whoami" in cx
    later = (await c.get("/api/me")).json()["connect"]["setup"]["cc"]
    assert tok not in later and "<your-hallway-key>" in later  # stored hashed: the app fills it in
