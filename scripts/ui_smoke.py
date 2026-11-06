"""Drive the attendee app in headless Chromium at phone size.

    .venv/bin/python scripts/ui_smoke.py <screenshot-dir> [base-url] [--dark] [--invite CODE]

Signs up a fresh attendee through the UI, walks every tab, saves a screenshot
per screen and fails on any console error or failed API call.

With --invite (the server runs with that INVITE_CODE) the sign-up starts from
/?invite=CODE, then two fresh visitors check the gate: one without the code
must get the invite-only screen, and one with it gets the demo-full screen
when no spots are left (start the server with INVITE_LIMIT=1 to see it).

The attendee's first name starts with "Uidev": agent and Mattermost names come
from it, so a test run can never take over a real attendee's agent."""

import argparse
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("base", nargs="?", default="http://localhost:8787")
ap.add_argument("--dark", action="store_true")
ap.add_argument("--invite")
opts = ap.parse_args()
out = Path(opts.out)
out.mkdir(parents=True, exist_ok=True)
base = opts.base.rstrip("/")
dark = opts.dark
stamp = int(time.time())
email = f"uidev.{stamp}@example.com"
problems: list[str] = []
expect: set[int] = set()  # statuses /api/auth/start may answer right now (the gate checks)


def watch(page) -> None:
    def on_console(m):
        if m.type == "error" and not any(f"status of {code}" in m.text for code in expect):
            problems.append(f"console: {m.text}")

    def on_response(r):
        if "/api/" in r.url and r.status >= 400 and not ("/api/auth/start" in r.url and r.status in expect):
            problems.append(f"{r.status} {r.url}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("response", on_response)


with sync_playwright() as p:
    browser = p.chromium.launch()

    def new_page():
        pg = browser.new_context(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="dark" if dark else "light"
        ).new_page()
        watch(pg)
        return pg

    page = new_page()

    def shot(name: str, pg=None) -> None:
        pg = pg or page
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(out / f"{name}.png"), full_page=False)

    page.goto(base + (f"/?invite={opts.invite}" if opts.invite else "/"))
    if opts.invite and "invite=" in page.url:
        problems.append("the invite code stayed in the address bar")
    page.fill("#email", email)
    shot("01-landing")
    page.click("#send")
    page.click("text=Open sign-in link")
    page.wait_for_url("**#/onboard")
    page.fill("#name", f"Uidev{stamp % 100000} Kapoor")
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
    page.click("[data-preset='balanced']")
    page.click("#next")
    page.wait_for_url("**#/me")
    shot("06-me")
    if "<your-hallway-key>" in page.inner_text("#cc") or "Bearer hw_" not in page.inner_text("#cc"):
        problems.append("the new attendee's MCP key is not shown after sign-up")
    page.screenshot(path=str(out / "06b-me-full.png"), full_page=True)
    for tab in ("now", "lobby", "rooms", "people"):
        page.click(f"[data-tab='{tab}']")
        shot(f"07-{tab}")
    page.click("[data-tab='lobby']")
    page.fill("#say", "Hi all, a UI test from Juspay. Anyone working on agent checkout UX?")
    page.click("#send")
    page.wait_for_timeout(3500)
    shot("08-lobby-after-post")
    page.click("[data-tab='people']")
    page.fill("#q", "PQC")
    page.wait_for_timeout(900)
    shot("09-people-search")
    page.click("[data-tab='rooms']")
    page.click("#new")
    shot("10-new-room-sheet")
    page.keyboard.press("Escape")
    page.click("[data-tab='me']")
    page.click("#edit")
    shot("11-edit-badge")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # Agent key: hidden after a reload (shown once), a new one on request.
    page.reload()
    page.wait_for_selector("#rotate")
    if "Bearer hw_" in page.inner_text("#cc"):
        problems.append("the MCP key is still shown after a reload")
    first_key = page.inner_text("#keyhint")
    page.wait_for_timeout(3000)  # let the sign-up toast go
    page.evaluate("document.getElementById('rotate').scrollIntoView({block: 'center'})")
    shot("15-key-hidden")
    page.click("#rotate")
    shot("15b-new-key-sheet")
    page.click("[data-answer='yes']")
    page.wait_for_timeout(800)
    if "Bearer hw_" not in page.inner_text("#cc") or page.inner_text("#keyhint") == first_key:
        problems.append("New key did not show a new MCP key")
    page.wait_for_timeout(3000)
    shot("16-new-key")
    page.evaluate("document.getElementById('rotate').scrollIntoView({block: 'center'})")
    shot("16b-new-key-hint")

    # Sign out everywhere: a second browser signed in as the same person goes too.
    cookies = page.context.cookies()
    other = httpx.Client(base_url=base, cookies={c["name"]: c["value"] for c in cookies if c["name"] == "hw_session"})
    if other.get("/api/me").status_code != 200:
        problems.append("the copied session did not work before signing out")
    page.evaluate("document.getElementById('logout-all').scrollIntoView({block: 'center'})")
    shot("17-signout-buttons")
    page.click("#logout-all")
    shot("18-signout-everywhere-sheet")
    page.click("[data-answer='yes']")
    page.wait_for_timeout(800)
    if page.locator("#email").count() == 0:
        problems.append("sign out everywhere did not land on the sign-in screen")
    expect.add(401)
    if other.get("/api/me").status_code != 401:
        problems.append("sign out everywhere left another session signed in")

    if opts.invite:
        expect.update({403, 409})
        # No code: the invite-only screen.
        v = new_page()
        v.goto(base + "/")
        v.fill("#email", f"uidev.{stamp}.nocode@example.com")
        v.click("#send")
        v.wait_for_selector(".gate")
        if "invite only" not in v.inner_text(".gate h1"):
            problems.append("no-code visitor did not get the invite-only screen")
        shot("12-invite-only", v)
        # A wrong code: the same screen.
        w = new_page()
        w.goto(base + "/?invite=not-the-code")
        w.fill("#email", f"uidev.{stamp}.wrong@example.com")
        w.click("#send")
        w.wait_for_selector(".gate")
        if "invite only" not in w.inner_text(".gate h1"):
            problems.append("wrong-code visitor did not get the invite-only screen")
        # The right code once the demo is full: the demo-full screen.
        ev = httpx.get(base + "/api/event").json()
        if ev.get("spots_left") == 0:
            f = new_page()
            f.goto(f"{base}/?invite={opts.invite}")
            shot("13-landing-full", f)
            f.fill("#email", f"uidev.{stamp}.late@example.com")
            f.click("#send")
            f.wait_for_selector(".gate")
            if "full" not in f.inner_text(".gate h1"):
                problems.append("invited visitor did not get the demo-full screen when no spots were left")
            shot("14-demo-full", f)
        else:
            print(f"note: {ev.get('spots_left')} spots left, demo-full screen not checked (use INVITE_LIMIT=1)")
    browser.close()

print("\n".join(problems) or "no problems")
sys.exit(1 if problems else 0)
