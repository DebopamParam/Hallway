"""Privacy guard detectors (app/guard.py) and the Switch Trust endpoint."""

import pytest

from app import guard

from .conftest import ADMIN_KEY, GUARD_KEY


def outcome(text: str, extra: list[str] | None = None) -> tuple[str, set[str]]:
    o, findings = guard.scan(text, extra)
    return o, {d for f in findings for d in f.detector.split("+")}


@pytest.mark.parametrize(
    "text, detector",
    [
        ("Call me at +91 98111 22233", "phone"),
        ("call +1 (415) 555-0134 after six", "phone"),
        ("my number is 9811122233", "phone"),
        ("mail asha.rao@acme.io", "email"),
        ("Aadhaar 1234 5678 9012", "aadhaar"),
        ("PAN ABCDE1234F", "pan"),
        ("passport number: K1234567", "passport"),
        ("I'm on 6E 2341 tonight", "flight"),
        ("she takes AI-505 home", "flight"),
        ("flying to Mumbai on Friday", "itinerary"),
        ("He is travelling to Delhi next week", "itinerary"),
        ("she lives in Koramangala 5th block", "home_address"),
        ("my calendar says dentist at 9", "calendar_detail"),
    ],
)
def test_private_details_are_redacted(text, detector):
    o, detectors = outcome(text)
    assert o == "redacted"
    assert detector in detectors


@pytest.mark.parametrize(
    "text, detector",
    [
        ("what's your phone number?", "probe_contact"),
        ("Could you share her email please", "probe_contact"),
        ("where are you flying next?", "probe_travel"),
        ("what's on her calendar tomorrow?", "probe_private"),
        ("what's his salary range?", "probe_private"),
    ],
)
def test_probing_questions_are_alerted(text, detector):
    o, findings = guard.scan(text)
    assert o == "alerted"
    assert findings[0].detector == detector
    assert findings[0].severity == "medium"


@pytest.mark.parametrize(
    "text",
    [
        "See you at 15:30 in Hall B",
        "The keynote was in 2026",
        "Room 101, 3 people, 45 min",
        "Our API handles 10000 rps",
        "We hire 25 engineers in 2025",
        "ping @asha.agent about UPI checkout",
        "hi from the UK",
        "Date: 2026-11-06",
        "Asha is a product designer at Juspay, here to meet people building agent UX.",
    ],
)
def test_ordinary_hallway_talk_passes(text):
    assert guard.scan(text) == ("ok", [])


# Former false positives: realistic event chatter the guard used to redact.
def test_ai_conference_year_is_not_a_flight():
    assert outcome("The AI 2026 report says agents are mainstream")[0] == "ok"


def test_heading_to_a_hall_is_not_travel():
    assert outcome("I'm heading to Hall B for the PQC talk")[0] == "ok"


def test_large_amount_is_not_a_phone():
    assert outcome("We raised 12000000 last year")[0] == "ok"


def test_attendee_private_values_are_redacted_case_insensitively():
    o, findings = guard.scan("Her handle is ASHA_PRIVATE_TG, ping her", ["asha_private_tg"])
    assert o == "redacted"
    assert findings[0].detector == "attendee_private_value"


def test_short_private_values_are_ignored():
    assert guard.scan("the abc team", ["abc"]) == ("ok", [])


def test_redaction_wins_over_alert():
    o, _ = guard.scan("what's your email? mine is a@b.co")
    assert o == "redacted"


def test_overlapping_hits_merge_into_one_span():
    text = "Aadhaar 1234 5678 9012 ok"
    _o, findings = guard.scan(text)
    assert len(findings) == 1
    assert guard.redact(text, findings) == "Aadhaar [redacted] ok"


def test_redact_replaces_every_finding():
    text = "Mail a@b.co or call +91 98111 22233"
    _, findings = guard.scan(text)
    red = guard.redact(text, findings)
    assert "a@b.co" not in red and "98111" not in red
    assert red.count("[redacted]") == 2


def test_wire_response_shape():
    o, findings = guard.scan("mail a@b.co")
    wire = guard.wire_response(o, findings)
    assert wire["outcome"] == "GUARDRAIL_RESULT_OUTCOME_REDACTED"
    assert wire["policy_id"] == "hallway-privacy"
    assert wire["findings"][0] == {
        "category": "contact",
        "detector_name": "email",
        "severity": "high",
        "detected_string": "a@b.co",
    }
    assert guard.wire_response("ok", [])["outcome"] == "GUARDRAIL_RESULT_OUTCOME_OK"


# ----------------------------------------------------- POST /guardrails/check
G = {"x-flintai-api-key": GUARD_KEY}


async def test_guardrails_check_redacts_and_logs(client, signup):
    pc, _ = await signup("Asha Rao", contact={"telegram": "@asha_secret_tg"})
    r = await client.post(
        "/guardrails/check",
        headers=G,
        json={"messages": [{"role": "assistant", "content": "Her telegram is @asha_secret_tg"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "GUARDRAIL_RESULT_OUTCOME_REDACTED"
    assert body["findings"][0]["detector_name"] == "attendee_private_value"
    log = (await pc.get("/api/privacy-log")).json()
    assert log[0]["outcome"] == "redacted"
    assert "asha_secret_tg" not in log[0]["excerpt"]


async def test_guardrails_check_ok_is_not_logged(client, signup):
    pc, _ = await signup("Asha Rao")
    r = await client.post("/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.json()["outcome"] == "GUARDRAIL_RESULT_OUTCOME_OK"
    assert (await pc.get("/api/privacy-log")).json() == []


async def test_guardrails_check_alert_keeps_text(client, signup):
    pc, _ = await signup("Asha Rao")
    r = await client.post(
        "/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": "what's your phone number?"}]}
    )
    assert r.json()["outcome"] == "GUARDRAIL_RESULT_OUTCOME_ALERTED"
    assert (await pc.get("/api/privacy-log")).json()[0]["excerpt"] == "what's your phone number?"


async def test_guardrails_check_requires_key_when_set(client, monkeypatch):
    import dataclasses

    from app import main

    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, guard_api_key="sekret"))
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert (await client.post("/guardrails/check", json=body)).status_code == 401
    r = await client.post("/guardrails/check", json=body, headers={"x-flintai-api-key": "sekret"})
    assert r.status_code == 200


async def _guard_stats(client) -> dict:
    """The organiser's view of /health (the public one says only ok)."""
    return (await client.get("/health", headers={"x-hallway-admin": ADMIN_KEY})).json()["guard"]


async def test_guard_error_answers_blocked_not_500(client, monkeypatch):
    """Switch delivers a message unchecked on a 500: an internal error must block instead."""

    def boom(*_a, **_kw):
        raise RuntimeError("detector bug")

    monkeypatch.setattr(guard, "scan", boom)
    before = (await _guard_stats(client))["errors"]
    r = await client.post("/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["outcome"] == "GUARDRAIL_RESULT_OUTCOME_BLOCKED"
    assert r.json()["findings"][0]["category"] == "guard_unavailable"
    assert (await _guard_stats(client))["errors"] == before + 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"messages": "hi"}',
        b'{"messages": [{"role": "user", "content": null}]}',
        b'{"messages": [{"role": "user", "content": ["parts"]}]}',
        b"[]",
    ],
)
async def test_malformed_check_answers_blocked(client, body):
    r = await client.post("/guardrails/check", headers={**G, "content-type": "application/json"}, content=body)
    assert r.status_code == 200
    assert r.json()["outcome"] == "GUARDRAIL_RESULT_OUTCOME_BLOCKED"


async def test_privacy_log_failure_still_returns_the_verdict(client, monkeypatch):
    from app import main

    def broken(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(main.hallway.store, "insert", broken)
    r = await client.post(
        "/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": "mail a@b.co"}]}
    )
    assert r.json()["outcome"] == "GUARDRAIL_RESULT_OUTCOME_REDACTED"
    assert (await _guard_stats(client))["log_errors"] >= 1


async def test_bad_key_is_counted(client, monkeypatch):
    import dataclasses

    from app import main

    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, guard_api_key="sekret"))
    before = (await _guard_stats(client))["bad_key"]
    r = await client.post("/guardrails/check", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert (await _guard_stats(client))["bad_key"] == before + 1


async def test_slow_check_is_counted(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "GUARD_SLOW_S", 0.0)
    before = (await _guard_stats(client))["slow"]
    await client.post("/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": "hi"}]})
    stats = await _guard_stats(client)
    assert stats["slow"] == before + 1 and stats["checks"] >= 1 and stats["p99_ms"] is not None


async def test_private_values_follow_contact_updates(client, signup, hallway):
    """Private values are cached per attendee; a contact change takes effect on the next check."""
    pc, _ = await signup("Asha Rao", contact={"telegram": "@asha_old_tg"})

    async def check(text: str) -> str:
        r = await client.post("/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": text}]})
        return r.json()["outcome"]

    assert await check("ping @asha_old_tg") == "GUARDRAIL_RESULT_OUTCOME_REDACTED"
    matcher = hallway.private_matcher()
    assert hallway.private_matcher() is matcher  # no rebuild without a change
    assert (await pc.put("/api/me", json={"contact": {"telegram": "@asha_new_tg"}})).status_code == 200
    assert await check("ping @asha_new_tg") == "GUARDRAIL_RESULT_OUTCOME_REDACTED"
    assert await check("ping @asha_old_tg") == "GUARDRAIL_RESULT_OUTCOME_OK"
    _, _ = await signup("Bela Shah", contact={"signal": "bela.signal.77"})
    assert await check("try bela.signal.77") == "GUARDRAIL_RESULT_OUTCOME_REDACTED"


def test_private_value_every_occurrence_is_redacted():
    text = "@Asha_TG or @asha_tg"
    o, findings = guard.scan(text, ["@asha_tg"])
    assert o == "redacted" and guard.redact(text, findings) == "[redacted] or [redacted]"
