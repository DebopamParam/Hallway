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
        ctx.close()
    browser.close()

print("\n".join(problems) or "no problems")
sys.exit(1 if problems else 0)
