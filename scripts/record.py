"""Record a phone-size tour of the app as a GIF, signed in as a persona.

    .venv/bin/python scripts/record.py <out.gif> [persona-email] [--dark] [--tour tabs|inbox]

Read-only unless the tour says otherwise. Needs ffmpeg."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("HALLWAY_URL", "http://localhost:8787")


def tour_tabs(page):
    for tab in ("now", "lobby", "people", "rooms", "me"):
        page.click(f"[data-tab='{tab}']")
        page.wait_for_timeout(1600)
        if tab == "people":
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(900)
        if tab == "me":
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(1200)


def tour_inbox(page):
    page.click("[data-tab='now']")
    page.wait_for_timeout(1500)
    button = page.locator("[data-meet][data-v='1'], [data-decide][data-v='1']").first
    if button.count():
        button.click()
        page.wait_for_timeout(2500)
    page.mouse.wheel(0, 400)
    page.wait_for_timeout(1500)


TOURS = {"tabs": tour_tabs, "inbox": tour_inbox}

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("email", nargs="?", default="priya@example.com")
ap.add_argument("--dark", action="store_true")
ap.add_argument("--tour", default="tabs", choices=TOURS)
a = ap.parse_args()

session = json.loads((ROOT / "data" / "personas.state.json").read_text())[a.email]["session"]
size = {"width": 390, "height": 844}
with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(
        viewport=size, record_video_dir=tmp, record_video_size=size, color_scheme="dark" if a.dark else "light"
    )
    ctx.add_cookies([{"name": "hw_session", "value": session, "url": BASE}])
    page = ctx.new_page()
    page.goto(BASE + "/#/now")
    page.wait_for_timeout(1500)
    TOURS[a.tour](page)
    video = page.video.path()
    ctx.close()
    browser.close()
    palette = Path(tmp) / "palette.png"
    vf = "fps=10,scale=360:-1:flags=lanczos"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", video, "-vf", f"{vf},palettegen", palette], check=True)
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            video,
            "-i",
            palette,
            "-lavfi",
            f"{vf}[x];[x][1:v]paletteuse",
            a.out,
        ],
        check=True,
    )
    shutil.copy(video, Path(a.out).with_suffix(".webm"))
print(a.out)
