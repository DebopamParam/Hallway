import json

import httpx


async def call(http: httpx.AsyncClient, token: str, tool: str, args: dict | None = None) -> object:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}
    r = await http.post(
        "/mcp",
        json=body,
        headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    res = r.json()["result"]
    if res.get("isError"):
        return {"error": res["content"][0]["text"]}
    if "structuredContent" in res:
        sc = res["structuredContent"]
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(res["content"][0]["text"])
