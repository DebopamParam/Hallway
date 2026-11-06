"""Screenshot every attendee screen, signed in as an existing persona, without
changing any data. For PR proof: run on two commits and compare.

    .venv/bin/python scripts/screens.py <out-dir> [persona-email] [--dark]"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
email = next((a for a in sys.argv[2:] if "@" in a), "priya@example.com")
dark = "--dark" in sys.argv
session = json.loads((ROOT / "data" / "personas.state.json").read_text())[email]["session"]
base = os.environ.get("HALLWAY_URL", "http://localhost:8787")
problems: list[str] = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                              color_scheme="dark" if dark else "light")
    ctx.add_cookies([{"name": "hw_session", "value": session, "url": base}])
    page = ctx.new_page()
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
    for name in ("now", "lobby", "rooms", "people", "me"):
        page.goto(f"{base}/#/{name}")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / f"{name}.png"))
    meet = page.evaluate("fetch('/api/rooms').then(r => r.json()).then(rs => (rs.find(r => r.kind === 'meet') || {}).name)")
    if meet:
        page.goto(f"{base}/#/room/{meet}")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "meeting-room.png"))
    anon = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                               color_scheme="dark" if dark else "light").new_page()
    anon.goto(base + "/")
    anon.wait_for_timeout(800)
    anon.screenshot(path=str(out / "landing.png"))
    browser.close()

print("\n".join(problems) or "no problems")
