"""Call Hallway MCP tools from the command line: mcp_call.py <token> <tool> [json-args]"""
import asyncio, json, os, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("HALLWAY_MCP", "http://localhost:8787/mcp")

async def main(token, tool=None, args="{}"):
    async with streamablehttp_client(URL, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if not tool:
                for t in (await s.list_tools()).tools: print(t.name, "-", (t.description or "")[:80])
                return
            res = await s.call_tool(tool, json.loads(args))
            for c in res.content: print(getattr(c, "text", c))

asyncio.run(main(*sys.argv[1:]))
