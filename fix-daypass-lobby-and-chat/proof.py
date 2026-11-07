"""Proof driver: python proof.py BASE OUTDIR ADMINKEY SCENARIO (f1|f2|f3|f6|f7)."""
import json, sys, time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

base, out, ak, scen = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
out.mkdir(parents=True, exist_ok=True)
stamp = int(time.time() * 1000) % 10_000_000
VH = 844


def signup(page, who, lobby):
    r = httpx.post(base + "/api/auth/start", json={"email": f"{who.lower()}{stamp}@example.com"},
                   headers={"origin": base, "x-hallway-admin": ak})
    page.goto(base + httpx.URL(r.json()["dev_link"]).raw_path.decode())
    page.wait_for_selector("#name"); page.fill("#name", f"{who} Rao")
    page.fill("#here_for", "Find teams building agent tooling")
    page.click("#next"); page.wait_for_selector("#c_phone"); page.click("#next")
    page.wait_for_selector('input[name="mode"]'); page.check("input[value='daypass']", force=True)
    page.click("#next"); page.wait_for_selector("#ob_lobby", state="attached")
    if lobby:
        page.check("#ob_lobby", force=True)
    t = time.time(); page.click("#next"); page.wait_for_url("**#/me")
    return t


def until(page, n, tries=120):
    for _ in range(tries):
        if page.locator("#chat-log").count() and page.locator("#chat-log").inner_text().count("(sim)") >= n:
            return True
        page.wait_for_timeout(250)
    return False


def chat_shot(page, name):
    page.evaluate("document.getElementById('chat-form').scrollIntoView({block: 'end'})")
    page.wait_for_timeout(300)
    page.screenshot(path=str(out / name))


with sync_playwright() as p:
    b = p.chromium.launch()
    kw = {"viewport": {"width": 390, "height": VH}, "device_scale_factor": 2, "color_scheme": "light"}
    if scen.startswith("f3"):
        kw.update(has_touch=True, is_mobile=True, record_video_dir=str(out / ("video-" + scen)), record_video_size={"width": 390, "height": VH})
    ctx = b.new_context(**kw)
    page = ctx.new_page()
    res = {"scenario": scen}
    if scen == "f1":
        res["finish_at"] = round(signup(page, "Fone", lobby=True), 2)
        res["first_reply"] = until(page, 1)
        chat_shot(page, "f1-first-run-lobby-on.png")
    elif scen == "f2":
        signup(page, "Ftwo", lobby=False)
        until(page, 1); page.wait_for_timeout(6000)
        page.click("[data-tab='lobby']"); page.wait_for_selector("#lobby_agent", state="attached")
        res["lobby_on_at"] = round(time.time(), 2)
        page.check("#lobby_agent", force=True); page.wait_for_timeout(1500)
        page.click("[data-tab='me']"); page.wait_for_selector("#chat-log")
        res["second_run_reply"] = until(page, 2, tries=60)  # 15 s
        chat_shot(page, "f2-after-lobby-switch-on.png")
        res["chat_log"] = page.locator("#chat-log").inner_text()
    elif scen in ("f3", "f3b"):
        signup(page, "Fthree" if scen == "f3" else "Fthreeb", lobby=False)
        until(page, 1); page.wait_for_timeout(6000)  # first run reported, page settled
        if scen == "f3":
            page.evaluate("window.scrollTo(0, 420)")  # scrolled down to the chat
        else:  # composer just above the tab bar, as after tapping into it
            page.evaluate("window.scrollTo(0, document.getElementById('chat-form').getBoundingClientRect().bottom + scrollY - 763)")
        page.fill("#chat-in", "introduce me to someone working on evals")
        page.tap("#chat-send"); page.wait_for_timeout(300)
        res["scrollY_after_send"] = page.evaluate("window.scrollY")
        page.screenshot(path=str(out / f"{scen}-1-after-send.png"))
        until(page, 2)
        page.wait_for_timeout(200)
        res["scrollY_when_reply_painted"] = page.evaluate("window.scrollY")
        res["agent_msg_when_painted"] = page.evaluate("""(() => { const r = [...document.querySelectorAll('#chat-log .msg.agent')].pop().getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom)]; })()""")
        page.screenshot(path=str(out / f"{scen}-2-reply-painted.png"))
        page.wait_for_timeout(5500)  # the 5 s /api/me poll sees runs change
        res["scrollY_after_me_poll"] = page.evaluate("window.scrollY")
        tab = page.evaluate("Math.round(document.querySelector('.tabbar, nav.tabs, [data-tab]')?.closest('nav')?.getBoundingClientRect().top ?? -1)")
        res["tab_bar_top"] = tab
        res["last_agent_msg_top_bottom"] = page.evaluate("""(() => { const m = [...document.querySelectorAll('#chat-log .msg.agent')].pop(); const r = m.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom)]; })()""")
        page.screenshot(path=str(out / f"{scen}-3-after-me-poll.png"))
    elif scen == "f6":
        signup(page, "Fsix", lobby=False)
        until(page, 1); page.wait_for_timeout(6000)
        page.click("#pause"); page.wait_for_timeout(1500)
        res["hint"] = page.locator(".agent-chat .tiny").inner_text()
        page.locator(".agent-chat").screenshot(path=str(out / "f6-paused-chat-hint.png"))
    elif scen == "f7":
        signup(page, "Fseven" + str(stamp % 97), lobby=False)
        until(page, 1)
        every = page.evaluate("fetch('/api/me').then(r => r.json()).then(m => m.pass.every_s)")
        page.wait_for_timeout((every + 40) * 1000)  # past due, idle: the round is skipped
        page.reload(); page.wait_for_selector("#chat-log"); page.wait_for_timeout(1500)
        res["every_s"] = every
        res["pass_card"] = page.locator(".page").first.inner_text()[:400]
        page.screenshot(path=str(out / "f7-pass-card-idle.png"))
    ctx.close(); b.close()
    print(json.dumps(res))
