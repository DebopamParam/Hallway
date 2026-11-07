"""walk.py <main|after> <base_url> <outdir>: the judge walkthrough, same steps on both builds."""
import json
import sys
import time

from playwright.sync_api import sync_playwright

VARIANT, BASE, OUT = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]
PAUSE = 1200
ASK = "Who should I meet today?"
facts: dict = {"variant": VARIANT, "console_errors": [], "page_errors": [], "http_errors": [], "scroll": []}


def shot(page, name: str) -> None:
    page.screenshot(path=f"{OUT}/{VARIANT}-{name}.png")


def pause(page, ms: int = PAUSE) -> None:
    page.wait_for_timeout(ms)


def bubbles(page) -> list[str]:
    return page.eval_on_selector_all(".msg.in.agent .bubble", "els => els.map(e => e.innerText)")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, color_scheme="light",
        record_video_dir=f"{OUT}/video-{VARIANT}", record_video_size={"width": 390, "height": 844},
    )
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and facts["console_errors"].append(m.text[:300]))
    page.on("pageerror", lambda e: facts["page_errors"].append(str(e)[:300]))
    page.on("response", lambda r: r.status >= 400 and facts["http_errors"].append(f"{r.status} {r.request.method} {r.url.replace(BASE, '')}"))

    page.goto(BASE + "/")
    page.wait_for_selector("#email")
    pause(page)
    shot(page, "01-landing")
    page.locator(".steps-list").scroll_into_view_if_needed()
    pause(page)
    facts["how_it_works_1"] = page.inner_text(".steps-list li:first-child")
    page.evaluate("window.scrollTo(0, 0)")
    page.fill("#email", "asha.verma@example.com")
    pause(page, 600)
    page.click("#send")
    page.get_by_text("Open sign-in link").wait_for()
    pause(page)
    page.get_by_text("Open sign-in link").click()

    page.wait_for_selector("#name")
    pause(page)
    for sel, val in [("#name", "Asha Verma"), ("#role", "Partner"), ("#company", "North Star Ventures"),
                     ("#here_for", "Meet founders building agent infrastructure and find one to back"),
                     ("#interests", "AI agents, devtools, infra")]:
        page.fill(sel, val)
        pause(page, 300)
    pause(page)
    shot(page, "02-step1")
    page.click("#next")
    page.wait_for_selector("#c_email")
    pause(page)
    page.click("#next")

    page.wait_for_selector('input[name="mode"]')
    pause(page)
    facts["step3_order"] = page.eval_on_selector_all(".mode .mode-title", "els => els.map(e => e.innerText.split('\\n')[0])")
    facts["step3_default"] = page.eval_on_selector_all(".mode.on .mode-title", "els => els.map(e => e.innerText.split('\\n')[0])")
    page.locator(".mode.on").first.scroll_into_view_if_needed() if facts["step3_default"] else None
    shot(page, "03-step3-default")
    if VARIANT == "main":
        page.click('label.mode:has(input[value="daypass"])')
        page.wait_for_selector('label.mode.on:has(input[value="daypass"])')
        page.locator(".mode.on").scroll_into_view_if_needed()
        pause(page)
    facts["step3_chosen"] = page.eval_on_selector_all(".mode.on .mode-title", "els => els.map(e => e.innerText.split('\\n')[0])")
    pause(page)
    shot(page, "03-step3")
    page.click("#next")

    page.wait_for_selector("#next")
    pause(page)
    facts["lobby_offered"] = page.locator("#ob_lobby").count() > 0
    if facts["lobby_offered"]:
        page.locator("label.ob-lobby").scroll_into_view_if_needed()
        if not page.is_checked("#ob_lobby"):
            page.click("label.ob-lobby")
        pause(page)
        facts["lobby_checked"] = page.is_checked("#ob_lobby")
    shot(page, "04-step4")
    page.click("#next")
    t_enter = time.time()

    page.wait_for_selector("#chat-in", timeout=20000)
    page.wait_for_timeout(500)
    facts["pill_at_land"] = page.inner_text("#agent-status .pill-label")
    facts["toast"] = page.inner_text("#toast") if page.is_visible("#toast") else ""
    shot(page, "05-agent-landed")
    page.locator("#chat-in").scroll_into_view_if_needed()
    pause(page)
    facts["suggestions"] = page.eval_on_selector_all("#chat-suggest [data-ask]", "els => els.map(e => e.innerText)")
    facts["chat_placeholder"] = page.get_attribute("#chat-in", "placeholder")
    shot(page, "06-agent-chat-empty")
    page.click("#chat-in")
    page.keyboard.type(ASK, delay=40)
    pause(page, 600)
    page.keyboard.press("Enter")
    t_ask = time.time()
    pause(page)
    shot(page, "07-agent-working")

    want = "Here's who I'd meet"
    deadline = time.time() + 150
    while time.time() < deadline and not any(want in b or "couldn't look around" in b for b in bubbles(page)):
        page.wait_for_timeout(500)
    facts["reply_after_s"] = round(time.time() - t_ask, 1)
    facts["agent_bubbles"] = bubbles(page)
    facts["agent_bubbles_html"] = page.eval_on_selector_all(".msg.in.agent .bubble", "els => els.map(e => e.innerHTML.slice(0, 400))")
    facts["steps_in_chat"] = page.eval_on_selector_all(".chat-log .step, .chat-log [class*=step]", "els => els.map(e => e.innerText)")
    pause(page)
    facts["pill_after_reply"] = page.inner_text("#agent-status .pill-label")
    page.locator("#chat-form").scroll_into_view_if_needed()
    pause(page)
    shot(page, "08-agent-reply")

    page.evaluate("document.activeElement.blur()")
    page.locator(".chat-log").hover()
    page.mouse.wheel(0, -140)
    page.wait_for_timeout(400)
    probe = "({log: document.querySelector('.chat-log')?.scrollTop, win: Math.round(scrollY), runs: document.body.innerText.match(/(\\d+)\\s*checks?/)?.[1]})"
    before = page.evaluate(probe)
    shot(page, "09-chat-scrolled")
    samples = []
    for _ in range(16):
        page.wait_for_timeout(500)
        samples.append(page.evaluate(probe))
    facts["scroll"] = {"before": before, "samples": samples}
    shot(page, "10-chat-after-polls")

    page.click('[data-tab="now"]')
    page.wait_for_timeout(2500)
    facts["now_badge"] = page.inner_text('[data-tab="now"] .badge') if page.locator('[data-tab="now"] .badge').count() else ""
    facts["now_first_ticket"] = page.inner_text(".ticket >> nth=0")[:300] if page.locator(".ticket").count() else ""
    facts["now_text"] = page.inner_text("main")[:1500]
    pause(page)
    shot(page, "11-now")
    page.mouse.wheel(0, 500)
    pause(page)
    shot(page, "12-now-scrolled")

    page.click('[data-tab="lobby"]')
    page.wait_for_timeout(2500)
    facts["lobby_has_intro"] = page.get_by_text("I'm Asha's agent").count() > 0
    facts["lobby_text"] = page.inner_text("main")[-1200:]
    pause(page)
    shot(page, "13-lobby")

    page.click('[data-tab="rooms"]')
    page.wait_for_timeout(2000)
    facts["rooms_text"] = page.inner_text("main")[:800]
    pause(page)
    shot(page, "14-rooms")
    page.click('[data-tab="me"]')
    page.wait_for_selector("#chat-in")
    pause(page)
    facts["pill_end"] = page.inner_text("#agent-status .pill-label")
    facts["total_s"] = round(time.time() - t_enter, 1)
    facts["video"] = str(page.video.path())
    ctx.close()
    browser.close()

json.dump(facts, open(f"{OUT}/{VARIANT}-facts.json", "w"), indent=1)
print(json.dumps({k: facts[k] for k in ("console_errors", "page_errors", "http_errors")}))
