"""Stand-in for daypass/worker.py against the fake stack: same long-poll and chat
calls, no LLM. Logs what each run would have been told (runs, lobby, events)."""
import asyncio, json, os, sys, time
import httpx
URL = sys.argv[1]; KEY = os.environ["DAYPASS_WORKER_KEY"]; LOG = sys.argv[2]
RUN_S = float(os.environ.get("SIM_RUN_S", "6"))
running = {}
def log(d):
    with open(LOG, "a") as f: f.write(json.dumps(d) + "\n")
async def run_one(http, p):
    t0 = time.time()
    first = "FIRST_RUN" if p["runs"] == 0 and p["lobby"] else "PRIVATE_FIRST_RUN" if p["runs"] == 0 else "NEXT/EVENT/CHAT"
    kinds = [e.get("kind") for e in p["events"]]
    log({"t": round(t0, 2), "handle": p["handle"], "runs": p["runs"], "lobby": p["lobby"], "prompt": first,
         "events": kinds, "note": p["note"]})
    h = {"Authorization": f"Bearer {p['token']}"}
    await http.post("/agent/chat", json={"busy": True}, headers=h)
    await asyncio.sleep(RUN_S)
    await http.post("/agent/chat", json={"text": f"(sim) {first} with lobby={p['lobby']}, events={kinds}", "busy": False}, headers=h)
    r = await http.post(f"/internal/daypass/{p['attendee_id']}/runs", json={"engine": "sim", "cost_usd": 0.02, "tool_calls": 3, "summary": "sim", "ok": True})
    r.raise_for_status()
async def main():
    async with httpx.AsyncClient(base_url=URL, headers={"x-hallway-worker": KEY}, timeout=60) as http:
        while True:
            r = await http.get("/internal/daypass/due", params={"wait": 1 if running else 25, "busy": ",".join(running)})
            r.raise_for_status()
            for p in r.json():
                if p["attendee_id"] in running: continue
                async def go(p=p):
                    try: await run_one(http, p)
                    finally: running.pop(p["attendee_id"], None)
                running[p["attendee_id"]] = asyncio.create_task(go())
asyncio.run(main())
