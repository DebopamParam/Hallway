"""Invite-only sign-up (INVITE_CODE), the demo cap (INVITE_LIMIT) and the
organiser's persona bypass (x-hallway-admin)."""
import dataclasses
from urllib.parse import urlparse

import pytest

from app import main

from .conftest import PROFILE, register

CODE = "bharat-2026"
ADMIN = {"x-hallway-admin": "admin-key"}


@pytest.fixture
def invite(monkeypatch):
    def use(**changes):
        base = dict(invite_code=CODE, invite_limit=0, demo_video_url="", guard_api_key="admin-key")
        monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, **{**base, **changes}))
    return use


async def start(client, email, invite=None, headers=None):
    return await client.post("/api/auth/start", json={"email": email, "invite": invite}, headers=headers or {})


async def join(client, name, email, invite=CODE, headers=None):
    """auth/start with the invite -> link -> register. Returns the register response."""
    r = await start(client, email, invite, headers)
    assert r.status_code == 200, r.text
    link = urlparse(r.json()["dev_link"])
    await client.get(f"{link.path}?{link.query}")
    return await client.post("/api/register", json={"name": name, "profile": PROFILE}, headers=headers or {})


# ------------------------------------------------------------------ invite
async def test_no_invite_settings_means_open(client):
    ev = (await client.get("/api/event")).json()
    assert ev["invite_only"] is False and ev["spots_left"] is None and ev["demo_video_url"] is None
    r = await start(client, "a@example.com")
    assert r.status_code == 200 and r.json()["dev_link"]


@pytest.mark.parametrize("code", [None, "", "wrong", CODE.upper(), CODE + "x"])
async def test_missing_or_wrong_invite_is_refused(client, invite, hallway, code):
    invite()
    r = await start(client, "new@example.com", code)
    assert r.status_code == 403
    assert r.json() == {"error": main.INVITE_ONLY, "code": "invite_only"}
    assert not hallway.store.all("SELECT key FROM kv WHERE key LIKE 'magic:%'")


async def test_correct_invite_gets_a_link(make_client, invite):
    invite()
    c = make_client()
    r = await join(c, "Asha Rao", "asha@example.com", invite=f"  {CODE} ")
    assert r.status_code == 200 and r.json()["handle"] == "asha"


async def test_existing_attendee_needs_no_invite(make_client, invite):
    await register(make_client(), "Asha Rao", email="asha@example.com")
    invite()
    r = await start(make_client(), "asha@example.com", None)
    assert r.status_code == 200 and r.json()["dev_link"]
    r = await start(make_client(), "asha@example.com", "stale-code")
    assert r.status_code == 200 and r.json()["dev_link"]


async def test_demo_mode_existing_email_still_gets_already_joined(make_client, invite):
    await register(make_client(), "Asha Rao", email="asha@example.com")
    invite(signin_mode="demo")
    r = await start(make_client(), "asha@example.com", None)
    assert r.status_code == 200 and r.json()["already_joined"] is True and r.json()["dev_link"] is None


async def test_email_mode_refusal_goes_to_the_inbox(make_client, invite, monkeypatch):
    """No enumeration: the API answers the same; the reason arrives by email."""
    sent = []

    class Outbox:
        async def send(self, to, subject, text):
            sent.append((to, text))
    monkeypatch.setattr(main.mailer, "make_sender", lambda _s: Outbox())
    invite(signin_mode="email", demo_video_url="https://example.com/demo")
    refused = await start(make_client(), "new@example.com", None)
    invited = await start(make_client(), "other@example.com", CODE)
    assert refused.status_code == invited.status_code == 200
    assert refused.json() == invited.json() == {"sent": True, "dev_link": None}
    assert "invite only" in sent[0][1] and "/auth/magic" not in sent[0][1]
    assert "/auth/magic?t=" in sent[1][1]


async def test_event_exposes_invite_state(client, invite):
    invite(invite_limit=10, demo_video_url="https://example.com/demo")
    ev = (await client.get("/api/event")).json()
    assert ev["invite_only"] is True and ev["invite_limit"] == 10 and ev["spots_left"] == 10
    assert ev["demo_video_url"] == "https://example.com/demo"


# ------------------------------------------------------------------- limit
async def test_limit_reached(make_client, invite, hallway):
    invite(invite_limit=2, demo_video_url="https://example.com/demo")
    for i, name in enumerate(["Asha Rao", "Bo Chen"]):
        r = await join(make_client(), name, f"p{i}@example.com")
        assert r.status_code == 200, r.text
    assert (await make_client().get("/api/event")).json()["spots_left"] == 0
    r = await start(make_client(), "late@example.com", CODE)
    assert r.status_code == 409
    assert r.json() == {"error": "This demo is full: all 2 spots are taken.", "code": "demo_full",
                        "demo_video_url": "https://example.com/demo"}
    # People who already joined can still sign in.
    r = await start(make_client(), "p0@example.com", None)
    assert r.status_code == 200 and r.json()["dev_link"]


async def test_limit_without_invite_code(make_client, invite):
    invite(invite_code="", invite_limit=1)
    assert (await join(make_client(), "Asha Rao", "a@example.com", invite=None)).status_code == 200
    r = await start(make_client(), "b@example.com")
    assert r.status_code == 409 and r.json()["code"] == "demo_full"


async def test_limit_enforced_again_at_register(make_client, invite, hallway):
    """Two links issued while one spot was left: only the first to register gets in."""
    invite(invite_limit=1)
    first, second = make_client(), make_client()
    links = [urlparse((await start(c, e, CODE)).json()["dev_link"])
             for c, e in ((first, "a@example.com"), (second, "b@example.com"))]
    for c, link in zip((first, second), links):
        await c.get(f"{link.path}?{link.query}")
    assert (await first.post("/api/register", json={"name": "Asha Rao"})).status_code == 200
    r = await second.post("/api/register", json={"name": "Bo Chen"})
    assert r.status_code == 409 and r.json()["code"] == "demo_full"
    assert len(hallway.store.attendees()) == 1


async def test_personas_dont_count(make_client, invite, hallway):
    invite(invite_limit=1)
    for i in range(3):
        r = await join(make_client(), f"Persona{i} Test", f"persona{i}@example.com", invite=None, headers=ADMIN)
        assert r.status_code == 200, r.text
    rows = hallway.store.all("SELECT is_persona FROM attendees")
    assert [r["is_persona"] for r in rows] == [1, 1, 1]
    assert (await make_client().get("/api/event")).json()["spots_left"] == 1
    assert (await join(make_client(), "Asha Rao", "asha@example.com")).status_code == 200
    assert (await make_client().get("/api/event")).json()["spots_left"] == 0


# --------------------------------------------------------- persona bypass
async def test_persona_bypass_skips_invite_and_full_demo(make_client, invite, hallway):
    invite(invite_limit=1)
    assert (await join(make_client(), "Asha Rao", "asha@example.com")).status_code == 200
    r = await join(make_client(), "Priya Raman", "priya@example.com", invite=None, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert hallway.store.attendee(email="priya@example.com")["is_persona"] == 1


@pytest.mark.parametrize("mode", ["demo", "email"])
async def test_persona_bypass_returns_link_in_every_mode(make_client, invite, mode):
    """Seeding works on the public demo, and re-running it signs personas back in."""
    invite(signin_mode=mode)
    await join(make_client(), "Priya Raman", "priya@example.com", invite=None, headers=ADMIN)
    r = await start(make_client(), "priya@example.com", None, ADMIN)
    assert r.status_code == 200 and r.json()["dev_link"]


@pytest.mark.parametrize("headers", [{}, {"x-hallway-admin": "wrong"}, {"x-hallway-admin": ""}])
async def test_bad_admin_header_gets_no_bypass(client, invite, headers):
    invite()
    r = await start(client, "mallory@example.com", None, headers)
    assert r.status_code == 403


@pytest.mark.parametrize("key", ["", "change-me"])
async def test_no_bypass_with_unset_or_default_key(client, invite, key):
    invite(guard_api_key=key)
    r = await start(client, "mallory@example.com", None, {"x-hallway-admin": key})
    assert r.status_code == 403


async def test_admin_header_alone_cannot_register(client, invite, hallway):
    invite()
    r = await client.post("/api/register", json={"name": "Mallory"}, headers=ADMIN)
    assert r.status_code == 401  # a session from a sign-in link comes first
