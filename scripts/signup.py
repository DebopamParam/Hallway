"""Sign up an attendee through the public web API, as the organiser.
Prints the session cookie and MCP token as JSON. Usage: signup.py persona.json

Sends the admin key (x-hallway-admin: GUARD_API_KEY, from the environment or
./.env) so seeding works in every SIGNIN_MODE, needs no INVITE_CODE, and the
attendee is marked as a persona: personas don't take one of the INVITE_LIMIT
spots. Without a key it signs up like a visitor (dev mode links only)."""
import json
import os
import sys
from pathlib import Path

import httpx

BASE = "http://localhost:8787"
ROOT = Path(__file__).resolve().parent.parent


def admin_key() -> str:
    key = os.environ.get("HALLWAY_ADMIN_KEY") or os.environ.get("GUARD_API_KEY")
    if key is None and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.strip().startswith("GUARD_API_KEY="):
                key = line.split("=", 1)[1].strip()
    return key or ""


def signup(p: dict, base: str = BASE) -> dict:
    key = admin_key()
    headers = {"x-hallway-admin": key} if key else {}
    c = httpx.Client(base_url=base, follow_redirects=False, timeout=60, headers=headers)
    r = c.post("/api/auth/start", json={"email": p["email"]})
    r.raise_for_status()
    link = r.json().get("dev_link")
    if not link:
        sys.exit(f"no sign-in link for {p['email']}: {r.json().get('message') or 'check GUARD_API_KEY'}")
    c.get(link.replace(base, ""))
    me = c.get("/api/me").json()
    if me.get("onboarding"):
        r = c.post("/api/register", json={k: p[k] for k in ("name", "profile", "contact", "autonomy", "mode", "live_agent_name") if k in p})
        r.raise_for_status()
        me = r.json()
    return {"handle": me["handle"], "session": c.cookies.get("hw_session"), "token": me["connect"]["token"], "agent": me["agent"]}


if __name__ == "__main__":
    print(json.dumps(signup(json.load(open(sys.argv[1])))))
