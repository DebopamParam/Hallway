"""Seed 3 agent cards (dark-mode rules / plain dark text / no colours) and shoot each in OS dark+light x app dark+light."""
import json, sys, time, httpx
sys.path.insert(0, sys.argv[3] + "/scripts")
from signup import signup
from playwright.sync_api import sync_playwright
port, tag = sys.argv[1], sys.argv[2]; base = f"http://localhost:{port}"
me = signup({"name": "Jamie Judge", "email": f"jj{int(time.time())}@example.com", "profile": {"role": "Judge"}}, base)
H = {"Authorization": f"Bearer {me['token']}", "Accept": "application/json, text/event-stream"}
def mcp(tool, args):
    return httpx.post(base + "/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}).json()
post = "<script>parent.postMessage({type:'hallway:height',px:document.body.scrollHeight},'*')</script>"
cards = {
 "A-dark-rules": "<style>body{font:14px system-ui;margin:0;padding:12px}@media(prefers-color-scheme:dark){body{color:#eee}}</style><b>14:10</b> Meera, Hall B coffee bar<br><b>16:30</b> Rohan, Expo booth 12" + post,
 "B-plain-dark-text": "<style>body{font:14px system-ui;margin:0;padding:12px;color:#1a1a1a}</style><b>14:10</b> Meera, Hall B coffee bar<br><b>16:30</b> Rohan, Expo booth 12" + post,
 "C-no-colours": "<div style='font:14px system-ui;padding:12px'><b>14:10</b> Meera, Hall B coffee bar<br><b>16:30</b> Rohan, Expo booth 12</div>" + post,
}
for t, h in cards.items(): mcp("show_card", {"title": t, "html": h})
with sync_playwright() as p:
    b = p.chromium.launch()
    for os_s in ["dark", "light"]:
      for app_t in ["dark", "light"]:
        ctx = b.new_context(viewport={"width": 390, "height": 1400}, device_scale_factor=2, color_scheme=os_s)
        ctx.add_cookies([{"name": "hw_session", "value": me["session"], "url": base}])
        ctx.add_init_script(f"try{{localStorage.setItem('hw-theme','{app_t}')}}catch(e){{}}")
        pg = ctx.new_page(); pg.goto(base + "/#/now"); pg.wait_for_selector(".agent-card-box summary")
        for s in pg.locator(".agent-card-box summary").all(): s.click()
        pg.wait_for_timeout(1800)
        out = []
        for fr in [f for f in pg.frames if "/cards/" in f.url]:
            out.append(fr.evaluate("()=>[getComputedStyle(document.body).color, getComputedStyle(document.documentElement).backgroundColor, document.compatMode]"))
        hs = pg.evaluate("[...document.querySelectorAll('iframe.agent-card')].map(i=>Math.round(i.getBoundingClientRect().height))")
        print(tag, f"os={os_s} app={app_t}", "heights", hs, out)
        pg.locator(".agent-card-box").first.locator("xpath=..").screenshot(path=f"shots/{tag}-os{os_s}-app{app_t}.png")
        ctx.close()
    b.close()
