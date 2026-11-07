"""The real worker (from TREE/daypass), --once, logging every model response:
the max_tokens sent, finish_reason, output and reasoning tokens, tool-call arg sizes.

    run_logged.py TREE"""

import asyncio
import json
import sys

sys.path.insert(0, sys.argv.pop(1) + "/daypass")
import worker
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import infer_model
from pydantic_ai.models.wrapper import WrapperModel

priced = worker.priced_model


class Logged(WrapperModel):
    async def request(self, messages, model_settings, params):
        r = await super().request(messages, model_settings, params)
        calls = [
            f"{p.tool_name}({len(p.args if isinstance(p.args, str) else json.dumps(p.args))} chars)"
            for p in r.parts
            if isinstance(p, ToolCallPart)
        ]
        worker.log.info(
            "response: max_tokens=%s finish_reason=%s (raw %s) in=%s out=%s reasoning=%s calls=%s",
            (model_settings or {}).get("max_tokens"),
            r.finish_reason,
            (r.provider_details or {}).get("finish_reason"),
            r.usage.input_tokens,
            r.usage.output_tokens,
            r.usage.output_reasoning_tokens,
            calls or "-",
        )
        return r


worker.priced_model = lambda model, *a: priced(Logged(infer_model(model)), *a)
sys.argv = ["worker.py", "--once"]
asyncio.run(worker.main())
