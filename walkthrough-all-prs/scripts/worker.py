"""Scripted day pass worker: the real worker's long-poll and chat protocol, with a
fixed good-agent script of real MCP calls in place of the LLM.

    DAYPASS_WORKER_KEY=... worker.py <base_url> <log.jsonl>"""
import asyncio
import json
import os
import sys
import time

import httpx

from hwmcp import call

URL, LOG = sys.argv[1], sys.argv[2]
KEY = os.environ["DAYPASS_WORKER_KEY"]
STEP_S = float(os.environ.get("SIM_STEP_S", "0.8"))
PRIVACY_ANSWERS = [
    "I can't share that. Ask them in person or request a contact swap in Hallway.",
    "That's private, sorry.",
    "I don't share their schedule.",
]
running: dict[str, asyncio.Task] = {}


def log(d: dict) -> None:
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": round(time.time(), 2), **d}) + "\n")


def failed(r: object) -> bool:
    return isinstance(r, dict) and "error" in r


def people(r: object, me: str) -> list[dict]:
    rows = r if isinstance(r, list) else (r or {}).get("people", []) if isinstance(r, dict) else []
    return [x for x in rows if isinstance(x, dict) and x.get("handle") and x["handle"] != me]


def why(x: dict) -> str:
    p = x.get("profile") or x
    return (p.get("here_for") or p.get("bio") or "Shares your interests")[:110]


def role(x: dict) -> str:
    p = x.get("profile") or x
    return ", ".join(b for b in (p.get("role"), p.get("company")) if b)


def first_name(x: dict) -> str:
    return (x.get("name") or x["handle"]).split()[0]


async def run_one(http: httpx.AsyncClient, p: dict) -> None:
    tok, first, me = p["token"], p["name"].split()[0], p["handle"]
    asks = [e for e in p["events"] if e.get("kind") == "chat"]
    log({"handle": me, "runs": p["runs"], "lobby": p["lobby"], "events": [e.get("kind") for e in p["events"]]})
    auth = {"Authorization": f"Bearer {tok}"}
    await http.post("/agent/chat", json={"busy": True}, headers=auth)
    calls = 0

    async def tool(name: str, args: dict | None = None) -> object:
        nonlocal calls
        await asyncio.sleep(STEP_S)
        calls += 1
        r = await call(http, tok, name, args)
        log({"handle": me, "tool": name, "error": r["error"][:160] if failed(r) else None})
        return r

    replies = []
    if p["runs"] == 0:
        await tool("whoami")
        await tool("start_privacy_check")
        await tool("submit_privacy_check", {"answers": PRIVACY_ANSWERS})
        await tool("my_inbox")
        if p["lobby"]:
            here = p.get("here_for") or "meeting good people"
            await tool("post", {"room": "hallway-lobby", "text": f"Hi all, I'm {first}'s agent. {first} is here to {here[0].lower() + here[1:]}. Say hi to @{me}.agent."})
            found = people(await tool("search_people", {"query": ""}), me)[:3]
            if found:
                r = await tool("propose_shortlist", {
                    "goal": f"People for {first} to meet today",
                    "candidates": [{"handle": x["handle"], "why": why(x)} for x in found],
                    "pick": 2,
                })
                names = ", ".join(f"**{first_name(x)}**" for x in found)
                lines = "\n".join(f"- **{x.get('name') or x['handle']}** ({role(x)}): {why(x)}" for x in found)
                if failed(r):
                    replies.append(f"I said hi in the lobby and found {names}, but couldn't put up the shortlist.")
                else:
                    replies.append(f"I'm in. I introduced you in the lobby and picked three people worth meeting:\n{lines}\n"
                                   "Tick the ones you like on the **Now** tab and I'll say hi.")
        else:
            replies.append("I'm set up and passed the privacy check. I'm not in the lobby yet, so I'm only working on what you ask.")
    if asks:
        r = await tool("search_people", {"query": ""})
        found = people(r, me)[:3]
        if failed(r) or not found:
            replies.append("I couldn't look around the hallway just now: " + (r["error"][:140] if failed(r) else "nobody's here yet."))
        else:
            lines = "\n".join(f"- **{x.get('name') or x['handle']}** ({role(x)}): {why(x)}" for x in found)
            replies.append(f"Here's who I'd meet today, best first:\n{lines}\nWant me to say hi to any of them?")
    if not replies:
        await tool("my_inbox")
        replies.append("Checked the hallway. Nothing new for you right now.")
    await http.post("/agent/chat", json={"text": "\n\n".join(replies), "busy": False}, headers=auth)
    r = await http.post(f"/internal/daypass/{p['attendee_id']}/runs",
                        json={"engine": "sim", "cost_usd": 0.02, "tool_calls": calls, "summary": "scripted run", "ok": True})
    log({"handle": me, "report": r.status_code})


async def main() -> None:
    async with httpx.AsyncClient(base_url=URL, headers={"x-hallway-worker": KEY}, timeout=60) as http:
        while True:
            r = await http.get("/internal/daypass/due", params={"wait": 1 if running else 25, "busy": ",".join(running)})
            r.raise_for_status()
            for p in r.json():
                if p["attendee_id"] in running:
                    continue

                async def go(p=p):
                    try:
                        await run_one(http, p)
                    except Exception as e:
                        log({"handle": p["handle"], "crash": repr(e)[:300]})
                    finally:
                        running.pop(p["attendee_id"], None)

                running[p["attendee_id"]] = asyncio.create_task(go())


asyncio.run(main())
