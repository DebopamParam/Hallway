"""The profile and the private contact vault have a schema (QA #5, #9, #13, X2)."""

import json
import random
import string
import time

from app import guard

from .conftest import mcp_call, sign_in

G = {"x-flintai-api-key": "test-guard-key"}


async def check(client, text: str) -> dict:
    r = await client.post("/guardrails/check", headers=G, json={"messages": [{"role": "user", "content": text}]})
    return r.json()


# ---------------------------------------------------------------- QA #5
async def test_contact_vault_cannot_be_stuffed(signup):
    pc, _ = await signup("Asha Rao")
    junk = {"".join(random.choices(string.ascii_lowercase, k=12)): "x" * 12 for _ in range(2000)}
    r = await pc.put("/api/me", json={"contact": junk})
    assert r.status_code == 400 and "Unknown contact field" in r.json()["error"]
    r = await pc.put("/api/me", json={"contact": {"email": "a" * 200 + "@x.io"}})
    assert r.status_code == 400 and "at most 200" in r.json()["error"]


async def test_a_row_stuffed_before_the_schema_does_not_slow_the_guard(signup, client, hallway):
    await signup("Asha Rao")
    asha = hallway.resolve("asha")
    junk = {f"k{i}": "".join(random.choices(string.ascii_lowercase, k=12)) for i in range(60_000)}
    hallway.store.update("attendees", asha["id"], contact=junk)
    hallway.store.forget_private(asha["id"])
    hallway.store._private = None  # re-read every row, as after a restart
    assert hallway.store.private_values() == []
    started = time.perf_counter()
    assert (await check(client, "hello " * 700))["outcome"] == "GUARDRAIL_RESULT_OUTCOME_OK"
    assert time.perf_counter() - started < 0.5


def test_the_private_matcher_does_not_slow_down_with_many_values():
    """The old matcher was one alternation scanned value by value: 50k values
    took 0.74 s on a 1,600-character message, past Switch's 2 s on longer ones."""
    rnd = random.Random(5)
    values = ["".join(rnd.choices(string.ascii_lowercase, k=12)) + "@example.com" for _ in range(50_000)]
    m = guard.PrivateMatcher(values)
    text = "Looking for founders working on payments and UPI rails, say hi. " * 60  # ~3,900 characters
    started = time.perf_counter()
    assert guard.scan(text, m) == ("ok", [])
    assert time.perf_counter() - started < 0.3  # about 3 ms; the old matcher took seconds
    assert [f.text for f in guard.scan(f"mail {values[123]} now", m)[1]] == [values[123]]


# ---------------------------------------------------------------- QA #9
async def test_vault_values_must_look_like_contact_details(signup):
    pc, _ = await signup("Asha Rao")
    for contact in ({"linkedin": "payments"}, {"whatever": "Razorpay"}, {"phone": "12"}, {"email": "payments"}):
        r = await pc.put("/api/me", json={"contact": contact})
        assert r.status_code == 400, contact
    ok = {"email": "asha@x.io", "phone": "+91 98111 22233", "linkedin": "linkedin.com/in/asha"}
    assert (await pc.put("/api/me", json={"contact": ok})).status_code == 200
    assert (await pc.get("/api/me")).json()["contact"] == ok


async def test_an_ordinary_word_cannot_be_censored_event_wide(signup, client, hallway):
    await signup("Asha Rao")
    asha = hallway.resolve("asha")
    # A vault written before the schema existed, holding ordinary words.
    hallway.store.update("attendees", asha["id"], contact={"linkedin": "payments", "whatever": "Razorpay"})
    hallway.store._private = None
    hallway.store.private_version += 1
    body = await check(client, "Looking for founders working on payments, ex-Razorpay folks welcome")
    assert body["outcome"] == "GUARDRAIL_RESULT_OUTCOME_OK"


def test_vault_values_match_whole_words_only():
    m = guard.PrivateMatcher(["linkedin.com/in/asha"])
    assert guard.scan("see linkedin.com/in/asha.", m)[0] == "redacted"
    assert guard.scan("see linkedin.com/in/ashanti", m)[0] == "ok"
    digits = guard.PrivateMatcher(["9811122233"])
    assert guard.scan("call +919811122233", digits)[0] == "redacted"


# ------------------------------------------------------------ QA #13, X2
async def test_bad_profile_types_are_refused(signup):
    pc, _ = await signup("Mallory Q")
    for profile in (
        {"interests": [1, 2]},
        {"interests": "rust, go"},
        {"role": {"n": 1}},
        {"talks": "x"},
        {"links": ["x"]},
        {"bio": "x" * 301},
        {"interests": ["x"] * 21},
    ):
        r = await pc.put("/api/me", json={"profile": profile})
        assert r.status_code == 400, profile
    r = await pc.put("/api/me", json={"profile": {"role": "Engineer", "evil": "x"}})
    assert r.status_code == 200
    assert "evil" not in (await pc.get("/api/me")).json()["profile"]


async def test_register_checks_the_profile_too(client):
    await sign_in(client, "qafix.x2@example.com")
    r = await client.post("/api/register", json={"name": "Qafix X2", "profile": {"interests": "rust, go"}})
    assert r.status_code == 400
    r = await client.post("/api/register", json={"name": "Qafix X2", "contact": {"ssn": "123-45-6789"}})
    assert r.status_code == 400


async def test_one_bad_profile_already_stored_does_not_break_people_for_everyone(signup, client, hallway):
    vc, victim = await signup("Vic Mehta")
    await signup("Mallory Q")
    mallory = hallway.resolve("mallory")
    hallway.store.db.execute(
        "UPDATE attendees SET profile = ? WHERE id = ?",
        (json.dumps({"interests": [1, 2], "talks": "x", "role": {"n": 1}, "links": "y"}), mallory["id"]),
    )
    r = await vc.get("/api/people")
    assert r.status_code == 200
    m = next(p for p in r.json() if p["handle"] == mallory["handle"])
    assert "interests" not in m and "role" not in m
    res = await mcp_call(client, victim["connect"]["token"], "search_people")
    assert mallory["handle"] in json.dumps(res) and "strip" not in json.dumps(res)
