import json, sys
from playwright.sync_api import sync_playwright
P = "/tmp/claude-1000/qa2/wt-text-proof"; port, tag = sys.argv[1], sys.argv[2]
BASE = f"http://localhost:{port}"; S = json.load(open(f"{P}/me-{port}.json"))["session"]
with sync_playwright() as p:
    b = p.chromium.launch()
    def ctx_for(os_s, app_t):
        ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme=os_s)
        ctx.add_cookies([{"name": "hw_session", "value": S, "url": BASE}])
        ctx.add_init_script(f"try{{localStorage.setItem('hw-theme','{app_t}')}}catch(e){{}}")
        return ctx
    for os_s, app_t in [("dark", "light"), ("dark", "dark"), ("light", "light"), ("light", "dark")]:
        ctx = ctx_for(os_s, app_t); pg = ctx.new_page()
        pg.goto(BASE + "/#/now"); pg.wait_for_selector(".agent-card-box summary")
        box = pg.locator(".agent-card-box").first
        box.locator("summary").click(); pg.wait_for_timeout(1500)
        fr = [f for f in pg.frames if "/cards/" in f.url][0]
        info = fr.evaluate("()=>({quirks:document.compatMode,color:getComputedStyle(document.body).color,bg:getComputedStyle(document.documentElement).backgroundColor})")
        h = pg.evaluate("document.querySelector('iframe.agent-card').getBoundingClientRect().height")
        print(f"card os={os_s} app={app_t} iframe_h={h} {info}")
        box.scroll_into_view_if_needed(); pg.wait_for_timeout(300)
        box.screenshot(path=f"{P}/shots/{tag}-card-os{os_s}-app{app_t}.png"); ctx.close()
    for app_t in ["light", "dark"]:
        ctx = ctx_for(app_t, app_t); pg = ctx.new_page()
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("dialog", lambda d: (errs.append("DIALOG " + d.message), d.dismiss()))
        pg.goto(BASE + "/#/me"); pg.wait_for_selector("#chat-log .msg.in"); pg.wait_for_timeout(1500)
        pg.evaluate("document.getElementById('chat-form').scrollIntoView({block:'end'})"); pg.wait_for_timeout(400)
        res = pg.evaluate("""()=>{const a=[...document.querySelectorAll('#chat-log .msg.in .bubble')].pop(), y=[...document.querySelectorAll('#chat-log .msg.out .bubble')].pop();
          return {title:document.title, agent_script_tags:a.querySelectorAll('script,img').length, agent_strong:a.querySelectorAll('strong').length,
                  agent_br:a.querySelectorAll('br').length, mentions:a.querySelectorAll('[data-profile]').length,
                  agent_text_has_script:a.textContent.includes('<script>alert(1)</script>'), agent_text_has_img:a.textContent.includes('<img src=x onerror'),
                  you_br:y.querySelectorAll('br').length, you_b:y.querySelectorAll('b').length, you_text_has_b:y.textContent.includes('<b>not bold</b>')}}""")
        print(f"chat app={app_t} {res} errors={errs}")
        pg.screenshot(path=f"{P}/shots/{tag}-chat-{app_t}.png"); ctx.close()
    b.close()
