"""Security headers, the CSRF Origin check and its exemptions, the client IP
behind Cloudflare, and that the frontend fits the CSP (#9)."""
import dataclasses
import re
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from app import main, security

from .conftest import ADMIN_KEY, GUARD_KEY, WORKER_KEY, mcp_call, register

STATIC = Path(main.__file__).parent / "static"


def use(monkeypatch, **changes):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, **changes))


def bare(origin: str | None = None, **headers) -> httpx.AsyncClient:
    """A client with no default Origin (the conftest clients act like a browser)."""
    if origin:
        headers["origin"] = origin
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver", headers=headers)


# ------------------------------------------------------------------ headers
@pytest.mark.parametrize("path", ["/", "/health", "/api/event", "/static/app.js", "/api/me"])
async def test_security_headers_everywhere(client, path):
    r = await client.get(path)
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers  # public URL is http in tests


async def test_headers_on_mcp_and_csrf_refusals(client, signup):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          headers={"Accept": "application/json, text/event-stream"})
    assert "content-security-policy" in r.headers
    async with bare() as c:
        r = await c.post("/api/auth/start", json={"email": "a@example.com"})
    assert r.status_code == 403 and r.headers["x-content-type-options"] == "nosniff"


async def test_hsts_only_on_https(client, monkeypatch):
    use(monkeypatch, public_url="https://hallway.example.com")
    r = await client.get("/health")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert "upgrade-insecure-requests" in r.headers["content-security-policy"]


async def test_session_cookie_secure_on_https(client, monkeypatch):
    use(monkeypatch, public_url="https://hallway.example.com")
    client.headers["origin"] = "https://hallway.example.com"
    r = await client.post("/api/auth/start", json={"email": "a@example.com"})
    link = r.json()["dev_link"].replace("https://hallway.example.com", "")
    r = await client.get(link)
    cookie = r.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


async def test_session_cookie_not_secure_on_http(client):
    r = await client.post("/api/auth/start", json={"email": "a@example.com"})
    r = await client.get(r.json()["dev_link"].replace("http://testserver", ""))
    assert "secure" not in r.headers["set-cookie"].lower()


# --------------------------------------------------------------------- CSRF
async def test_same_origin_write_is_accepted(signup):
    pc, _ = await signup("Asha Rao")
    assert (await pc.put("/api/me", json={"profile": {"role": "Designer"}})).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://testserver.evil.example", "null",
                                    "https://testserver", "http://testserver:8080"])
async def test_cross_origin_write_is_refused(signup, origin):
    pc, _ = await signup("Asha Rao")
    r = await pc.put("/api/me", json={"profile": {"role": "Pwned"}}, headers={"origin": origin})
    assert r.status_code == 403 and r.json()["code"] == "csrf"
    assert (await pc.get("/api/me")).json()["profile"]["role"] != "Pwned"


async def test_write_without_origin_or_referer_is_refused(signup):
    pc, _ = await signup("Asha Rao")
    del pc.headers["origin"]
    r = await pc.post("/api/me/agent-key")
    assert r.status_code == 403


async def test_referer_is_the_fallback(signup):
    pc, _ = await signup("Asha Rao")
    del pc.headers["origin"]
    assert (await pc.post("/api/me/agent-key", headers={"referer": "http://testserver/#/me"})).status_code == 200
    assert (await pc.post("/api/me/agent-key", headers={"referer": "https://evil.example/x"})).status_code == 403


@pytest.mark.parametrize("path", ["/api/auth/start", "/api/auth/logout", "/api/register", "/api/rooms",
                                  "/api/contacts"])
async def test_every_api_write_is_checked(hallway, path):
    async with bare("https://evil.example") as c:
        assert (await c.post(path, json={})).status_code == 403


async def test_reads_are_not_checked(hallway):
    async with bare("https://evil.example") as c:
        assert (await c.get("/api/event")).status_code == 200


async def test_localhost_and_127_are_the_same_origin_on_a_dev_box(hallway, monkeypatch):
    use(monkeypatch, public_url="http://localhost:8799")
    async with bare("http://127.0.0.1:8799") as c:
        assert (await c.post("/api/auth/start", json={"email": "a@example.com"})).status_code == 200
    async with bare("http://127.0.0.1:8798") as c:
        assert (await c.post("/api/auth/start", json={"email": "a@example.com"})).status_code == 403


# --------------------------------------------------------------- exemptions
async def test_mcp_is_exempt(signup):
    _, me = await signup("Asha Rao")
    async with bare("https://evil.example") as c:
        assert (await mcp_call(c, me["connect"]["token"], "whoami"))["your_human"]["handle"] == "asha"
    async with bare() as c:
        assert (await mcp_call(c, me["connect"]["token"], "whoami"))["your_human"]["handle"] == "asha"


async def test_guard_and_internal_are_exempt(hallway):
    async with bare() as c:
        r = await c.post("/guardrails/check", headers={"x-flintai-api-key": GUARD_KEY},
                         json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        r = await c.post("/internal/daypass/nobody/runs", headers={"x-hallway-worker": WORKER_KEY},
                         json={"engine": "t", "cost_usd": 0})
        assert r.status_code != 403  # past the CSRF check (404/400 for an unknown attendee is fine)


async def test_admin_key_is_exempt(hallway, monkeypatch):
    class Gw:
        async def login(self): pass
        async def put_trust_settings(self, *a): return {"ok": True}
        async def set_agent_greetings(self, on): return 0
        async def close(self): pass
    monkeypatch.setattr(main, "GatewayClient", Gw)
    async with bare() as c:
        assert (await c.post("/api/admin/enable-guard", headers={"x-hallway-admin": ADMIN_KEY})).status_code == 200
        r = await c.post("/api/auth/start", json={"email": "p@example.com"}, headers={"x-hallway-admin": ADMIN_KEY})
        assert r.status_code == 200
        # A wrong key is no exemption: refused by the Origin check first.
        r = await c.post("/api/admin/enable-guard", headers={"x-hallway-admin": "wrong"})
        assert r.status_code == 403 and r.json()["code"] == "csrf"


async def test_signup_script_flow_works_with_origin(make_client):
    """scripts/signup.py: Origin + admin key; register via cookie."""
    c = make_client()
    c.headers.update({"x-hallway-admin": ADMIN_KEY})
    me = await register(c, "Priya Raman")
    assert me["connect"]["token"]


# ---------------------------------------------------------------- client IP
def req(client=("10.0.0.7", 1234), **headers) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "client": client,
                    "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]})


def test_client_ip_prefers_cf_connecting_ip():
    assert main.client_ip(req(cf_connecting_ip="203.0.113.9")) == "203.0.113.9"
    assert main.client_ip(req(cf_connecting_ip="2001:db8::1")) == "2001:db8::1"


@pytest.mark.parametrize("bad", ["", "not-an-ip", "1.2.3.4, 5.6.7.8"])
def test_client_ip_falls_back_to_peer(bad):
    assert main.client_ip(req(cf_connecting_ip=bad)) == "10.0.0.7"


def test_client_ip_ignores_cf_header_when_untrusted(monkeypatch):
    use(monkeypatch, trust_cf_connecting_ip=False)
    assert main.client_ip(req(cf_connecting_ip="203.0.113.9")) == "10.0.0.7"


# --------------------------------------------------------- frontend vs CSP
def test_frontend_has_no_inline_script_style_or_external_origin():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> breaks script-src 'self'"
    assert "<style" not in html and " style=" not in html
    js = (STATIC / "app.js").read_text()
    assert not re.search(r"\son[a-z]+=\"", js), "inline event handler attributes break script-src 'self'"
    assert not re.search(r"\sstyle=\"", js), "style attributes break style-src 'self'"
    for f in ("index.html", "app.js", "app.css", "theme.js"):
        assert not re.search(r"https?://(?!www\.w3\.org/)", (STATIC / f).read_text()), f"external origin in {f}"
