"""Seed one attendee on a fake stack: an agent card (no doctype, dark-mode text like
the tool doc asks for) and an agent chat with markdown + an HTML-injection attempt."""
import json, sys
import httpx
sys.path.insert(0, "/home/agent/workspace/wt-text/scripts")
from signup import signup
port = sys.argv[1]; base = f"http://localhost:{port}"
me = signup({"name": "Jamie Judge", "email": "jamie.judge@example.com",
             "profile": {"role": "Developer advocate", "company": "DevTools Academy", "bio": "Judging hackathons",
                         "here_for": "Meet founders building with AI agents", "interests": ["agents", "MCP"]}}, base)
for n, e in [("Meera Iyer", "meera@example.com"), ("Kabir Shah", "kabir@example.com")]:
    signup({"name": n, "email": e, "profile": {"role": "Founder", "company": "X", "bio": "b", "here_for": "h", "interests": ["agents"]}}, base)
H = {"Authorization": f"Bearer {me['token']}", "Accept": "application/json, text/event-stream"}
def mcp(tool, args):
    r = httpx.post(base + "/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    return r.json()["result"]
card = ("<style>body{font:14px system-ui;margin:0;padding:12px}@media(prefers-color-scheme:dark){body{color:#eee}}</style>"
        "<b>14:10</b> Meera, Hall B coffee bar (speaker slot)<br><b>16:30</b> Rohan, Expo booth 12<br><small>Two invites waiting on you.</small>"
        "<script>parent.postMessage({type:'hallway:height',px:document.body.scrollHeight},'*')</script>")
print(str(mcp("show_card", {"title": "Your afternoon plan", "html": card}))[:300])
c = httpx.Client(base_url=base, headers={"origin": base, "cookie": f"hw_session={me['session']}"})
print(c.post("/api/me/chat", json={"text": "Who should I meet today?\nI care about MCP and agent infra.\n<b>not bold</b>"}).status_code)
reply = ("Three good fits:\n- **Meera Iyer** (DevRel, MCP tooling): looking for meetup speakers\n"
         "- **Kabir Shah** (Lumen AI): hiring for agent infra, ask @kabir about sandboxes\n\n"
         "Want me to say hi to both? Ping @meera.agent if you'd rather go first.\n"
         "Test: <script>alert(1)</script> <img src=x onerror=\"document.title='pwned'\">")
print(httpx.post(base + "/agent/chat", headers=H, json={"text": reply}).status_code)
json.dump(me, open(f"/tmp/claude-1000/qa2/wt-text-proof/me-{port}.json", "w"))
