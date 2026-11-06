"""Phone-size checks for UI bugs QA found, in headless Chromium.

    .venv/bin/python scripts/ui_regressions.py <screenshot-dir> [base-url] [--dark]

Signs up two attendees through the API (as the organiser, with HALLWAY_ADMIN_KEY
from the environment or ./.env, like signup.py), then at 390x844 and 320x568:

- QA #15: a room with a long unbroken title must not widen the page; the tab
  bar must stay on screen; the title wraps in the Now feed's notice.
- QA #16: in a room with a long conversation, scrolled to the top, the composer
  must still be pinned to the bottom of the viewport.
- QA #17: a signed-in attendee opening the site root goes into the app, not to
  the sign-up landing.
- QA #33: pressing Enter three times on a slow network sends the message once.
- QA #36: a network drop while navigating doesn't sign the attendee out, and
  the app carries on by itself once the connection is back.
- QA #37: a short room title and the Lobby header are readable at 390 and 320.
- QA #45 (with --invite CODE, against a server with that INVITE_CODE): a wrong
  invite code says so at the field; the right one goes on to the sign-in link.
- QA #47: reloading mid-onboarding keeps the step and what was typed, and
  browser Back goes to the previous step.

Attendees' first names start with "Qafix", so a run can never take over a real
attendee's agent. Exits 1 and lists what failed."""

import argparse
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright
from signup import admin_key, signup

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("base", nargs="?", default="http://localhost:8787")
ap.add_argument("--dark", action="store_true")
ap.add_argument("--invite", default="", help="the server's INVITE_CODE: also run the invite gate checks")
opts = ap.parse_args()
out = Path(opts.out)
out.mkdir(parents=True, exist_ok=True)
base = opts.base.rstrip("/")
theme = "dark" if opts.dark else "light"
stamp = int(time.time()) % 100000
problems: list[str] = []

a = signup({"name": f"Qafixa{stamp} Rao", "email": f"qafix.a{stamp}@example.com"}, base)
b = signup({"name": f"Qafixb{stamp} Shah", "email": f"qafix.b{stamp}@example.com"}, base)
key = admin_key()


def as_(who: dict) -> httpx.Client:
    headers = {"origin": base, "cookie": f"hw_session={who['session']}", **({"x-hallway-admin": key} if key else {})}
    return httpx.Client(base_url=base, headers=headers, timeout=60)


ca = as_(a)
long_title = "Qafix" + "x" * 75  # 80 characters, no spaces: the longest title allowed
r = ca.post("/api/rooms", json={"title": long_title, "invite": [b["handle"]]})
r.raise_for_status()
room = r.json()["name"]
for i in range(30):
    ca.post(f"/api/rooms/{room}/messages", json={"text": f"Message {i + 1} of a long conversation about agent UX."})
r = ca.post("/api/rooms", json={"title": "Qafix chat room", "invite": [b["handle"]]})
r.raise_for_status()
short_room = r.json()["name"]


def copies(text: str) -> int:
    msgs = ca.get(f"/api/rooms/{short_room}/messages").json()["messages"]
    return sum(m["text"] == text for m in msgs)


def open_room(page, path: str, title: str) -> None:
    """A hash change keeps the old view until the new one is drawn: wait for its title."""
    page.goto(base + path)
    page.wait_for_function("(t) => document.querySelector('.topbar-title .t')?.textContent === t", arg=title)
    page.wait_for_selector("#say")
    page.wait_for_timeout(800)


def fits(page, sel: str) -> tuple[bool, int]:
    """(whole text shown, width) of the first element matching sel."""
    return page.evaluate(
        "(s) => { const e = document.querySelector(s); return e ? [e.scrollWidth <= e.clientWidth + 1, e.clientWidth] : [false, 0]; }",
        sel,
    )


with sync_playwright() as p:
    browser = p.chromium.launch()
    for w, h in ((390, 844), (320, 568)):
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, color_scheme=theme)
        host = httpx.URL(base).host
        ctx.add_cookies([{"name": "hw_session", "value": b["session"], "domain": host, "path": "/"}])
        page = ctx.new_page()
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        tag = f"{w}-{theme}"

        # QA #17: the site root, signed in.
        page.goto(base + "/")
        page.wait_for_timeout(1200)
        if not page.url.endswith("#/now") or page.locator("#email").count():
            problems.append(f"#17 {tag}: a signed-in attendee at / got {page.url}, not the app")
        # The "started a room with you" notice holds the long title: it wraps.
        clipped = page.evaluate(
            """[...document.querySelectorAll('.feed-text')].some((e) => e.scrollWidth > e.clientWidth + 1)"""
        )
        if clipped:
            problems.append(f"#15 {tag}: a notice with a long unbroken title runs past its box on Now")
        page.screenshot(path=str(out / f"17-root-signed-in-{tag}.png"))

        # QA #15: Rooms with a long title.
        page.goto(base + "/#/rooms")
        page.wait_for_selector(".room-row")
        page.wait_for_timeout(700)
        sw, iw = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
        if sw > iw:
            problems.append(f"#15 {tag}: the Rooms page is {sw}px wide in a {iw}px viewport")
        dock = page.locator(".dock").bounding_box()
        if not dock or dock["y"] + dock["height"] > h + 1 or dock["x"] + dock["width"] > w + 1:
            problems.append(f"#15 {tag}: the tab bar is off screen: {dock}")
        page.screenshot(path=str(out / f"15-rooms-long-title-{tag}.png"))

        # QA #16: the composer, scrolled to the top of a long conversation.
        page.goto(base + f"/#/room/{room}")
        page.wait_for_selector(".composer")
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
        box = page.locator(".composer").bounding_box()
        if not box or box["y"] < 0 or box["y"] + box["height"] > h + 1:
            problems.append(f"#16 {tag}: the composer is not pinned to the viewport: {box}")
        page.screenshot(path=str(out / f"16-room-scrolled-up-{tag}.png"))

        # QA #37: a short room title, and the Lobby, in the header.
        open_room(page, f"/#/room/{short_room}", "Qafix chat room")
        whole, width = fits(page, ".topbar-title .t")
        if not whole:
            problems.append(f"#37 {tag}: the room title 'Qafix chat room' is cut off at {width}px")
        whole, width = fits(page, ".topbar-title .s")
        if not whole:
            problems.append(f"#37 {tag}: the room subtitle is cut off at {width}px")
        page.screenshot(path=str(out / f"37-room-header-{tag}.png"))
        open_room(page, "/#/lobby", "Lobby")
        for sel, what in ((".topbar-title .t", "title"), (".topbar-title .s", "subtitle")):
            whole, width = fits(page, sel)
            if not whole:
                problems.append(f"#37 {tag}: the Lobby {what} is cut off at {width}px")
        page.screenshot(path=str(out / f"37-lobby-header-{tag}.png"))

        # QA #33: Enter three times on a slow network.
        open_room(page, f"/#/room/{short_room}", "Qafix chat room")
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 2500, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        text = f"Qafix slow send {stamp} {w}"
        page.locator("#say").press_sequentially(text)  # real keystrokes, not fill()
        for _ in range(3):
            page.press("#say", "Enter")
            page.wait_for_timeout(150)
        page.wait_for_timeout(9000)
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        n = copies(text)
        if n != 1:
            problems.append(f"#33 {tag}: three Enters on a slow network sent {n} copies")

        # QA #36: offline while navigating, then back online.
        ctx.set_offline(True)
        page.click('.dock [data-tab="now"]')
        page.wait_for_timeout(2000)
        if page.locator("#email").count():
            problems.append(f"#36 {tag}: offline, tapping Now showed the sign-in landing ({page.url})")
        page.screenshot(path=str(out / f"36-offline-{tag}.png"))
        ctx.set_offline(False)
        page.wait_for_timeout(6000)
        if page.locator("#email").count() or not page.url.endswith("#/now") or not page.locator(".dock").count():
            problems.append(f"#36 {tag}: back online, the app is at {page.url}, not Now")
        page.screenshot(path=str(out / f"36-back-online-{tag}.png"))
        ctx.close()

        # QA #47: reload and Back mid-onboarding.
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, color_scheme=theme)
        page = ctx.new_page()
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        r = httpx.post(
            base + "/api/auth/start",
            json={"email": f"qafix.ob{stamp}{w}@example.com"},
            headers={"origin": base, **({"x-hallway-admin": key} if key else {})},
        )
        link = httpx.URL(r.json()["dev_link"])
        page.goto(base + link.raw_path.decode())
        page.wait_for_selector("#name")
        page.fill("#name", f"Qafixo{stamp} Rao")
        page.fill("#role", "Tester")
        page.click("#next")
        page.wait_for_selector("#c_phone")
        page.fill("#c_phone", "+91 98111 00042")
        page.click("#next")
        page.wait_for_selector('input[name="mode"]')
        page.check("input[value='live']")
        page.wait_for_selector("#live_name", timeout=5000)
        page.fill("#live_name", f"qafixlive{stamp}")
        page.reload()
        page.wait_for_selector(".ob-top")
        page.wait_for_timeout(500)
        step = page.locator(".ob-top").inner_text()
        if "Step 3 of 4" not in step:
            problems.append(f"#47 {tag}: after a reload on step 3, onboarding shows '{step.strip()}'")
        elif not page.locator("#live_name").count() or page.locator("#live_name").input_value() != f"qafixlive{stamp}":
            problems.append(f"#47 {tag}: after a reload on step 3, the Live agent choice was lost")
        page.check("input[value='connect']")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / f"47-onboard-reloaded-{tag}.png"))
        page.go_back()
        page.wait_for_timeout(800)
        phone = page.locator("#c_phone").input_value() if page.locator("#c_phone").count() else None
        if phone != "+91 98111 00042":
            problems.append(f"#47 {tag}: browser Back from step 3 went to {page.url}, phone field {phone!r}")
        page.go_back()
        page.wait_for_timeout(800)
        name = page.locator("#name").input_value() if page.locator("#name").count() else None
        if name != f"Qafixo{stamp} Rao" or page.locator("#role").input_value() != "Tester":
            problems.append(f"#47 {tag}: Back to step 1 lost the name: {name!r}")
        page.screenshot(path=str(out / f"47-onboard-back-{tag}.png"))
        ctx.close()

        # QA #45: a wrong invite code, then the right one.
        if opts.invite:
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, color_scheme=theme)
            page = ctx.new_page()
            page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            page.goto(base + "/?invite=wrongcode")
            page.wait_for_selector("#email")
            # Real keystrokes, not fill(): fill() bypassed the blocked-typing bug.
            page.locator("#email").press_sequentially(f"qafix.gate{stamp}{w}@example.com")
            page.click("#send")
            page.wait_for_selector("#invite-code")
            page.wait_for_timeout(300)
            page.screenshot(path=str(out / f"45-gate-first-{tag}.png"))
            page.fill("#invite-code", "alsowrong")
            page.click("#use-code")
            page.wait_for_timeout(1500)
            err = page.locator("#invite-err")
            if not err.count() or not err.is_visible():
                problems.append(f"#45 {tag}: a wrong invite code gave no error at the field")
            elif page.locator("#invite-code").input_value() != "alsowrong":
                problems.append(f"#45 {tag}: the wrong code was cleared from the field")
            page.screenshot(path=str(out / f"45-gate-wrong-code-{tag}.png"))
            if page.locator("#invite-code").count():
                page.fill("#invite-code", opts.invite)
                page.click("#use-code")
                page.wait_for_timeout(1500)
                if not page.get_by_text("Your link is ready").count():
                    problems.append(f"#45 {tag}: the right invite code didn't lead to the sign-in link")
                page.screenshot(path=str(out / f"45-gate-right-code-{tag}.png"))
            ctx.close()
    browser.close()

print("\n".join(problems) or "no problems")
sys.exit(1 if problems else 0)
