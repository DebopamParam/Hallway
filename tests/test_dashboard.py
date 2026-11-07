"""The organiser dashboard (app/organiser.py): its own loopback port, a
password sign-in with a CSRF token, day pass controls, event edits, and
provider keys that never come back to the browser."""

import asyncio
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from app import config, main, mcp_server, organiser, providers, research

from .conftest import ADMIN_KEY

PORT = 8788
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "correct-horse-battery"
SECRET = "sk-ant-test-0123456789abcdefWXYZ"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A providers file of its own, the organiser password, fresh sessions, and
    the event settings put back afterwards (the dashboard changes the shared
    settings instance, as it does for real)."""
    monkeypatch.setenv("HALLWAY_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    monkeypatch.setenv("ORGANISER_PASSWORD", PASSWORD)
    for name in providers.KEYS:
        monkeypatch.delenv(name, raising=False)
    organiser._sessions.clear()
    organiser._fails.clear()
    before = dataclasses.replace(config.settings)
    agenda = list(config.AGENDA)
    yield
    config.apply_overrides({k: getattr(before, k) for k in config.EDITABLE} | {"agenda": agenda})
    mcp_server.refresh_instructions()


@pytest.fixture
async def dash(hallway):
    """An unsigned browser on the dashboard's own origin."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=organiser.build(PORT)), base_url=BASE, headers={"origin": BASE}
    ) as c:
        yield c


async def login(c: httpx.AsyncClient) -> httpx.AsyncClient:
    r = await c.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = r.json()["csrf"]
    return c


@pytest.fixture
async def org(dash):
    return await login(dash)


ENDPOINTS = [
    ("GET", "/api/overview", None),
    ("PUT", "/api/event", {"name": "X", "agenda": [], "daypass_enabled": True, "daypass_cap_usd": 1}),
    ("POST", "/api/passes/asha/issue", None),
    ("POST", "/api/passes/asha/revoke", None),
    ("POST", "/api/passes/asha", {"cap_usd": 1}),
    ("GET", "/api/providers", None),
    ("PUT", "/api/providers", {"keys": {"EXA_API_KEY": "x" * 20}}),
    ("POST", "/api/providers/EXA_API_KEY/test", None),
]


# ------------------------------------------------------------------ auth
@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
async def test_every_endpoint_needs_a_session(dash, method, path, body):
    r = await dash.request(method, path, json=body)
    assert r.status_code == 401
    assert not providers.status()["keys"]["EXA_API_KEY"]["set"]


async def test_wrong_password_and_lockout(dash):
    for _ in range(organiser.LOGIN_FAILS):
        assert (await dash.post("/api/login", json={"password": "nope"})).status_code == 401
    r = await dash.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 429  # even the right one, until the window passes


async def test_session_cookie_flags(dash):
    r = await dash.post("/api/login", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert (await dash.get("/api/session")).json()["signed_in"] is True


async def test_admin_key_signs_in_when_no_password_is_set(dash, monkeypatch):
    monkeypatch.delenv("ORGANISER_PASSWORD")
    assert (await dash.post("/api/login", json={"password": ADMIN_KEY})).status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), [e for e in ENDPOINTS if e[0] != "GET"])
async def test_writes_need_the_csrf_token(org, method, path, body):
    del org.headers["x-csrf-token"]
    r = await org.request(method, path, json=body)
    assert r.status_code == 403


async def test_writes_need_our_origin(org):
    r = await org.post("/api/passes/asha", json={"cap_usd": 1}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = await org.post("/api/login", json={"password": PASSWORD}, headers={"origin": "http://127.0.0.1:8787"})
    assert r.status_code == 403  # Hallway's own port is a different origin


async def test_refuses_other_host_names(org):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example."""
    r = await org.get("/api/overview", headers={"host": f"evil.example:{PORT}"})
    assert r.status_code == 403
    assert (await org.get("/api/overview", headers={"host": f"localhost:{PORT}"})).status_code == 200


async def test_logout_ends_the_session(org):
    assert (await org.post("/api/logout")).status_code == 200
    assert (await org.get("/api/overview")).status_code == 401


async def test_attendees_cant_reach_it_on_hallways_port(client):
    """Nothing of the dashboard is served by the public app."""
    for path in ("/api/overview", "/api/providers", "/ui/organiser.js", "/api/login"):
        r = await client.get(path)
        assert r.status_code in (404, 405), path


def test_dispatch_by_port():
    seen = []

    def app(name):
        async def a(scope, receive, send):
            seen.append(name)

        return a

    both = organiser.dispatch(app("public"), app("dash"), PORT)
    asyncio.run(both({"type": "http", "server": ("127.0.0.1", PORT)}, None, None))
    asyncio.run(both({"type": "http", "server": ("0.0.0.0", 8787)}, None, None))
    asyncio.run(both({"type": "lifespan"}, None, None))
    assert seen == ["dash", "public", "public"]


# ------------------------------------------------------------------ event
async def test_overview_lists_attendees_and_pass_spend(org, signup):
    await signup("Asha Rao", mode="daypass")
    await signup("Ben Ode")
    body = (await org.get("/api/overview")).json()
    assert body["counts"]["attendees"] == 2 and body["counts"]["modes"]["daypass"] == 1
    asha = next(a for a in body["attendees"] if a["handle"] == "asha")
    assert asha["pass"]["cap_usd"] == 0.5 and "online" in asha
    assert body["daypass"]["budget_usd"] == 0.5 and body["daypass"]["active"] == 1


async def test_edit_event_applies_at_once_and_persists(org, client):
    agenda = [{"time": "14:00", "title": "Demos", "room": "Hall C"}, {"time": "09:30", "title": "Doors", "room": ""}]
    r = await org.put(
        "/api/event",
        json={"name": "  Flip  the Switch ", "agenda": agenda, "daypass_enabled": False, "daypass_cap_usd": 0.75},
    )
    assert r.status_code == 200, r.text
    ev = (await client.get("/api/event")).json()  # the attendee app
    assert ev["name"] == "Flip the Switch" and ev["daypass_enabled"] is False
    assert [a["time"] for a in ev["agenda"]] == ["09:30", "14:00"]
    assert "Flip the Switch" in mcp_server.mcp.instructions
    assert main.hallway.store.kv_get(config.OVERRIDES_KEY)["daypass_cap_usd"] == 0.75


@pytest.mark.parametrize(
    "bad",
    [
        {"name": ""},
        {"daypass_cap_usd": -1},
        {"daypass_cap_usd": 5000},
        {"agenda": [{"time": "25:00", "title": "x"}]},
        {"agenda": [{"time": "10:00", "title": ""}]},
    ],
)
async def test_edit_event_validation(org, bad):
    body = {"name": "Event", "agenda": [], "daypass_enabled": True, "daypass_cap_usd": 1, **bad}
    r = await org.put("/api/event", json=body)
    assert r.status_code == 422
    assert "error" in r.json()


# ------------------------------------------------------------------ passes
async def test_issue_cap_pause_and_revoke(org, signup):
    await signup("Asha Rao")
    r = await org.post("/api/passes/asha/issue")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "daypass" and r.json()["pass"]["status"] == "active"
    r = await org.post("/api/passes/asha", json={"cap_usd": 2})
    assert r.json()["pass"]["cap_usd"] == 2
    r = await org.post("/api/passes/asha", json={"paused": True})
    assert r.json()["pass"]["status"] == "paused"

    a = main.hallway.store.attendee(handle="asha")
    token = main.hallway.store.worker_token(a["id"])
    assert main.hallway.store.mcp_attendee(token)
    r = await org.post("/api/passes/asha/revoke")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "connect" and r.json()["pass"] is None
    assert main.hallway.store.mcp_attendee(token) is None  # a run in progress stops at once
    assert (await org.post("/api/passes/asha/revoke")).status_code == 400

    r = await org.post("/api/passes/asha/issue")  # comes back with its old cap and spend
    assert r.json()["pass"]["cap_usd"] == 2


async def test_pass_validation(org, signup):
    await signup("Asha Rao", mode="daypass")
    assert (await org.post("/api/passes/asha", json={"cap_usd": -1})).status_code == 422
    assert (await org.post("/api/passes/asha", json={})).status_code == 400
    assert (await org.post("/api/passes/nobody/issue")).status_code == 404


async def test_no_pass_when_day_passes_are_off(org, signup):
    await signup("Asha Rao")
    config.apply_overrides({"daypass_enabled": False})
    r = await org.post("/api/passes/asha/issue")
    assert r.status_code == 400 and "aren't available" in r.json()["error"]


# ------------------------------------------------------------------ keys
async def test_keys_are_write_only(org):
    r = await org.put("/api/providers", json={"keys": {"ANTHROPIC_API_KEY": SECRET}})
    assert r.status_code == 200
    assert SECRET not in r.text
    k = r.json()["keys"]["ANTHROPIC_API_KEY"]
    assert k == {"label": k["label"], "set": True, "source": "dashboard", "last4": "WXYZ"}
    for path in ("/api/providers", "/api/overview", "/api/session"):
        assert SECRET not in (await org.get(path)).text
    assert providers.get("ANTHROPIC_API_KEY") == SECRET


async def test_providers_file_is_0600(org):
    await org.put("/api/providers", json={"keys": {"EXA_API_KEY": SECRET}})
    mode = stat.S_IMODE(os.stat(providers.path()).st_mode)
    assert mode == 0o600


async def test_invalid_key_is_not_echoed(org):
    bad = SECRET + " with spaces"
    r = await org.put("/api/providers", json={"keys": {"EXA_API_KEY": bad}})
    assert r.status_code == 400 and bad not in r.text
    r = await org.put("/api/providers", json={"keys": {"EXA_API_KEY": 12345678901234}})  # wrong type: a 422
    assert r.status_code == 422 and "12345678901234" not in r.text
    r = await org.put("/api/providers", json={"keys": {"OPENAI_API_KEY": SECRET}})
    assert r.status_code == 400 and SECRET not in r.text


async def test_options_validation(org):
    r = await org.put("/api/providers", json={"options": {"DAYPASS_MODEL": "bad model; rm -rf"}})
    assert r.status_code == 400
    r = await org.put("/api/providers", json={"options": {"DAYPASS_PRICE_PER_MTOK": "cheap"}})
    assert r.status_code == 400
    r = await org.put(
        "/api/providers", json={"options": {"DAYPASS_MODEL": "vercel:deepseek/x", "DAYPASS_PRICE_PER_MTOK": "0.1,0.4"}}
    )
    assert r.json()["options"]["DAYPASS_MODEL"] == {"value": "vercel:deepseek/x", "source": "dashboard"}


async def test_clearing_a_key_falls_back_to_env(org, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "env-key-000000000000AAAA")
    await org.put("/api/providers", json={"keys": {"EXA_API_KEY": SECRET}})
    assert providers.get("EXA_API_KEY") == SECRET
    r = await org.put("/api/providers", json={"keys": {"EXA_API_KEY": ""}})
    assert r.json()["keys"]["EXA_API_KEY"] == {**r.json()["keys"]["EXA_API_KEY"], "source": "env", "last4": "AAAA"}
    assert json.loads(providers.path().read_text()) == {}


async def test_research_uses_the_dashboard_key(org, monkeypatch):
    monkeypatch.setattr(research, "settings", dataclasses.replace(research.settings, exa_api_key=""))
    assert not research.enabled()
    await org.put("/api/providers", json={"keys": {"EXA_API_KEY": SECRET}})
    assert research.enabled() and research.api_key() == SECRET


async def test_test_connection(org, monkeypatch):
    sent = {}

    async def fake_probe(name, key):
        sent[name] = key
        return {"ok": True, "detail": "The key works."}

    monkeypatch.setattr(organiser, "probe", fake_probe)
    r = await org.post("/api/providers/ANTHROPIC_API_KEY/test")
    assert r.json() == {"ok": False, "detail": "Not set."} and not sent
    await org.put("/api/providers", json={"keys": {"ANTHROPIC_API_KEY": SECRET}})
    r = await org.post("/api/providers/ANTHROPIC_API_KEY/test")
    assert r.json()["ok"] and sent == {"ANTHROPIC_API_KEY": SECRET} and SECRET not in r.text
    assert (await org.post("/api/providers/NOPE/test")).status_code == 404


async def test_probe_reports_without_the_key(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == SECRET
        return httpx.Response(401, json={"error": f"bad key {SECRET}"})

    real = httpx.AsyncClient
    monkeypatch.setattr(organiser.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = await organiser.probe("ANTHROPIC_API_KEY", SECRET)
    assert r == {"ok": False, "detail": "The provider refused this key."}


def test_worker_reads_the_providers_file(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daypass"))
    try:
        import worker  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    f = tmp_path / "p.json"
    monkeypatch.setattr(worker, "PROVIDERS_FILE", f)
    monkeypatch.setattr(worker, "ENV_AT_START", {"ANTHROPIC_API_KEY": "from-env", "VERCEL_AI_GATEWAY_API_KEY": None})
    monkeypatch.setattr(worker, "MODEL", worker.MODEL)
    monkeypatch.setattr(worker, "PRICE_PER_MTOK", worker.PRICE_PER_MTOK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    f.write_text(json.dumps({"ANTHROPIC_API_KEY": SECRET, "DAYPASS_MODEL": "claude-sonnet-5-5"}))
    worker.load_providers()
    assert os.environ["ANTHROPIC_API_KEY"] == SECRET and worker.MODEL == "claude-sonnet-5-5"
    f.write_text("{}")
    worker.load_providers()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"
