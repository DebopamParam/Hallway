"""A judge signs up on a Day pass (lobby on/off), waits for the real agent's first reply,
asks "Who should I meet here?" and waits for the real reply. Saves the chat JSON and shots.

    judge_live.py BASE OUT_DIR ADMIN_KEY TAG LOBBY(0|1)"""

import json
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

base, out, ak, tag, lobby = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5] == "1"
out.mkdir(parents=True, exist_ok=True)
stamp = int(time.time() * 1000) % 10_000_000
t0 = time.time()


def mark(s):
    print(f"[{time.time() - t0:6.1f}s] {s}", flush=True)


def wait_reply(page, n_agent, limit=240):
    """Until the agent has posted n_agent replies and is no longer busy."""
    end = time.time() + limit
    while time.time() < end:
        r = page.request.get(base + "/api/me/chat").json()
        if sum(m["role"] == "agent" for m in r["messages"]) >= n_agent and not r["busy"]:
            return r
        page.wait_for_timeout(1000)
    raise SystemExit(f"no reply #{n_agent} within {limit}s")


def shoot(page, name):
    page.wait_for_timeout(2500)  # let the 1.5 s chat poll paint
    page.locator("#chat-form").scroll_into_view_if_needed()
    page.evaluate("document.getElementById('chat-log').scrollTop = 1e9")
    page.evaluate("window.scrollBy(0, 120)")  # past the floating tab bar
    page.screenshot(path=str(out / f"{tag}-{name}.png"))
    # The whole log, for reading: lift the 48vh cap on the log (a capture aid, not the real layout).
    page.evaluate("document.getElementById('chat-log').style.maxHeight = 'none'")
    page.locator("#chat-log").screenshot(path=str(out / f"{tag}-{name}-fulllog.png"))
    page.evaluate("document.getElementById('chat-log').style.maxHeight = ''")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="light")
    page = ctx.new_page()
    r = httpx.post(
        base + "/api/auth/start",
        json={"email": f"judge{stamp}@example.com"},
        headers={"origin": base, "x-hallway-admin": ak},
    )
    page.goto(base + httpx.URL(r.json()["dev_link"]).raw_path.decode())
    page.wait_for_selector("#name")
    page.fill("#name", "Riya Kapoor")
    page.fill("#role", "Hackathon judge")
    page.fill("#here_for", "Judging the agents track; meet builders working on payments, LLM costs and dev tooling")
    page.click("#next")
    page.wait_for_selector("#c_phone")
    page.click("#next")
    page.wait_for_selector('input[name="mode"]')
    page.check("input[value='daypass']", force=True)
    page.click("#next")
    page.wait_for_selector("#ob_lobby", state="attached")
    if lobby:
        page.check("#ob_lobby", force=True)
    page.click("#next")
    page.wait_for_url("**#/me")
    mark("signed up (lobby switch %s)" % ("ON" if lobby else "off"))
    first = wait_reply(page, 1)
    mark("first-run reply in")
    shoot(page, "1-first-run")
    page.fill("#chat-in", "Who should I meet here?")
    page.click("#chat-send")
    mark("sent: Who should I meet here?")
    chat = wait_reply(page, 2)
    mark("chat reply in")
    shoot(page, "2-who-should-i-meet")
    me = page.request.get(base + "/api/me").json()
    page.click("[data-tab='now']")
    page.wait_for_timeout(1500)
    page.screenshot(path=str(out / f"{tag}-3-now-tab.png"), full_page=True)
    (out / f"{tag}-chat.json").write_text(json.dumps({"chat": chat, "lobby_agent_after": me.get("lobby_agent")}, indent=1))
    print("--- chat transcript ---")
    for m in chat["messages"]:
        print(f"{m['role']:>5}: {m['text']}")
    print("lobby_agent now:", me.get("lobby_agent"))
    b.close()
