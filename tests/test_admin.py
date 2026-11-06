"""Separate secrets (#11): HALLWAY_ADMIN_KEY for the organiser, GUARD_API_KEY for
Switch's guard calls, DAYPASS_WORKER_KEY for the worker; no localhost bypass
on a deployed instance; refuse to start a public instance with weak keys."""
import dataclasses

import pytest

from app import config, main

from .conftest import ADMIN_KEY, GUARD_KEY, WORKER_KEY


def use(monkeypatch, **changes):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, **changes))


class FakeGateway:
    calls: list = []

    async def login(self):
        pass

    async def put_trust_settings(self, endpoint, policy, key):
        FakeGateway.calls.append((endpoint, policy, key))
        return {"ok": True}

    async def set_agent_greetings(self, on):
        return 0

    async def close(self):
        pass


@pytest.fixture
def gateway(monkeypatch):
    FakeGateway.calls = []
    monkeypatch.setattr(main, "GatewayClient", FakeGateway)
    return FakeGateway


# -------------------------------------------------------------- enable-guard
async def test_enable_guard_needs_admin_key_even_from_localhost(client, gateway):
    # The ASGI test client connects from 127.0.0.1, like every visitor behind a tunnel.
    r = await client.post("/api/admin/enable-guard")
    assert r.status_code == 403
    assert gateway.calls == []


@pytest.mark.parametrize("sent", ["wrong", "", GUARD_KEY, WORKER_KEY])
async def test_enable_guard_refuses_other_keys(client, gateway, sent):
    r = await client.post("/api/admin/enable-guard", headers={"x-hallway-admin": sent})
    assert r.status_code == 403


async def test_enable_guard_with_admin_key_sends_the_guard_key_to_switch(client, gateway):
    r = await client.post("/api/admin/enable-guard", headers={"x-hallway-admin": ADMIN_KEY})
    assert r.status_code == 200, r.text
    assert gateway.calls[0][2] == GUARD_KEY  # Switch gets the guard key, never the admin key


async def test_localhost_exception_only_on_a_dev_box(client, gateway, monkeypatch):
    client.headers["origin"] = "http://localhost:8787"
    use(monkeypatch, hallway_env="dev", public_url="http://localhost:8787")
    assert (await client.post("/api/admin/enable-guard")).status_code == 200
    use(monkeypatch, hallway_env="", public_url="http://localhost:8787")
    assert (await client.post("/api/admin/enable-guard")).status_code == 403
    use(monkeypatch, hallway_env="dev", public_url="https://hallway.example.com")
    assert (await client.post("/api/admin/enable-guard")).status_code == 403


# ------------------------------------------------------------ persona bypass
async def test_guard_key_is_not_an_admin_key(client, monkeypatch):
    use(monkeypatch, invite_code="code")
    r = await client.post("/api/auth/start", json={"email": "m@example.com"}, headers={"x-hallway-admin": GUARD_KEY})
    assert r.status_code == 403
    r = await client.post("/api/auth/start", json={"email": "m@example.com"}, headers={"x-hallway-admin": ADMIN_KEY})
    assert r.status_code == 200 and r.json()["dev_link"]


async def test_admin_key_falls_back_to_guard_key(client, monkeypatch):
    """Old .env files without HALLWAY_ADMIN_KEY keep working on localhost."""
    use(monkeypatch, invite_code="code", admin_key="", public_url="http://localhost:8787")
    r = await client.post("/api/auth/start", json={"email": "m@example.com"}, headers={"x-hallway-admin": GUARD_KEY})
    assert r.status_code == 200


# -------------------------------------------------------------------- worker
async def test_worker_endpoints_refuse_guard_and_admin_keys(client):
    for sent in (GUARD_KEY, ADMIN_KEY):
        assert (await client.get("/internal/daypass/due", headers={"x-hallway-worker": sent})).status_code == 401
    assert (await client.get("/internal/daypass/due", headers={"x-hallway-worker": WORKER_KEY})).status_code == 200


async def test_guard_refuses_admin_key(client):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert (await client.post("/guardrails/check", json=body, headers={"x-flintai-api-key": ADMIN_KEY})).status_code == 401


# ------------------------------------------------------------ startup checks
PUBLIC = dict(public_url="https://hallway.example.com", guard_api_key="g" * 24, admin_key="a" * 24,
              daypass_worker_key="w" * 24)


def keys(**kw) -> config.Settings:
    return dataclasses.replace(config.settings, **{**PUBLIC, **kw})


def test_distinct_keys_start_on_a_public_url():
    assert config.check_keys(keys()) == []


@pytest.mark.parametrize("name", ["guard_api_key", "admin_key", "daypass_worker_key"])
@pytest.mark.parametrize("value", ["", "change-me", "  "])
def test_missing_or_placeholder_key_refused_on_public_url(name, value):
    with pytest.raises(RuntimeError, match="random secret"):
        config.check_keys(keys(**{name: value}))


@pytest.mark.parametrize("a, b", [("guard_api_key", "admin_key"), ("guard_api_key", "daypass_worker_key"),
                                  ("admin_key", "daypass_worker_key")])
def test_shared_keys_refused_on_public_url(a, b):
    with pytest.raises(RuntimeError, match="different secrets"):
        config.check_keys(keys(**{a: "same" * 6, b: "same" * 6}))


def test_hallway_env_dev_does_not_excuse_a_public_url():
    with pytest.raises(RuntimeError):
        config.check_keys(keys(hallway_env="dev", admin_key=""))


@pytest.mark.parametrize("url", ["http://localhost:8787", "http://127.0.0.1:8799"])
def test_localhost_falls_back_with_a_deprecation_warning(url):
    warnings = config.check_keys(keys(public_url=url, admin_key="", daypass_worker_key=""))
    assert len(warnings) == 2 and all("deprecated" in w for w in warnings)
    s = keys(public_url=url, admin_key="", daypass_worker_key="")
    assert config.admin_key(s) == config.worker_key(s) == "g" * 24


def test_placeholder_keys_never_match():
    s = keys(public_url="http://localhost", guard_api_key="change-me", admin_key="", daypass_worker_key="")
    assert config.admin_key(s) == config.worker_key(s) == ""


async def test_app_refuses_to_start_with_shared_keys(monkeypatch):
    use(monkeypatch, admin_key=GUARD_KEY)
    with pytest.raises(RuntimeError, match="different secrets"):
        async with main.lifespan(main.app):
            pass

