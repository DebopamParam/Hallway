"""Sign up an attendee through the public web API, as the organiser.
Prints the session cookie and MCP token as JSON. Usage: signup.py persona.json

    HALLWAY_URL=http://localhost:8799 signup.py persona.json

Sends the admin key (x-hallway-admin: HALLWAY_ADMIN_KEY, from the environment
or ./.env; GUARD_API_KEY still works on localhost but is deprecated) so seeding
works in every SIGNIN_MODE, needs no INVITE_CODE, and the attendee is marked as
a persona: personas don't take one of the INVITE_LIMIT spots. Without a key it
signs up like a visitor (dev mode links only)."""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

BASE = os.environ.get("HALLWAY_URL", "http://localhost:8787").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent


def _env(name: str) -> str:
    """From the environment, else ./.env."""
    if name in os.environ:
        return os.environ[name].strip()
    if (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


def admin_key() -> str:
    key = _env("HALLWAY_ADMIN_KEY") or _env("ADMIN_KEY")
    if not key and _env("GUARD_API_KEY"):
        print("signup.py: HALLWAY_ADMIN_KEY is not set, sending GUARD_API_KEY (deprecated)", file=sys.stderr)
        key = _env("GUARD_API_KEY")
    return "" if key == "change-me" else key


def signup(p: dict, base: str = BASE) -> dict:
    key = admin_key()
    # Origin: Hallway refuses cookie-authenticated writes without a same-origin
    # Origin (CSRF check); the admin key alone would exempt us, a visitor isn't.
    headers = {"origin": base, **({"x-hallway-admin": key} if key else {})}
    c = httpx.Client(base_url=base, follow_redirects=False, timeout=60, headers=headers)

    # On an https HALLWAY_PUBLIC_URL the session cookie is Secure, and httpx
    # won't send a Secure cookie back over plain http (seeding from the Docker
    # network, http://hallway:8787). Send the latest one explicitly instead.
    def keep_session(r: httpx.Response) -> None:
        if token := r.cookies.get("hw_session"):
            c.headers["cookie"] = f"hw_session={token}"

    c.event_hooks["response"] = [keep_session]
    r = c.post("/api/auth/start", json={"email": p["email"]})
    r.raise_for_status()
    link = r.json().get("dev_link")
    if not link:
        sys.exit(f"no sign-in link for {p['email']}: {r.json().get('message') or 'check HALLWAY_ADMIN_KEY'}")
    u = urlsplit(link)  # on HALLWAY_PUBLIC_URL's host, which may not be the one we call
    c.get(f"{u.path}?{u.query}")
    me = c.get("/api/me").json()
    if me.get("onboarding"):
        r = c.post(
            "/api/register",
            json={k: p[k] for k in ("name", "profile", "contact", "autonomy", "mode", "live_agent_name") if k in p},
        )
        r.raise_for_status()
        me = r.json()
    if not me["connect"]["token"]:  # already joined: the key was shown once, so issue a new one
        me = c.post("/api/me/agent-key").json()
    return {
        "handle": me["handle"],
        "session": c.headers.get("cookie", "").partition("=")[2] or None,
        "token": me["connect"]["token"],
        "agent": me["agent"],
    }


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(json.dumps(signup(json.load(f))))
