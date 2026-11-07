"""A judge signs up on a Day pass and asks "Who should I meet here?" before the
worker runs, so one real-model run does the first round and the shortlist. Then
it runs the worker once, waits for the agent's reply, and saves the chat and shots.

    judge_cap.py BASE OUT_DIR ADMIN_KEY TAG [--two-runs] -- WORKER_CMD...

--two-runs runs the worker once first (the judge's first round), like a real judge."""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

base, out, ak, tag = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
worker_cmd = sys.argv[sys.argv.index("--") + 1 :]
out.mkdir(parents=True, exist_ok=True)
stamp = int(time.time() * 1000) % 10_000_000
t0 = time.time()


def mark(s):
    print(f"[{time.time() - t0:6.1f}s] {s}", flush=True)


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
    page.click("#next")
    page.wait_for_url("**#/me")
    mark("signed up on a Day pass")
    if "--two-runs" in sys.argv[: sys.argv.index("--")]:
        rc = subprocess.run(worker_cmd).returncode
        mark(f"first run: worker --once exited {rc}")
        page.wait_for_timeout(2000)
    page.fill("#chat-in", "Who should I meet here?")
    page.click("#chat-send")
    page.wait_for_timeout(1000)
    mark("sent: Who should I meet here?  (worker not started yet)")
    rc = subprocess.run(worker_cmd).returncode
    mark(f"worker --once exited {rc}")
    chat = page.request.get(base + "/api/me/chat").json()
    page.wait_for_timeout(2500)  # let the 1.5 s chat poll paint
    page.locator("#chat-form").scroll_into_view_if_needed()
    page.evaluate("document.getElementById('chat-log').scrollTop = 1e9")
    page.evaluate("window.scrollBy(0, 120)")  # past the floating tab bar
    page.screenshot(path=str(out / f"{tag}-chat.png"))
    (out / f"{tag}-chat.json").write_text(json.dumps(chat, indent=1))
    print("--- chat transcript ---")
    for m in chat["messages"]:
        print(f"{m['role']:>5}: {m['text']}")
    b.close()
