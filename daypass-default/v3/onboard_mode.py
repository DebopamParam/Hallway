"""Walk onboarding to step 3, screenshot, finish, report the registered mode. Records video.
usage: python onboard_mode.py <base> <outdir> <tag> [--dark] [--desktop]"""
import sys, time, httpx
from pathlib import Path
from playwright.sync_api import sync_playwright
base, out, tag = sys.argv[1].rstrip("/"), Path(sys.argv[2]), sys.argv[3]
dark = "--dark" in sys.argv; desk = "--desktop" in sys.argv
out.mkdir(parents=True, exist_ok=True)
vp = {"width": 1440, "height": 900} if desk else {"width": 390, "height": 844}
stamp = int(time.time() * 1000) % 10**7
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport=vp, device_scale_factor=2, color_scheme="dark" if dark else "light",
                        record_video_dir=str(out / f"vid-{tag}"), record_video_size=vp)
    if dark: ctx.add_init_script("localStorage.setItem('hw-theme', 'dark')")
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errs.append(m.text))
    pg.goto(base + "/")
    pg.fill("#email", f"uidev.{stamp}@example.com"); pg.click("#send")
    pg.click("text=Open sign-in link"); pg.wait_for_url("**#/onboard**")
    pg.fill("#name", f"Uidev{stamp} Judge"); pg.fill("#role", "Judge"); pg.wait_for_timeout(400)
    pg.click("#next"); pg.wait_for_selector("#c_phone"); pg.wait_for_timeout(400)
    pg.click("#next"); pg.wait_for_selector('input[name="mode"]'); pg.wait_for_timeout(900)
    pg.screenshot(path=str(out / f"{tag}-step3.png"))
    checked = pg.eval_on_selector('input[name="mode"]:checked', "e => e.value") if pg.locator('input[name="mode"]:checked').count() else None
    order = pg.eval_on_selector_all('input[name="mode"]', "es => es.map(e => e.value)")
    pg.click("#next"); pg.wait_for_selector(".presets"); pg.wait_for_timeout(500)
    pg.click("#next"); pg.wait_for_url("**#/me**", timeout=10000); pg.wait_for_timeout(1500)
    pg.screenshot(path=str(out / f"{tag}-after.png"))
    me = pg.evaluate("fetch('/api/me').then(r => r.json())")
    print(tag, "order:", order, "checked:", checked, "registered mode:", me.get("mode"), "errors:", errs)
    ctx.close(); b.close()
