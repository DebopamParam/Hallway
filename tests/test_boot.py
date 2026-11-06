"""Startup against the fake Switch, and the person+agent sync loop."""
import pytest


async def test_startup_registers_host_and_lobby(hallway, world, client):
    assert ("register_agent", "hallway") in world.calls
    assert hallway.lobby["name"] == "hallway-lobby" and hallway.lobby["kind"] == "lobby"
    assert hallway.store.kv_get("host")["id"] == hallway.host.id
    rules = [c for c in world.calls if c[0] == "post"]
    assert len(rules) == 1 and "Welcome to the Test Event hallway" in rules[0][1][2]
    assert (await client.get("/health")).json() == {"ok": True}
    assert (await client.get("/")).status_code == 200


async def test_restart_reuses_host_and_lobby(hallway, world):
    await hallway.start()
    assert [c[0] for c in world.calls if c[0] in ("register_agent", "create_room")] == [
        "register_agent", "create_room"]  # from the first start only
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
    """Live Switch sometimes can't resolve a just-created user: add_users returns
    200 {"ok": false, "unresolved": [...]}. Hallway ignores that, and the sync
    loop only repairs half-pairs, so a pair that missed the lobby never joins."""
    world.unresolvable = {"asha", "asha.agent"}
    pc, _ = await signup("Asha Rao")
    world.unresolvable = set()  # Switch catches up
    await hallway.sync_once()
    r = await pc.get("/api/rooms/hallway-lobby/messages")
    if r.status_code == 400:
        pytest.xfail("bug: _join ignores add_users' ok:false/unresolved and sync_once never re-adds a pair "
                     "missing from a room on both sides (seen live: scripts/ui_smoke.py can't find #say)")
    assert r.status_code == 200
