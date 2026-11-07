"""Requests/s visible vs hidden on Agent (Working), Now and the Lobby room, plus
catch-up on return, duplicate and leak checks. Fake stack only. polls.py BASE OUT.json"""
import json
import sys
import time
from collections import Counter

import httpx
from playwright.sync_api import sync_playwright

base = sys.argv[1].rstrip("/")
out = sys.argv[2]
WIN = 15
stamp = int(time.time() * 1000) % 10**8
res = {"base": base, "screens": {}, "checks": {}}

c = httpx.Client(base_url=base, headers={"origin": base}, timeout=20)
r = c.post("/api/auth/start", json={"email": f"polls.{stamp}@example.com"}); r.raise_for_status()
link = httpx.URL(r.json()["dev_link"]); c.get(f"{link.path}?{link.query.decode()}")
r = c.post("/api/register", json={"name": f"Pollcheck {stamp}", "mode": "connect", "contact": {"phone": "+91 98000 11122"},
    "profile": {"role": "Engineer", "company": "Acme", "bio": "Polls", "here_for": "Meet builders", "interests": ["agents"]},
    "autonomy": {"chat": "auto", "rooms": "auto", "propose_meeting": "auto", "accept_meeting": "auto"}})
r.raise_for_status()
token = r.json()["connect"]["token"]
agent = lambda body: httpx.post(base + "/agent/chat", json=body, headers={"Authorization": f"Bearer {token}"}).raise_for_status()
c.post("/api/me/chat", json={"text": "Find me GTM people"}).raise_for_status()
httpx.get(base + "/agent/events", headers={"Authorization": f"Bearer {token}"}).raise_for_status()  # connected
agent({"busy": True})  # the agent is "Working"

HIDE = ("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});"
        "Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'hidden'});"
        "document.dispatchEvent(new Event('visibilitychange'))")
SHOW = "delete document.hidden; delete document.visibilityState; document.dispatchEvent(new Event('visibilitychange'))"

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    ctx.add_cookies([{"name": k, "value": v, "url": base} for k, v in c.cookies.items()])
    page = ctx.new_page()
    log = []
    page.on("request", lambda q: log.append((time.monotonic(), q.method, q.url.replace(base, "").split("?")[0])) if "/api/" in q.url else None)

    def window(secs):
        t0 = time.monotonic(); page.wait_for_timeout(secs * 1000); t1 = time.monotonic()
        hits = Counter(f"{m} {u}" for t, m, u in log if t0 <= t < t1)
        return {"rps": round(sum(hits.values()) / secs, 2), "by_endpoint": dict(hits)}

    def since(t0, t1):
        return dict(Counter(f"{m} {u}" for t, m, u in log if t0 <= t < t1))

    for name, route in (("agent", "#/me"), ("now", "#/now"), ("lobby", "#/lobby")):
        page.goto(f"{base}/{route}"); page.wait_for_timeout(2500)
        if name == "agent": assert page.locator(".chat-typing").count(), "agent not Working"; page.screenshot(path=out.replace(".json", "-agent-working.png"))
        s = {"visible": window(WIN)}
        page.evaluate(HIDE); page.wait_for_timeout(500)
        s["hidden"] = window(WIN)
        t = time.monotonic(); page.evaluate(SHOW); page.wait_for_timeout(1200)
        s["catchup_first_1.2s"] = since(t, time.monotonic())
        res["screens"][name] = s
        print(name, s["visible"]["rps"], s["hidden"]["rps"], s["catchup_first_1.2s"], flush=True)

    # A reply that lands while the tab is hidden shows the moment it's back.
    page.goto(f"{base}/#/me"); page.wait_for_selector(".chat-typing")
    page.evaluate(HIDE); page.wait_for_timeout(500)
    agent({"busy": False, "text": "Found 3 GTM people for you."})
    page.wait_for_timeout(4000)
    shown_while_hidden = page.locator("text=Found 3 GTM people").count() > 0
    t = time.monotonic(); page.evaluate(SHOW)
    page.wait_for_selector("text=Found 3 GTM people", timeout=10000)
    res["checks"]["reply_while_hidden"] = {"painted_while_hidden": shown_while_hidden,
                                           "ms_to_show_after_visible": round((time.monotonic() - t) * 1000)}
    page.screenshot(path=out.replace(".json", "-agent-after-return.png"))

    # Leak: polls from a route we left must not come back on the next visibilitychange.
    page.goto(f"{base}/#/lobby"); page.wait_for_timeout(1500)
    page.evaluate(HIDE); page.wait_for_timeout(300)
    page.evaluate("location.hash = '#/people'"); page.wait_for_timeout(1500)
    t = time.monotonic(); page.evaluate(SHOW); page.wait_for_timeout(6000)
    res["checks"]["leak_after_route_change"] = since(t, time.monotonic())

    # Rapid hide/show x5 within 2s: each return catches up once, intervals don't pile up.
    page.goto(f"{base}/#/me"); page.wait_for_timeout(2000)
    for _ in range(5):
        page.evaluate(HIDE); page.wait_for_timeout(200); page.evaluate(SHOW); page.wait_for_timeout(200)
    page.wait_for_timeout(500)
    res["checks"]["after_flapping_steady_15s"] = window(WIN)

    # Real tab switch: open a second tab in the same context and bring it to front.
    page.goto(f"{base}/#/now"); page.wait_for_timeout(1500)
    p2 = ctx.new_page(); p2.goto("about:blank"); p2.bring_to_front(); page.wait_for_timeout(500)
    res["checks"]["real_tab_switch"] = {"visibilityState": page.evaluate("document.visibilityState"), **window(10)}
    page.bring_to_front(); p2.close()
    b.close()
json.dump(res, open(out, "w"), indent=1)
print(json.dumps(res["checks"], indent=1))
