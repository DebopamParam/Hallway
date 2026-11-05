"""Sign up an attendee through the public web API (magic link in dev mode).
Prints the session cookie and MCP token as JSON. Usage: signup.py persona.json"""
import json, sys, httpx

BASE = "http://localhost:8787"

def signup(p: dict, base: str = BASE) -> dict:
    c = httpx.Client(base_url=base, follow_redirects=False, timeout=60)
    link = c.post("/api/auth/start", json={"email": p["email"]}).json()["dev_link"]
    c.get(link.replace(base, ""))
    me = c.get("/api/me").json()
    if me.get("onboarding"):
        me = c.post("/api/register", json={k: p[k] for k in ("name", "profile", "contact", "autonomy", "mode", "live_agent_name") if k in p}).json()
    return {"handle": me["handle"], "session": c.cookies.get("hw_session"), "token": me["connect"]["token"], "agent": me["agent"]}

if __name__ == "__main__":
    print(json.dumps(signup(json.load(open(sys.argv[1])))))
