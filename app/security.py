"""HTTP hardening for the public demo: security headers on every response, an
Origin check against cross-site requests (CSRF), and the client's real IP.

Hallway runs behind a Cloudflare Tunnel: TLS ends at Cloudflare and every
request reaches uvicorn from cloudflared (see main.client_ip for the visitor's IP).
"""

import json
import secrets
from collections.abc import Callable
from urllib.parse import urlparse

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config

# The attendee app is same-origin only: its script, stylesheet, fonts and images
# all live under /static, and it only talks to its own API. No inline script or
# style anywhere (index.html loads static/theme.js; app.js binds its handlers).
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
)
HSTS = "max-age=31536000"

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_REFUSED = "Cross-site request refused: open Hallway from its own address and try again."


def security_headers(s: "config.Settings") -> list[tuple[bytes, bytes]]:
    headers = {
        "content-security-policy": CSP + ("; upgrade-insecure-requests" if _https(s) else ""),
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "strict-origin-when-cross-origin",
        "permissions-policy": PERMISSIONS_POLICY,
        "cross-origin-opener-policy": "same-origin",
        # Revalidate every time (a 304 when the ETag still matches). Without it
        # Cloudflare caches /static/app.js for hours by default, and a deploy
        # reaches visitors only when that copy expires.
        "cache-control": "no-cache",
    }
    if _https(s):
        headers["strict-transport-security"] = HSTS
    return [(k.encode(), v.encode()) for k, v in headers.items()]


def _https(s: "config.Settings") -> bool:
    return urlparse(s.public_url).scheme == "https"


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) with default ports filled in, or None if unparseable."""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
        return u.scheme, u.hostname.lower(), u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None


def origin_allowed(sent: str, s: "config.Settings") -> bool:
    """Same origin as HALLWAY_PUBLIC_URL. On a localhost dev box, localhost and
    127.0.0.1 count as the same host."""
    got, want = _origin(sent), _origin(s.public_url)
    if not got or not want:
        return False
    if got == want:
        return True
    loopback = set(config.LOCAL_HOSTS)
    return (
        config.is_local_url(s)
        and got[0] == want[0]
        and got[2] == want[2]
        and got[1] in loopback
        and want[1] in loopback
    )


def csrf_exempt(scope: Scope, headers: dict[str, str], s: "config.Settings") -> bool:
    """Only cookie-authenticated, state-changing /api/* requests are checked.
    /mcp (bearer token), /guardrails/check and /internal/* (header keys) aren't
    under /api. A valid admin key in x-hallway-admin exempts a request too: a
    browser can't add that header cross-site without a CORS preflight, which
    Hallway never grants."""
    if scope["method"] not in UNSAFE_METHODS or not scope["path"].startswith("/api/"):
        return True
    key = config.admin_key(s)
    return bool(key) and secrets.compare_digest(headers.get("x-hallway-admin", "").encode(), key.encode())


def csrf_ok(headers: dict[str, str], s: "config.Settings") -> bool:
    """Origin (every modern browser sends it on POST/PUT/DELETE), else Referer.
    Neither: refused, so non-browser tools must send Origin (scripts/signup.py does)."""
    sent = headers.get("origin") or headers.get("referer") or ""
    return bool(sent) and sent != "null" and origin_allowed(sent, s)


class SecurityMiddleware:
    """Pure ASGI (no response buffering, so MCP streaming is untouched)."""

    def __init__(self, app: ASGIApp, get_settings: Callable[[], "config.Settings"]):
        self.app = app
        self.get_settings = get_settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        s = self.get_settings()
        extra = security_headers(s)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [(k, v) for k, v in extra if k not in present]
            await send(message)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not csrf_exempt(scope, headers, s) and not csrf_ok(headers, s):
            body = json.dumps({"error": CSRF_REFUSED, "code": "csrf"}).encode()
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send_with_headers)
