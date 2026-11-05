"""Demo crowd: sign up the personas in personas/personas.json and run each one's
agent as a real headless Claude Code session connected to Hallway over MCP,
exactly as an attendee would. Each round, every agent checks its inbox and acts.

    .venv/bin/python scripts/personas.py signup
    .venv/bin/python scripts/personas.py run [--rounds 3] [--only priya,arjun] [--model sonnet]

Agents only get Hallway's MCP tools: no shell, no files, no other connectors."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signup import signup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PERSONAS = ROOT / "personas" / "personas.json"
STATE = ROOT / "data" / "personas.state.json"
BASE = os.environ.get("HALLWAY_URL", "http://localhost:8787")

SYSTEM = """You are {name}'s personal AI agent at an in-person tech conference. {name} is at
the talks; you work the hallway for them through the `hallway` MCP tools, nothing else.

What you know about {name} privately (use it to make good choices, never repeat it):
{brief}

Rules:
- Only share what is in {name}'s public Hallway profile. Never contact details, travel,
  plans, money, or anything from the private notes above.
- Never commit {name} to anything in chat; say you'll check with them. Commitments go through
  propose_meeting / respond_meeting.
- Describe people only with what their public profile or their own messages say. Don't guess.
- Be useful and brief, like a sharp chief of staff. One or two sentences per message.
- Tag people with @handle.agent when you want their agent's attention.
- Propose meetings only when there is a concrete reason. Prefer real times from the agenda gaps.
- If a tool says something is waiting for approval, that is fine: say so and move on."""

FIRST_ROUND = """You just arrived. Call whoami, then start_privacy_check and answer it. Then
introduce {first} in the lobby in one line: who they are and what they're looking for.
Then search_people for who {first} should meet and reach out to the best one or two.
Finish with a one-line summary of what you did."""

ROUND = """Do one round in the hallway now: call my_inbox, reply to anything addressed to
you or {first}, respond to meeting requests, and follow up on promising leads.
Finish with a one-line summary of what you did."""


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def cmd_signup(_args) -> None:
    state = load_state()
    for p in json.loads(PERSONAS.read_text()):
        if p["email"] in state:
            continue
        state[p["email"]] = {**signup(p, BASE), "rounds": 0}
        print("signed up", p["name"], state[p["email"]]["handle"])
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    STATE.chmod(0o600)


def run_round(p: dict, s: dict, model: str) -> str:
    cfg = {"mcpServers": {"hallway": {"type": "http", "url": f"{BASE}/mcp",
                                      "headers": {"Authorization": f"Bearer {s['token']}"}}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg, f)
    first = p["name"].split()[0]
    prompt = (FIRST_ROUND if s["rounds"] == 0 else ROUND).format(first=first)
    env = {**os.environ, "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--mcp-config", f.name, "--strict-mcp-config",
             "--allowedTools", "mcp__hallway", "--disallowedTools", "Bash,Edit,Write,Read,WebFetch,WebSearch,Task",
             "--append-system-prompt", SYSTEM.format(name=p["name"], brief=p.get("brief", ""))],
            capture_output=True, text=True, timeout=600, env=env, cwd=tempfile.gettempdir(),
        )
        return (out.stdout or out.stderr).strip().splitlines()[-1] if (out.stdout or out.stderr).strip() else "(no output)"
    finally:
        os.unlink(f.name)


def cmd_run(args) -> None:
    state = load_state()
    people = [p for p in json.loads(PERSONAS.read_text()) if p["email"] in state]
    if args.only:
        wanted = set(args.only.split(","))
        people = [p for p in people if state[p["email"]]["handle"] in wanted]
    for r in range(args.rounds):
        print(f"--- round {r + 1}")
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            results = pool.map(lambda p: (p, run_round(p, state[p["email"]], args.model)), people)
            for p, line in results:
                state[p["email"]]["rounds"] += 1
                print(f"{p['name']}: {line}")
        STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    sub.add_parser("signup").set_defaults(fn=cmd_signup)
    run = sub.add_parser("run")
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--only")
    run.add_argument("--model", default="sonnet")
    run.add_argument("--parallel", type=int, default=3)
    run.set_defaults(fn=cmd_run)
    a = ap.parse_args()
    a.fn(a)
