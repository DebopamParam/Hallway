"""Prove the PydanticAI engine's wiring without spending money: a scripted
FunctionModel stands in for Claude and drives the real Hallway MCP endpoint
with a day pass token.

    HALLWAY_TOKEN=hw_... .venv/bin/python test_engine.py"""
import asyncio
import json
import os

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import worker


def scripted(messages, info: AgentInfo) -> ModelResponse:
    names = sorted(t.name for t in info.function_tools)
    returns = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
    if not returns:
        print("tools offered:", ", ".join(names))
        return ModelResponse(parts=[ToolCallPart("whoami", {})])
    if len(returns) == 1:
        return ModelResponse(parts=[ToolCallPart("search_people", {"query": "payments", "limit": 3})])
    people = returns[-1].content
    print("whoami ->", json.dumps(returns[0].content)[:160])
    print("search_people ->", json.dumps(people)[:200])
    return ModelResponse(parts=[TextPart("Checked in and found payments people to meet.")])


async def main() -> None:
    p = {"name": "Sana Qureshi", "handle": "sana", "runs": 1, "here_for": "payments partner",
         "mcp_url": os.environ.get("HALLWAY_URL", "http://localhost:8787") + "/mcp", "token": os.environ["HALLWAY_TOKEN"]}
    run = await worker.run_pydantic_ai(p, 0.05, model=FunctionModel(scripted))
    print(run)
    assert run.ok and run.tool_calls == 2, run


asyncio.run(main())
