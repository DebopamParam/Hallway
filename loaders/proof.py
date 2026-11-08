"""Loader proof: Switch-bound calls are slowed in the browser (page.route) so the
busy state is visible; nothing else changes. Local fake stack only."""
import asyncio, json, sys, time
from playwright.async_api import async_playwright

base, out, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
STATE = "b => ({text: b.textContent.trim(), busy: b.getAttribute('aria-busy'), disabled: b.disabled, spinner: !!b.querySelector('.spinner'), width: b.offsetWidth})"

async def main():
    res = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                  record_video_dir=out, record_video_size={"width": 390, "height": 844})
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        async def slow(route):
            if route.request.method != "POST":
                return await route.continue_()
            await asyncio.sleep(delay)
            await route.continue_()
        await pg.route("**/api/register", slow)
        await pg.route("**/api/rooms", slow)
        stamp = int(time.time()) % 100000
        await pg.goto(base + "/")
        await pg.fill("#email", f"loader{stamp}@example.com"); await pg.click("#send"); await pg.click("text=Open sign-in link")
        await pg.wait_for_url("**#/onboard")
        await pg.fill("#name", f"Loader{stamp} Rao"); await pg.fill("#role", "Engineer")
        await pg.fill("#here_for", "Meet people working on PQC"); await pg.fill("#interests", "pqc, payments")
        for _ in range(3):
            await pg.click("#next")
        res["onboard_width_before"] = await pg.eval_on_selector("#next", "b => b.offsetWidth")
        await pg.screenshot(path=f"{out}/onboard-step4.png")
        await pg.click("#next", no_wait_after=True)
        await pg.wait_for_timeout(700)
        res["onboard_busy"] = await pg.eval_on_selector("#next", STATE)
        await pg.screenshot(path=f"{out}/onboard-joining.png")
        if delay > 8:
            await pg.wait_for_timeout(7800)
            res["onboard_slow_text"] = await pg.eval_on_selector("#next", "b => b.textContent.trim()")
            await pg.screenshot(path=f"{out}/onboard-still-working.png")
        await pg.wait_for_url("**#/me", timeout=30000)
        await pg.wait_for_timeout(3200)
        await pg.click("[data-tab='people']"); await pg.wait_for_selector("[data-room]")
        await pg.locator("[data-room]").first.click(no_wait_after=True)
        await pg.wait_for_timeout(700)
        res["room_busy"] = await pg.evaluate("() => { const b = document.querySelector('[data-room].is-busy'); return b && {text: b.textContent.trim(), busy: b.getAttribute('aria-busy'), spinner: !!b.querySelector('.spinner')}; }")
        await pg.screenshot(path=f"{out}/people-starting.png")
        await pg.wait_for_url("**#/room/**", timeout=30000)
        await pg.wait_for_timeout(1500)
        # A failed start puts the button back as it was.
        await pg.unroute("**/api/rooms")
        async def fail(route):
            if route.request.method != "POST":
                return await route.continue_()
            await route.fulfill(status=502, content_type="application/json", body=json.dumps({"detail": "Switch is busy, try again"}))
        await pg.route("**/api/rooms", fail)
        await pg.click("[data-tab='people']"); await pg.wait_for_selector("[data-room]")
        btn = pg.locator("[data-room]").first
        before = await btn.inner_html()
        await btn.click(); await pg.wait_for_timeout(900)
        res["room_error_restored"] = (await btn.inner_html()) == before and not await btn.is_disabled() and (await btn.get_attribute("aria-busy")) is None
        res["page_errors"] = errs
        await ctx.close(); await b.close()
    print(json.dumps(res, indent=1))

asyncio.run(main())
