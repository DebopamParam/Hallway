"""Read the og:/twitter: tags from a local Hallway's / (like an unfurl bot) and
render Slack- and X-style cards from them. unfurl.py BASE OUT.png"""
import html
import re
import sys

import httpx
from playwright.sync_api import sync_playwright

base, out = sys.argv[1].rstrip("/"), sys.argv[2]
page_html = httpx.get(base + "/").text
tags = {m.group(1): html.unescape(m.group(2))
        for m in re.finditer(r'<meta (?:property|name)="((?:og|twitter):[^"]+|description)" content="([^"]*)"', page_html)}
title = tags.get("og:title") or re.search(r"<title>(.*?)</title>", page_html).group(1)
desc = tags.get("og:description") or tags.get("description", "")
img = tags.get("og:image")
large = tags.get("twitter:card") == "summary_large_image"
host = base.split("//")[1]
e = html.escape
slack_img = f'<img class="big" src="{e(img)}">' if img and large else ""
x_card = (f'<div class="xc"><img src="{e(img)}"><div class="xt">{e(host)}</div><div class="xh">{e(title)}</div></div>' if img and large
          else f'<div class="xs"><div class="ph">&#128279;</div><div><div class="xt">{e(host)}</div><div class="xh">{e(title)}</div><div class="xt">{e(desc)}</div></div></div>')
doc = f"""<!doctype html><meta charset=utf-8><style>
body{{margin:0;padding:24px;background:#f4f4f5;font:15px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;width:560px}}
h4{{margin:0 0 8px;color:#666;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.slack{{background:#fff;border-radius:8px;padding:14px 16px;margin-bottom:22px}}
.msg b{{font-weight:800}} .msg a{{color:#1264a3}}
.att{{border-left:4px solid #ddd;padding-left:12px;margin-top:8px}}
.site{{font-weight:700;font-size:14px}} .t{{color:#1264a3;font-weight:700}} .d{{color:#1d1c1d}}
.big{{display:block;width:100%;border-radius:8px;margin-top:8px}}
.x{{background:#fff;border-radius:12px;padding:14px 16px}}
.xc{{border:1px solid #cfd9de;border-radius:16px;overflow:hidden;position:relative;margin-top:8px}} .xc img{{display:block;width:100%}}
.xc .xt{{position:absolute;left:12px;bottom:40px;background:rgba(0,0,0,.77);color:#fff;border-radius:4px;padding:0 4px;font-size:13px}}
.xc .xh{{padding:8px 12px;color:#0f1419}}
.xs{{display:flex;gap:12px;border:1px solid #cfd9de;border-radius:16px;overflow:hidden;margin-top:8px}}
.ph{{width:130px;min-height:130px;background:#f7f9f9;display:grid;place-items:center;font-size:32px;border-right:1px solid #cfd9de}}
.xs .xt{{color:#536471;font-size:14px;padding-top:12px}} .xs .xh{{color:#0f1419}}
</style>
<h4>Slack-style unfurl</h4>
<div class="slack"><div class="msg"><b>Debopam</b> <a>{e(base)}/?invite=JUDGES</a></div>
<div class="att"><div class="site">{e(tags.get("og:site_name", host))}</div><div class="t">{e(title)}</div><div class="d">{e(desc)}</div>{slack_img}</div></div>
<h4>X-style card ({e(tags.get("twitter:card", "none"))})</h4>
<div class="x"><div class="msg"><b>Debopam</b> Try it: <a>{e(host)}/?invite=JUDGES</a></div>{x_card}</div>"""
with sync_playwright() as pw:
    b = pw.chromium.launch()
    p = b.new_page(viewport={"width": 608, "height": 400}, device_scale_factor=2)
    p.set_content(doc); p.wait_for_load_state("networkidle")
    p.screenshot(path=out, full_page=True)
    b.close()
print({k: tags.get(k) for k in ("og:title", "og:image", "twitter:card")})
