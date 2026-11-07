"""Accessibility audit of the attendee app: axe-core plus Hallway's own checks,
in headless Chromium at phone size, in light and dark.

    .venv/bin/python scripts/a11y.py <out-dir> [base-url] [--theme light|dark|both] [--report-only]

Signs up a fresh attendee through the UI the way scripts/ui_smoke.py does,
seeds a second attendee whose agent proposes a meeting, opens a room and talks
in the lobby (over the real /mcp endpoint), then walks every screen and sheet.
Works against the dev server or scripts/fake_stack.py (no Switch needed).

axe-core comes from scripts/vendor/axe-core (pinned, MPL-2.0) and is injected
with page.evaluate, which the page's Content-Security-Policy doesn't apply to.

Fails (exit 1) on any serious or critical axe violation, or a failed Hallway
check:
  target-size-44   a control's tap target is under 44x44 CSS px (probed with
                   elementFromPoint, so a hit area grown by a pseudo-element
                   counts; inline links in running text are exempt)
  focus-visible    Tab lands on something with no visible focus indicator
  focus-order      Tab lands on something hidden or off screen
  dialog-*         a sheet doesn't take focus, lets Tab leave it, ignores Esc,
                   or doesn't give focus back to the control that opened it
  live-region      chat, approvals or toasts aren't announced
  reduced-motion   animations still run under prefers-reduced-motion
Moderate and minor axe findings are reported but don't fail the run
(--report-only never fails). Screenshots and a11y-report.json go to <out-dir>."""

import argparse
import json
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
AXE = (ROOT / "scripts" / "vendor" / "axe-core" / "axe.min.js").read_text()
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
FAILING = {"serious", "critical"}
PHONE = {"width": 390, "height": 844}

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("base", nargs="?", default="http://localhost:8787")
ap.add_argument("--theme", choices=["light", "dark", "both"], default="both")
ap.add_argument("--report-only", action="store_true")
opts = ap.parse_args()
out = Path(opts.out)
out.mkdir(parents=True, exist_ok=True)
base = opts.base.rstrip("/")
stamp = int(time.time()) % 100000

findings: list[dict] = []  # every axe violation node and Hallway check failure
problems: list[str] = []  # console errors, failed API calls, script trouble
notes: list[str] = []  # seeding trouble that doesn't stop the audit


def add(theme: str, screen: str, rule: str, impact: str, target: str, detail: str = "") -> None:
    findings.append(
        {"theme": theme, "screen": screen, "rule": rule, "impact": impact, "target": target, "detail": detail}
    )


# ------------------------------------------------------------------ seeding
def client() -> httpx.Client:
    return httpx.Client(base_url=base, headers={"origin": base}, timeout=20)


def sign_in(c: httpx.Client, email: str) -> None:
    r = c.post("/api/auth/start", json={"email": email})
    r.raise_for_status()
    link = urlparse(r.json()["dev_link"])
    c.get(f"{link.path}?{link.query}")


def register(c: httpx.Client, name: str, email: str, mode: str = "connect") -> dict:
    sign_in(c, email)
    r = c.post(
        "/api/register",
        json={
            "name": name,
            "mode": mode,
            "contact": {"phone": "+91 98000 11122"},
            "profile": {
                "role": "Payments engineer",
                "company": "Razorpay",
                "bio": "UPI rails and agent checkout",
                "here_for": "Meet people building agent UX",
                "interests": ["design", "payments", "agents"],
            },
            "autonomy": {"chat": "auto", "rooms": "auto", "propose_meeting": "auto", "accept_meeting": "auto"},
        },
    )
    r.raise_for_status()
    return r.json()


def mcp(c: httpx.Client, token: str, tool: str, args: dict | None = None) -> object:
    r = c.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}},
    )
    r.raise_for_status()
    text = r.text
    if text.startswith("event:") or "\ndata:" in text:  # an SSE answer
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    res = json.loads(text).get("result", {})
    if res.get("isError"):
        problems.append(f"seed: {tool}: {res['content'][0]['text']}")
    return res


# ------------------------------------------------------------------ checks
TARGETS_JS = """(min) => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=radio], [role=tab], [tabindex]:not([tabindex="-1"])';
  const scope = document.querySelector('#sheet-root.open .sheet') || document;
  const bad = [];
  const desc = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).join('.') : '')
    + (el.textContent.trim() ? ' "' + el.textContent.trim().slice(0, 24) + '"' : el.getAttribute('aria-label') ? ' [' + el.getAttribute('aria-label') + ']' : '');
  for (const el of scope.querySelectorAll(sel)) {
    if (el.closest('[hidden]') || el.disabled && el.closest('.seg')?.closest('.locked')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    let r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    // Inline links in a sentence are exempt (WCAG 2.5.8's inline exception).
    if (el.tagName === 'A' && cs.display === 'inline' && !el.className) continue;
    if (el.classList.contains('mention') && cs.display === 'inline') continue;  // an @name in a sentence
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const inside = (x, y) => { const h = document.elementFromPoint(x, y); return h && (h === el || el.contains(h) || (el.labels && [...el.labels].some((l) => l.contains(h)))); };
    if (!inside(cx, cy)) continue;  // covered (by the dock, say): not this check's business
    const reach = (dx, dy) => { let n = 0; while (n < 40 && inside(cx + dx * (n + 1), cy + dy * (n + 1))) n++; return n; };
    const w = reach(-1, 0) + reach(1, 0) + 1, h = reach(0, -1) + reach(0, 1) + 1;
    if (w < min || h < min) bad.push({ target: desc(el), detail: `${w}x${h}` });
  }
  window.scrollTo(0, 0);
  return bad;
}"""

FOCUS_STYLE_JS = r"""() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const nodes = [el, el.parentElement, el.parentElement && el.parentElement.parentElement].filter(Boolean);
  // Compare end states, not a transition's first frame (CSSOM, so the CSP allows it).
  const saved = nodes.map((n) => n.style.getPropertyValue('transition'));
  nodes.forEach((n) => n.style.setProperty('transition', 'none', 'important'));
  const snap = () => nodes.map((n) => { const s = getComputedStyle(n); return [s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow, s.borderColor, s.backgroundColor].join('|'); }).join('/');
  const r = el.getBoundingClientRect();
  const focused = snap();
  const style = getComputedStyle(el);
  const desc = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/)[0] : '')
    + ' "' + (el.getAttribute('aria-label') || el.textContent || el.value || '').trim().slice(0, 24) + '"';
  const hiddenInput = parseFloat(style.opacity) === 0;
  const box = hiddenInput ? (el.closest('label') || el).getBoundingClientRect() : r;
  const visible = box.width > 0 && box.height > 0 && box.bottom > 0 && box.top < innerHeight + 4000;
  // The ring's contrast against what's behind it (WCAG 1.4.11: 3:1).
  const ringed = [el, el.closest('label')].filter(Boolean).find((n) => getComputedStyle(n).outlineStyle !== 'none' && parseFloat(getComputedStyle(n).outlineWidth) > 0);
  let ring = null;
  if (ringed) {
    // Any CSS colour (rgb(), color(srgb ...), color-mix results) to [r, g, b, a] via a canvas.
    const cv = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
    const rgb = (c) => { cv.clearRect(0, 0, 1, 1); cv.fillStyle = c; cv.fillRect(0, 0, 1, 1); const d = cv.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
    const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }).reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
    let bgNode = ringed.parentElement, bg = [255, 255, 255];
    while (bgNode) { const c = rgb(getComputedStyle(bgNode).backgroundColor); if (c[3] > 0.5) { bg = c.slice(0, 3); break; } bgNode = bgNode.parentElement; }
    const a = lum(rgb(getComputedStyle(ringed).outlineColor)), b = lum(bg);
    ring = Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100;
  }
  el.blur();
  const blurred = snap();
  el.focus({ preventScroll: true });
  nodes.forEach((n, i) => (saved[i] ? n.style.setProperty('transition', saved[i]) : n.style.removeProperty('transition')));
  return { desc, changed: focused !== blurred, visible, ring, inSheet: Boolean(el.closest('.sheet')) };
}"""

# Toasts and new approval cards go through one always-present status region;
# chat is a log of its own.
LIVE_JS = """() => {
  const status = document.querySelector('#sr-status[role=status], #sr-status[aria-live]');
  const t = document.getElementById('toast');
  const c = document.getElementById('chat');
  const n = document.getElementById('needs');
  return {
    toast: Boolean(status || (t && (t.getAttribute('role') === 'status' || t.getAttribute('aria-live')))),
    chat: !c || c.getAttribute('role') === 'log',
    needs: !n || Boolean(status || n.getAttribute('aria-live')),
  };
}"""


def axe(page: Page, theme: str, screen: str, within: str | None = None) -> None:
    page.evaluate(AXE)  # idempotent: a second copy replaces the first
    ctx = within or "document"
    res = page.evaluate(f"""async () => {{
        const r = await axe.run({ctx if ctx == "document" else f"document.querySelector({json.dumps(ctx)})"},
          {{ runOnly: {{ type: 'tag', values: {json.dumps(AXE_TAGS)} }}, resultTypes: ['violations'] }});
        return r.violations.map((v) => ({{ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => ({{
          target: n.target.join(' '), detail: (n.any[0] || n.all[0] || n.none[0] || {{}}).message || '' }})) }}));
    }}""")
    for v in res:
        for n in v["nodes"]:
            add(theme, screen, v["id"], v["impact"] or "minor", n["target"], n["detail"])


def targets(page: Page, theme: str, screen: str) -> None:
    for b in page.evaluate(TARGETS_JS, 44):
        add(theme, screen, "target-size-44", "serious", b["target"], b["detail"])


def tab_walk(page: Page, theme: str, screen: str, presses: int = 40) -> None:
    page.evaluate("document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)")
    seen = set()
    for _ in range(presses):
        page.keyboard.press("Tab")
        f = page.evaluate(FOCUS_STYLE_JS)
        if not f or f["desc"] in seen:
            continue
        seen.add(f["desc"])
        if not f["visible"]:
            add(theme, screen, "focus-order", "serious", f["desc"], "focus went to something not on screen")
        elif not f["changed"]:
            add(theme, screen, "focus-visible", "serious", f["desc"], "no visible change when focused")
        elif f["ring"] is not None and f["ring"] < 3:
            add(theme, screen, "focus-contrast", "serious", f["desc"], f"focus ring contrast {f['ring']}:1, needs 3:1")
    page.evaluate("document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)")


def check_screen(page: Page, theme: str, screen: str, *, tab: bool = True) -> None:
    page.wait_for_timeout(500)
    page.screenshot(path=str(out / f"{theme}-{screen}.png"))
    axe(page, theme, screen)
    targets(page, theme, screen)
    if tab:
        tab_walk(page, theme, screen)


def check_sheet(page: Page, theme: str, screen: str, trigger: str, *, close: str = "Escape") -> None:
    """Open a sheet from the keyboard, audit it, and check focus handling."""
    page.focus(trigger)
    page.keyboard.press("Enter")
    page.wait_for_selector("#sheet-root.open .sheet")
    page.wait_for_timeout(450)
    page.screenshot(path=str(out / f"{theme}-{screen}.png"))
    if not page.evaluate("Boolean(document.activeElement && document.activeElement.closest('.sheet'))"):
        add(theme, screen, "dialog-focus", "serious", trigger, "focus did not move into the sheet")
    axe(page, theme, screen)
    targets(page, theme, screen)
    for key in ["Tab"] * 30 + ["Shift+Tab"] * 8:
        page.keyboard.press(key)
        f = page.evaluate(FOCUS_STYLE_JS)
        if not f or not f["inSheet"]:
            add(theme, screen, "dialog-trap", "serious", trigger, f"{key} moved focus out of the sheet")
            break
        if not f["changed"]:
            add(theme, screen, "focus-visible", "serious", f["desc"], "no visible change when focused")
    page.keyboard.press(close)
    page.wait_for_timeout(450)
    if page.locator("#sheet-root .sheet").count():
        add(theme, screen, "dialog-esc", "serious", trigger, "Esc did not close the sheet")
        page.evaluate("document.querySelector('#sheet-root [data-close]')?.click()")
        page.wait_for_timeout(450)
    elif not page.evaluate("(sel) => document.activeElement === document.querySelector(sel)", trigger):
        add(
            theme,
            screen,
            "dialog-return",
            "serious",
            trigger,
            "focus did not go back to the button that opened the sheet",
        )


def check_live(page: Page, theme: str, screen: str) -> None:
    live = page.evaluate(LIVE_JS)
    for k, ok in live.items():
        if not ok:
            add(theme, screen, "live-region", "serious", f"#{k}", "not announced to screen readers")


def watch(page: Page) -> None:
    page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and problems.append(f"{r.status} {r.url}"))


# ------------------------------------------------------------------ the walk
def run(browser, theme: str) -> None:
    n = f"{stamp}{'d' if theme == 'dark' else 'l'}"
    ctx = browser.new_context(viewport=PHONE, device_scale_factor=1, color_scheme=theme)
    page = ctx.new_page()
    watch(page)

    page.goto(base + "/")
    page.wait_for_selector("#email")
    check_screen(page, theme, "01-landing")
    check_live(page, theme, "01-landing")
    page.fill("#email", f"uidev.a11y.{n}@example.com")
    page.click("#send")
    page.wait_for_selector("text=Open sign-in link")
    check_screen(page, theme, "02-landing-link")
    page.click("text=Open sign-in link")
    page.wait_for_url("**#/onboard")
    page.fill("#name", f"Uidevmain{n} Kapoor")
    for sel, v in {
        "#role": "Product designer",
        "#company": "Juspay",
        "#bio": "Checkout flows for UPI",
        "#here_for": "Meet people building agent UX",
        "#interests": "design, payments, agents",
    }.items():
        page.fill(sel, v)
    page.check("[data-talk='1']")
    check_screen(page, theme, "03-onboard-you")
    page.click("#next")
    check_screen(page, theme, "04-onboard-private")
    page.click("#next")
    check_screen(page, theme, "05-onboard-agent")
    page.check("input[value='live']")
    page.wait_for_selector("#live_name")
    check_screen(page, theme, "05b-onboard-agent-live", tab=False)
    if page.locator("input[value='daypass']:not([disabled])").count():
        page.check("input[value='daypass']")
        check_screen(page, theme, "05c-onboard-agent-daypass", tab=False)
    page.check("input[value='connect']")
    page.click("#next")
    page.click("[data-preset='careful']")
    check_screen(page, theme, "06-onboard-rules")
    page.click("#next")
    page.wait_for_url("**#/me")
    check_screen(page, theme, "07-agent")
    check_live(page, theme, "07-agent")
    token = re.search(r"Bearer (hw_[\w-]+)", page.inner_text("#cc"))
    me = httpx.Client(
        base_url=base,
        headers={"origin": base},
        timeout=20,
        cookies={c["name"]: c["value"] for c in ctx.cookies() if c["name"] == "hw_session"},
    )
    my = me.put(
        "/api/me", json={"lobby_agent": True, "autonomy": {"join_rooms": "auto"}}
    ).json()  # its agent asks for approvals below; the peer's room joins directly

    # A second attendee and their agent: a meeting request, a room, lobby chat.
    peer_c = client()
    peer = register(peer_c, f"Uidevpeer{n} Rao", f"uidev.peer.{n}@example.com")
    pt = peer["connect"]["token"]
    peer_c.put("/api/me", json={"lobby_agent": True})  # people start with their agent out of the lobby
    mcp(peer_c, pt, "start_privacy_check")
    mcp(
        peer_c,
        pt,
        "submit_privacy_check",
        {"answers": ["I can't share that.", "That's private, sorry.", "I don't share their schedule."]},
    )
    lobby = peer_c.get("/api/event").json().get("lobby", "lobby")
    mcp(
        peer_c,
        pt,
        "post",
        {"room": lobby, "text": f"Hi all! @{my['agent']} your human works on checkout: worth a chat?"},
    )
    mcp(
        peer_c,
        pt,
        "propose_meeting",
        {"with_who": my["handle"], "time": "15:50", "place": "Hall B exit", "why": "Both on agent checkout"},
    )
    mcp(
        peer_c,
        pt,
        "propose_meeting",
        {"with_who": my["handle"], "time": "16:30", "place": "Coffee bar", "why": "Payments design"},
    )
    mcp(peer_c, pt, "create_room", {"title": "Agent checkout", "invite": [my["handle"]]})
    mine = [m for m in me.get("/api/meetings").json() if m["status"] == "proposed" and not m["you_proposed"]]
    if mine:  # a confirmed meeting and its room; seeding trouble is noted, the audit goes on
        try:
            r = me.post(f"/api/meetings/{mine[-1]['id']}", json={"accept": True})
            if r.status_code >= 400:
                notes.append(f"{theme}: seed: accepting a meeting answered {r.status_code}")
        except httpx.HTTPError as e:
            notes.append(f"{theme}: seed: accepting a meeting failed: {type(e).__name__}: {e}")
    if token:  # our agent asks first ("Careful"): an approval card
        mcp(me, token.group(1), "create_room", {"title": "Design + payments", "invite": [peer["handle"]]})
    else:
        problems.append("no MCP key on the Agent tab after sign-up")

    for tab_ in ("now", "lobby", "rooms", "people"):
        page.click(f"[data-tab='{tab_}']")
        page.wait_for_timeout(900)
        check_screen(page, theme, f"08-{tab_}")
        check_live(page, theme, f"08-{tab_}")
    page.click("[data-tab='lobby']")
    page.fill("#say", "Hi all, a UI test from Juspay.")
    page.press("#say", "Enter")
    page.wait_for_timeout(2500)
    check_screen(page, theme, "09-lobby-after-post", tab=False)
    page.fill("#say", "Call me on +91 98111 22233 after the keynote")
    page.click("#send")
    page.wait_for_selector("#sheet-root.open .sheet")
    page.wait_for_timeout(450)
    axe(page, theme, "09b-share-warning-sheet")
    targets(page, theme, "09b-share-warning-sheet")
    page.screenshot(path=str(out / f"{theme}-09b-share-warning-sheet.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(450)
    page.fill("#say", "")

    page.click("[data-tab='rooms']")
    page.wait_for_selector("#new")
    meet = page.locator("a.room-row.meet")
    if meet.count():
        meet.first.click()
        page.wait_for_selector("#chat")
        page.wait_for_timeout(900)
        check_screen(page, theme, "10-meeting-room")
        page.click("[data-tab='rooms']")
        page.wait_for_selector("#new")
    check_sheet(page, theme, "11-new-room-sheet", "#new")

    page.click("[data-tab='people']")
    page.wait_for_selector("#q")
    page.wait_for_timeout(600)
    if page.locator("[data-contact]").count():
        check_sheet(page, theme, "12-swap-sheet", "[data-contact]")
    page.fill("#q", "zzzz-nobody")
    page.wait_for_timeout(900)
    check_screen(page, theme, "13-people-no-match", tab=False)

    page.click("[data-tab='me']")
    page.wait_for_selector("#edit", state="attached")
    # Settings sit in folded groups.
    page.evaluate("document.querySelectorAll('details.group').forEach((d) => d.open = true)")
    check_sheet(page, theme, "14-edit-badge-sheet", "#edit")
    check_sheet(page, theme, "15-new-key-sheet", "#rotate")
    check_sheet(page, theme, "16-signout-all-sheet", "#logout-all")

    page.click("[data-tab='now']")
    page.wait_for_selector("[data-decide], [data-meet]", timeout=8000)
    page.click("[data-decide][data-v='1']")  # approve: the stamp and a toast
    # Wait for the API to answer (the stamp lands then), or a slow answer races the checks below.
    page.wait_for_selector(".ticket.stamped", timeout=15000)
    page.wait_for_timeout(300)
    if not page.inner_text("#sr-status").strip() if page.locator("#sr-status").count() else True:
        add(theme, "17-now-after-approve", "live-region", "serious", "#sr-status", "the approval was not announced")
    # The stamped card fades out (a moment, not a state), then the list reloads.
    page.wait_for_function("() => !document.querySelector('.ticket.stamped')", timeout=15000)
    page.wait_for_timeout(500)
    check_live(page, theme, "17-now-after-approve")
    axe(page, theme, "17-now-after-approve")
    page.screenshot(path=str(out / f"{theme}-17-now-after-approve.png"))

    # The day pass Agent tab, as a third attendee.
    if page.evaluate("fetch('/api/event').then((r) => r.json()).then((e) => Boolean(e.daypass))"):
        dp = browser.new_context(viewport=PHONE, device_scale_factor=1, color_scheme=theme)
        dpage = dp.new_page()
        watch(dpage)
        dc = client()
        register(dc, f"Uidevpass{n} Iyer", f"uidev.pass.{n}@example.com", mode="daypass")
        dp.add_cookies([{"name": "hw_session", "value": dc.cookies["hw_session"], "url": base}])
        dpage.goto(base + "/#/me")
        dpage.wait_for_selector(".pass")
        check_screen(dpage, theme, "18-agent-daypass")
        dp.close()

    # Reduced motion: nothing keeps animating.
    rm = browser.new_context(viewport=PHONE, color_scheme=theme, reduced_motion="reduce")
    rpage = rm.new_page()
    rm.add_cookies(ctx.cookies())
    for screen in ("", "#/now", "#/me"):
        rpage.goto(base + "/" + screen)
        rpage.wait_for_timeout(900)
        moving = rpage.evaluate("""() => document.getAnimations().filter((a) => a.playState === 'running'
            && (a.effect.getComputedTiming().duration > 1 || a.effect.getComputedTiming().iterations === Infinity))
            .map((a) => (a.animationName || a.transitionProperty || 'animation') + ' on ' + (a.effect.target && a.effect.target.className && a.effect.target.className.baseVal !== undefined ? a.effect.target.className.baseVal : a.effect.target && a.effect.target.className))""")
        for m in moving:
            add(theme, f"reduced-motion {screen or 'landing'}", "reduced-motion", "serious", m, "still animating")
    rm.close()
    ctx.close()


with sync_playwright() as p:
    browser = p.chromium.launch()
    for theme in ["light", "dark"] if opts.theme == "both" else [opts.theme]:
        try:
            run(browser, theme)
        except Exception as e:  # report what was found so far, then fail
            problems.append(f"{theme}: walk stopped: {type(e).__name__}: {e}")
            traceback.print_exc()
    browser.close()

# ------------------------------------------------------------------ report
by_rule: dict[tuple[str, str], Counter] = defaultdict(Counter)
for f in findings:
    by_rule[(f["rule"], f["impact"])][f["theme"]] += 1
order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
print(f"{'rule':34} {'impact':9} {'light':>6} {'dark':>6}")
for (rule, impact), c in sorted(by_rule.items(), key=lambda kv: (order.get(kv[0][1], 9), kv[0][0])):
    print(f"{rule:34} {impact:9} {c['light']:>6} {c['dark']:>6}")
failing = [f for f in findings if f["impact"] in FAILING]
print(f"\n{len(findings)} findings, {len(failing)} serious or critical")
for f in failing[:80]:
    print(f"  [{f['theme']}] {f['screen']}: {f['rule']}: {f['target']} {f['detail'][:110]}")
if len(failing) > 80:
    print(f"  ... {len(failing) - 80} more in a11y-report.json")
(out / "a11y-report.json").write_text(json.dumps({"findings": findings, "problems": problems}, indent=1))
print("\n".join(f"note: {n}" for n in notes) or "", end="\n" if notes else "")
print("\n".join(problems) or "no script problems")
sys.exit(0 if opts.report_only else 1 if failing or problems else 0)
