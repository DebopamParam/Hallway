"""Startup against the fake Switch, and the person+agent sync loop."""

from .conftest import ADMIN_KEY


async def test_startup_registers_host_and_lobby(hallway, world, client):
    assert ("register_agent", "hallway") in world.calls
    assert hallway.lobby["name"] == "hallway-lobby" and hallway.lobby["kind"] == "lobby"
    assert hallway.store.kv_get("host")["id"] == hallway.host.id
    rules = [c for c in world.calls if c[0] == "post"]
    assert len(rules) == 1 and "Welcome to the Test Event hallway" in rules[0][1][2]
    assert (await client.get("/health")).json() == {"ok": True}  # public: nothing else
    health = (await client.get("/health", headers={"x-hallway-admin": ADMIN_KEY})).json()
    assert {"checks", "errors", "rate_limited", "slow", "bad_key", "p99_ms"} <= set(health["guard"])
    assert (await client.get("/")).status_code == 200


async def test_restart_reuses_host_and_lobby(hallway, world):
    await hallway.start()
    assert [c[0] for c in world.calls if c[0] in ("register_agent", "create_room")] == [
        "register_agent",
        "create_room",
    ]  # from the first start only
    hallway._sync_task.cancel()


async def test_sync_keeps_person_and_agent_together(hallway, world, signup):
    await signup("Asha Rao")
    ch = world.channels[world.rooms[hallway.lobby["id"]]]
    ch["members"].discard("u_asha.agent")
    await hallway.sync_once()
    assert "u_asha.agent" in ch["members"]
    ch["members"].discard("u_asha")
    await hallway.sync_once()
    assert "u_asha" in ch["members"]


async def test_sync_repairs_attendee_missing_from_lobby(hallway, world, signup):
    """Switch can't resolve a user who isn't in the Mattermost team (seen live
    when the team hit its 50-member cap): add_users answers 200 {"ok": false,
    "unresolved": [...]}. A pair that missed the lobby must be repaired by sync."""
    world.unresolvable = {"asha", "asha.agent"}
    pc, _ = await signup("Asha Rao")
    world.unresolvable = set()  # Switch catches up
    await hallway.sync_once()
    r = await pc.get("/api/rooms/hallway-lobby/messages")
    assert r.status_code == 200
    ch = world.channels[world.rooms[hallway.lobby["id"]]]
    assert {"u_asha", "u_asha.agent"} <= ch["members"]


async def test_index_versions_the_app_assets(client):
    """A CDN can't hand out yesterday's app.js: each deploy's index points at new URLs."""
    import hashlib
    import re

    from app.main import STATIC

    html = (await client.get("/")).text
    for name in ("app.js", "app.css", "theme.js"):
        v = re.search(rf'"/static/{re.escape(name)}\?v=([0-9a-f]+)"', html)
        assert v and v.group(1) == hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        assert (await client.get(f"/static/{name}?v={v.group(1)}")).status_code == 200


async def test_index_has_a_link_preview_card(client):
    """A pasted link unfurls with a large card whose image URL is absolute."""
    html = (await client.get("/")).text
    assert '<meta property="og:image" content="http://testserver/static/brand/og.png">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert "__HALLWAY_URL__" not in html
    assert (await client.get("/static/brand/og.png")).status_code == 200


async def test_index_answers_head(client):
    """Link previewers and uptime checks send HEAD / first; a 404 reads as a dead link."""
    r = await client.head("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
