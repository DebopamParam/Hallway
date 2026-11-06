"""Organiser tooling (#22): the admin API behind scripts/hallway_admin.py.
Every endpoint needs the admin key (which also exempts it from the CSRF
origin check); without it, a 403."""

import csv
import dataclasses
import io

import pytest

from app import main

from .conftest import ADMIN_KEY, GUARD_KEY, WORKER_KEY, mcp_call, sign_in
from .test_meeting_accept import propose

ADMIN = {"x-hallway-admin": ADMIN_KEY}

ENDPOINTS = [
    ("GET", "/api/admin/attendees", None),
    ("DELETE", "/api/admin/attendees/asha", None),
    ("POST", "/api/admin/attendees/asha/pass", {"paused": True}),
    ("GET", "/api/admin/invite", None),
    ("POST", "/api/admin/invite/rotate", {}),
    ("GET", "/api/admin/export/attendees.csv", None),
    ("GET", "/api/admin/export/meetings.csv", None),
    ("POST", "/api/admin/announce", {"text": "hi"}),
]


def use(monkeypatch, **changes):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, **changes))


@pytest.fixture
def admin(make_client):
    """A client like the CLI: admin key, no Origin header, no cookie."""
    c = make_client()
    del c.headers["origin"]
    c.headers.update(ADMIN)
    return c


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
@pytest.mark.parametrize("key", [None, "wrong", GUARD_KEY, WORKER_KEY])
async def test_admin_endpoints_need_the_admin_key(signup, client, method, path, body, key):
    await signup("Asha Rao", mode="daypass")
    headers = {"x-hallway-admin": key} if key else {}
    r = await client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403, (path, r.text)
    # and nothing happened
    assert (await client.get("/api/admin/attendees", headers=ADMIN)).json()[0]["handle"] == "asha"


async def test_a_signed_in_attendee_is_not_an_admin(signup):
    ac, _ = await signup("Asha Rao")
    assert (await ac.get("/api/admin/attendees")).status_code == 403
    assert (await ac.delete("/api/admin/attendees/asha")).status_code == 403


async def test_admin_key_skips_csrf_and_the_session_write_limit(admin, signup):
    await signup("Asha Rao")
    # No Origin header at all (a script, not a browser): fine with the key.
    for _ in range(3):
        assert (await admin.post("/api/admin/invite/rotate", json={})).status_code == 200


async def test_list_attendees_with_mode_and_agent_status(admin, signup, client):
    _, asha = await signup("Asha Rao")
    await signup("Bo Chen", mode="daypass")
    await mcp_call(client, asha["connect"]["token"], "my_inbox", {})  # Asha's agent checks in
    rows = {r["handle"]: r for r in (await admin.get("/api/admin/attendees")).json()}
    assert rows["asha"]["mode"] == "connect" and rows["asha"]["agent_status"] == "active"
    assert rows["bo"]["mode"] == "daypass" and rows["bo"]["agent_status"] == "never"
    assert rows["bo"]["pass"]["status"] == "active" and rows["asha"]["pass"] is None
    assert "contact" not in rows["asha"] and "mcp_token_hash" not in rows["asha"]


async def test_invite_spots_and_rotation(admin, signup, make_client, monkeypatch):
    await signup("Asha Rao")
    use(monkeypatch, invite_code="first", invite_limit=3)
    inv = (await admin.get("/api/admin/invite")).json()
    assert inv == {
        "invite_only": True,
        "invite_code": "first",
        "invite_url": f"{main.settings.public_url}/?invite=first",
        "invite_limit": 3,
        "joined": 1,
        "spots_left": 2,
    }
    new = (await admin.post("/api/admin/invite/rotate", json={})).json()
    assert new["invite_code"] != "first" and len(new["invite_code"]) >= 8
    assert new["invite_url"].endswith(new["invite_code"])

    c = make_client()
    r = await c.post("/api/auth/start", json={"email": "x@example.com", "invite": "first"})
    assert r.status_code == 403 and r.json()["code"] == "invite_only"  # the old code stops working
    r = await c.post("/api/auth/start", json={"email": "x@example.com", "invite": new["invite_code"]})
    assert r.status_code == 200
    assert (await c.get("/api/event")).json()["invite_only"] is True

    chosen = (await admin.post("/api/admin/invite/rotate", json={"code": "pqc-day-2"})).json()
    assert chosen["invite_code"] == "pqc-day-2"
    r = await admin.post("/api/admin/invite/rotate", json={"code": "bad code!"})
    assert r.status_code == 400


async def test_remove_attendee_cleans_up_and_frees_a_spot(admin, signup, client, hallway, world, monkeypatch):
    use(monkeypatch, invite_limit=2)
    ac, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    bc, _ = await signup("Bo Chen")
    mid = await propose(client, asha)
    assert (await bc.post(f"/api/meetings/{mid}", json={"accept": True})).status_code == 200
    assert (await admin.get("/api/admin/invite")).json()["spots_left"] == 0
    me = hallway.store.need_attendee(handle="asha")

    r = await admin.delete("/api/admin/attendees/asha")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "removed": "asha",
        "switch_agent": "deleted",
        "mattermost_user": "deactivated",
        "spots_left": 1,
    }
    assert ("delete_agent", "asha.agent") in world.calls
    assert world.users[me["mm_user_id"]].get("delete_at")
    assert hallway.store.attendee(handle="asha") is None
    for table, col in (("sessions", "attendee_id"), ("notices", "attendee_id"), ("meetings", "proposer_id")):
        assert not hallway.store.all(f"SELECT 1 FROM {table} WHERE {col} = ?", me["id"])
    # Their session and their agent's key stop working.
    assert (await ac.get("/api/me")).status_code == 401
    assert "error" in await mcp_call(client, asha["connect"]["token"], "whoami", {})
    # Bo's app still works without the meeting that had Asha in it.
    assert (await bc.get("/api/meetings")).json() == []
    assert (await bc.get("/api/notices")).status_code == 200
    assert (await admin.delete("/api/admin/attendees/asha")).status_code == 404
    # The freed spot can be taken.
    assert (await sign_in(client, "new.person@example.com")).status_code in (200, 303)


async def test_remove_leaves_a_live_agent_alone(admin, signup, world):
    await signup("Lee Park", mode="live", live_agent_name="lee.claude")
    r = (await admin.delete("/api/admin/attendees/lee")).json()
    assert r["switch_agent"].startswith("not Hallway's")
    assert not any(c[0] == "delete_agent" for c in world.calls)


async def test_remove_goes_on_when_switch_fails(admin, signup, hallway, monkeypatch):
    from app.switch import SwitchError

    await signup("Asha Rao")

    async def boom(creds):
        raise SwitchError(502, "gateway down")

    monkeypatch.setattr(hallway.switch, "delete_agent", boom)
    r = (await admin.delete("/api/admin/attendees/asha")).json()
    assert r["switch_agent"].startswith("left in place") and r["removed"] == "asha"
    assert hallway.store.attendee(handle="asha") is None


async def test_pause_resume_and_cap_a_day_pass(admin, signup, hallway):
    await signup("Bo Chen", mode="daypass")
    await signup("Asha Rao")
    r = await admin.post("/api/admin/attendees/bo/pass", json={"paused": True})
    assert r.json()["status"] == "paused"
    r = await admin.post("/api/admin/attendees/bo/pass", json={"paused": False, "cap_usd": 2.5})
    assert r.json() == {"handle": "bo", "status": "active", "cap_usd": 2.5, "spent_usd": 0}

    # A spent pass comes back when its cap is raised, and stops when lowered under its spend.
    bo = hallway.store.need_attendee(handle="bo")
    hallway.record_run(bo["id"], "test", 2.5, 1, "ran", True)
    assert hallway.day_pass(bo)["status"] == "spent"
    assert (await admin.post("/api/admin/attendees/bo/pass", json={"cap_usd": 4})).json()["status"] == "active"
    assert (await admin.post("/api/admin/attendees/bo/pass", json={"cap_usd": 1})).json()["status"] == "spent"

    assert (await admin.post("/api/admin/attendees/asha/pass", json={"paused": True})).status_code == 400
    assert (await admin.post("/api/admin/attendees/bo/pass", json={})).status_code == 400
    assert (await admin.post("/api/admin/attendees/bo/pass", json={"cap_usd": -1})).status_code == 400
    assert (await admin.post("/api/admin/attendees/nobody/pass", json={"paused": True})).status_code == 404


async def test_csv_exports(admin, signup, client):
    _, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    await signup("=cmd Bo", contact={"phone": "+91 98111 22233"})
    await propose(client, asha, with_who="cmd", why="=HYPERLINK(evil)")

    r = await admin.get("/api/admin/export/attendees.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["handle"] for x in rows] == ["asha", "cmd"]
    assert rows[1]["name"] == "'=cmd Bo"  # no formula for the spreadsheet to run
    assert "98111" not in r.text  # private contact details stay out

    rows = list(csv.DictReader(io.StringIO((await admin.get("/api/admin/export/meetings.csv")).text)))
    assert len(rows) == 1
    assert rows[0]["proposer_handle"] == "asha" and rows[0]["invitee_handle"] == "cmd"
    assert rows[0]["why"] == "'=HYPERLINK(evil)" and rows[0]["status"] == "proposed"


async def test_announce_posts_in_the_lobby_as_the_host(admin, signup, world, hallway):
    ac, _ = await signup("Asha Rao")
    r = await admin.post("/api/admin/announce", json={"text": "Lunch is served in Hall C"})
    assert r.status_code == 200 and r.json()["room"] == hallway.lobby["name"]
    msgs = (await ac.get("/api/rooms/lobby/messages")).json()["messages"]
    assert msgs[-1]["text"] == "Lunch is served in Hall C" and msgs[-1]["sender"]["kind"] == "host"
    assert (await admin.post("/api/admin/announce", json={"text": "  "})).status_code == 400


def test_the_cli_starts_and_lists_its_commands():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "hallway_admin.py"
    out = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=True).stdout
    for cmd in ("attendees", "spots", "invite", "remove", "pass", "export", "announce"):
        assert cmd in out
