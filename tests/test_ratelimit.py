"""Rate limits (#10): the token bucket (with a fake clock), 429 + Retry-After on
the HTTP endpoints, and actionable tool errors on MCP."""
import pytest

from app import ratelimit
from app.ratelimit import Limit, RateLimiter, limiter, parse

from .conftest import ADMIN_KEY, GUARD_KEY, WORKER_KEY, mcp_call, pass_privacy_check, sign_in


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(limiter, "clock", c)
    return c


@pytest.fixture
def limits(monkeypatch, clock):
    """limits(name="count/period", ...): only these limits apply (others off)."""
    def use(**specs):
        monkeypatch.setattr(limiter, "limits", {name: parse(spec) for name, spec in specs.items()})
    return use


def ip(addr: str) -> dict:
    return {"cf-connecting-ip": addr}


# ------------------------------------------------------------ token bucket
@pytest.mark.parametrize("spec, want", [("20/60", Limit(20, 60)), ("6/m", Limit(6, 60)), ("100/h", Limit(100, 3600)),
                                        ("5 / 900s", Limit(5, 900)), ("3/min", Limit(3, 60))])
def test_parse(spec, want):
    assert parse(spec) == want


@pytest.mark.parametrize("spec", ["off", "0", "0/60", "OFF"])
def test_parse_off(spec):
    assert parse(spec) is None


@pytest.mark.parametrize("spec", ["lots", "5/fortnight", "/60", "5/-1"])
def test_parse_rejects_nonsense(spec):
    with pytest.raises(ValueError):
        parse(spec)


def test_env_overrides_and_global_off():
    got = ratelimit.limits_from_env({"RATE_LIMIT_MCP_OUTREACH": "2/m", "RATE_LIMIT_GUARD": "off"})
    assert got["mcp_outreach"] == Limit(2, 60) and got["guard"] is None
    assert got["auth_start_ip"] == parse(ratelimit.DEFAULTS["auth_start_ip"])
    assert all(v is None for v in ratelimit.limits_from_env({"RATE_LIMITS": "off"}).values())


def test_bucket_allows_a_burst_then_refills():
    c = Clock()
    rl = RateLimiter({"x": Limit(3, 60)}, clock=c)  # one token every 20 s
    assert [rl.hit("x", "k") for _ in range(3)] == [0, 0, 0]
    assert rl.hit("x", "k") == 20
    c.advance(10)
    assert rl.hit("x", "k") == 10
    c.advance(10)
    assert rl.hit("x", "k") == 0
    assert rl.hit("x", "other") == 0  # keys are independent
    assert rl.hit("unknown", "k") == 0  # no such limit: allowed


def test_full_buckets_are_pruned():
    c = Clock()
    rl = RateLimiter({"x": Limit(1, 10)}, clock=c)
    rl.PRUNE_AT = 5
    for i in range(5):
        rl.hit("x", str(i))
    c.advance(10)
    rl.hit("x", "new")
    assert ("x", "new") in rl.buckets and len(rl.buckets) == 1


# --------------------------------------------------------- /api/auth/start
async def test_auth_start_per_ip_429_with_retry_after(client, limits, clock):
    limits(auth_start_ip="2/60")
    for n in range(2):
        r = await client.post("/api/auth/start", json={"email": f"a{n}@example.com"}, headers=ip("203.0.113.1"))
        assert r.status_code == 200
    r = await client.post("/api/auth/start", json={"email": "a9@example.com"}, headers=ip("203.0.113.1"))
    assert r.status_code == 429
    assert r.headers["retry-after"] == "30"
    assert r.json() == {"error": "Too many requests. Wait 30 s and try again.", "code": "rate_limited", "retry_after": 30}
    assert r.headers["content-security-policy"]  # still gets the security headers
    # Another visitor (by CF-Connecting-IP) is unaffected; the first one recovers.
    r = await client.post("/api/auth/start", json={"email": "b@example.com"}, headers=ip("203.0.113.2"))
    assert r.status_code == 200
    clock.advance(30)
    r = await client.post("/api/auth/start", json={"email": "a9@example.com"}, headers=ip("203.0.113.1"))
    assert r.status_code == 200


async def test_auth_start_per_email_across_ips(client, limits):
    limits(auth_start_email="2/900")
    for n in range(2):
        r = await client.post("/api/auth/start", json={"email": "Victim@example.com"}, headers=ip(f"198.51.100.{n}"))
        assert r.status_code == 200
    r = await client.post("/api/auth/start", json={"email": " victim@example.com"}, headers=ip("198.51.100.9"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) == 450


async def test_admin_key_is_not_limited(client, limits):
    """scripts/signup.py and personas.py seed with the admin key."""
    limits(auth_start_ip="1/60", auth_start_email="1/60", register_ip="1/60", session_write="1/60")
    client.headers["x-hallway-admin"] = ADMIN_KEY
    for n in range(3):
        r = await client.post("/api/auth/start", json={"email": "p@example.com"})
        assert r.status_code == 200 and r.json()["dev_link"]


# ------------------------------------------------- /auth/magic, /api/register
async def test_magic_link_per_ip(client, limits):
    limits(magic_ip="2/60")
    for _ in range(2):
        assert (await client.get("/auth/magic?t=nope", headers=ip("203.0.113.5"))).status_code == 307
    r = await client.get("/auth/magic?t=nope", headers=ip("203.0.113.5"))
    assert r.status_code == 429 and r.headers["retry-after"] == "30"


async def test_register_per_ip(make_client, limits):
    limits(register_ip="1/60")
    a, b = make_client(), make_client()
    await sign_in(a, "a@example.com")
    await sign_in(b, "b@example.com")
    r = await a.post("/api/register", json={"name": "Asha Rao"}, headers=ip("203.0.113.7"))
    assert r.status_code == 200
    r = await b.post("/api/register", json={"name": "Bela Shah"}, headers=ip("203.0.113.7"))
    assert r.status_code == 429


# ----------------------------------------------------- per-session writes
async def test_cookie_writes_per_session(signup, limits, clock):
    pa, _ = await signup("Asha Rao")
    pb, _ = await signup("Bela Shah")
    limits(session_write="2/60")
    for _ in range(2):
        assert (await pa.put("/api/me", json={"profile": {"role": "y"}})).status_code == 200
    r = await pa.put("/api/me", json={"profile": {"role": "x"}})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert (await pa.get("/api/me")).status_code == 200  # reads aren't limited
    assert (await pb.put("/api/me", json={"profile": {"role": "y"}})).status_code == 200  # other session
    clock.advance(30)
    assert (await pa.put("/api/me", json={"profile": {"role": "x"}})).status_code == 200


# ------------------------------------------------------------------ guard
async def test_guard_has_a_cap(client, limits):
    limits(guard="2/60")
    body = {"messages": [{"role": "user", "content": "hi"}]}
    h = {"x-flintai-api-key": GUARD_KEY}
    assert [(await client.post("/guardrails/check", json=body, headers=h)).status_code for _ in range(3)] == [200, 200, 429]


async def test_daypass_worker_endpoints_are_not_limited(client, limits):
    limits(session_write="1/60", guard="1/60", mcp_call="1/60")
    for _ in range(5):
        assert (await client.get("/internal/daypass/due", headers={"x-hallway-worker": WORKER_KEY})).status_code == 200


# -------------------------------------------------------------------- MCP
async def test_mcp_calls_per_agent(signup, client, limits, clock):
    _, me = await signup("Asha Rao")
    _, other = await signup("Bela Shah")
    limits(mcp_call="2/60")
    token = me["connect"]["token"]
    assert "your_human" in await mcp_call(client, token, "whoami")
    assert "your_human" in await mcp_call(client, token, "whoami")
    r = await mcp_call(client, token, "whoami")
    assert r == {"error": "Rate limited: too many Hallway tool calls. Wait 30 s, then continue.", "retry_after_s": 30}
    assert "your_human" in await mcp_call(client, other["connect"]["token"], "whoami")  # per agent
    clock.advance(30)
    assert "your_human" in await mcp_call(client, token, "whoami")


async def test_new_key_does_not_reset_the_limit(signup, client, limits):
    pc, me = await signup("Asha Rao")
    limits(mcp_call="1/60")
    await mcp_call(client, me["connect"]["token"], "whoami")
    new = (await pc.post("/api/me/agent-key")).json()["connect"]["token"]
    assert "retry_after_s" in await mcp_call(client, new, "whoami")


async def test_posts_have_a_tighter_limit(signup, client, limits, clock):
    _, me = await signup("Asha Rao")
    token = me["connect"]["token"]
    await pass_privacy_check(client, token)
    limits(mcp_call="100/60", mcp_outreach="2/60")
    for n in range(2):
        r = await mcp_call(client, token, "post", {"room": "lobby", "text": f"hello {n}"})
        assert "error" not in r, r
    r = await mcp_call(client, token, "post", {"room": "lobby", "text": "hello again"})
    assert r["retry_after_s"] == 30 and r["error"].startswith("Rate limited: too many posts")
    # Other outreach shares the bucket; reading doesn't.
    r = await mcp_call(client, token, "request_contact", {"with_who": "nobody"})
    assert "retry_after_s" in r
    r = await mcp_call(client, token, "read_room", {"room": "lobby"})
    assert isinstance(r, list) and any("hello 1" in m.get("text", "") for m in r)
    clock.advance(30)
    assert "error" not in await mcp_call(client, token, "post", {"room": "lobby", "text": "back"})


async def test_default_limits_let_a_day_pass_run_through(signup, client):
    """A worker run (whoami, privacy check, an intro, two outreaches, inbox)
    stays well inside the defaults."""
    _, me = await signup("Sana Qureshi", mode="daypass")
    token = me["connect"]["token"]
    await mcp_call(client, token, "whoami")
    await pass_privacy_check(client, token)
    for text in ("Hi, Sana here for payments", "@asha.agent hello", "@bela.agent hello"):
        assert "error" not in await mcp_call(client, token, "post", {"room": "lobby", "text": text})
    assert "error" not in await mcp_call(client, token, "my_inbox")
