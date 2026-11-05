"""Drive the attendee app in headless Chromium at phone size.

    .venv/bin/python scripts/ui_smoke.py <screenshot-dir> [base-url]

Signs up a fresh attendee through the UI, walks every tab, saves a screenshot
per screen and fails on any console error or failed API call."""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
base = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8787"
email = f"neha.{int(time.time())}@example.com"
problems: list[str] = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and problems.append(f"{r.status} {r.url}"))

    def shot(name: str) -> None:
        page.wait_for_timeout(700)
        page.screenshot(path=str(out / f"{name}.png"), full_page=False)

    page.goto(base + "/")
    page.fill("#email", email)
    shot("01-landing")
    page.click("#send")
    page.click("text=Open sign-in link")
    page.wait_for_url("**#/onboard")
    page.fill("#name", "Neha Kapoor")
    page.fill("#role", "Product designer")
    page.fill("#company", "Juspay")
    page.fill("#bio", "Designing checkout flows for UPI")
    page.fill("#here_for", "Meet people building agent UX; hire a design engineer")
    page.fill("#interests", "design, payments, agents")
    page.check("[data-talk='1']")
    shot("02-onboard-profile")
    page.click("#next")
    page.fill("#c_phone", "+91 98111 22233")
    shot("03-onboard-private")
    page.click("#next")
    shot("04-onboard-agent")
    page.click("#next")
    page.click(".seg[data-key='rooms'] button[data-v='auto']")
    shot("05-onboard-autonomy")
    page.click("#next")
    page.wait_for_url("**#/me")
    shot("06-me")
    page.screenshot(path=str(out / "06b-me-full.png"), full_page=True)
    for tab in ("lobby", "rooms", "inbox", "people"):
        page.click(f"[data-tab='{tab}']")
        shot(f"07-{tab}")
    page.click("[data-tab='lobby']")
    page.fill("#say", "Hi all, Neha from Juspay. Anyone working on agent checkout UX?")
    page.click("#send")
    page.wait_for_timeout(3500)
    shot("08-lobby-after-post")
    page.click("[data-tab='people']")
    page.fill("#q", "PQC")
    page.wait_for_timeout(900)
    shot("09-people-search")
    browser.close()

print("\n".join(problems) or "no problems")
sys.exit(1 if problems else 0)
