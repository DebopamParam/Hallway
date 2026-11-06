"""Serve Hallway against the in-memory Switch and Mattermost from tests/fakes.py.

    .venv/bin/python scripts/fake_stack.py [port]      (default 8799)

For UI checks without the Switch stack (CI runs scripts/a11y.py against it):
a fresh temp DB, dev sign-in links, and the same fakes the test suite uses.
Agents never get a live Switch connection, and nothing leaves the process."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
_tmp = tempfile.mkdtemp(prefix="hallway-fake-")
# Pinned before `app` is imported: settings are read once, at import time, and
# these win over ./.env (which is only applied with setdefault).
os.environ.update(
    {
        "HALLWAY_DB": os.path.join(_tmp, "hallway.db"),
        "HALLWAY_PUBLIC_URL": f"http://localhost:{PORT}",
        "SWITCH_URL": "http://switch.invalid",
        "SWITCH_REGISTRATION_KEY": "",
        "MM_URL": "http://mm.invalid",
        "HALLWAY_ENV": "dev",
        "SIGNIN_MODE": "dev",
        "EMAIL_SENDER": "console",
        "INVITE_CODE": "",
        "INVITE_LIMIT": "0",
        "HOST_AGENT_NAME": "hallway-fake",
        "LOBBY_ROOM_NAME": "hallway-lobby",
    }
)
sys.path.insert(0, str(ROOT))

import uvicorn

from app import main
from app.presence import Presence
from tests.fakes import FakeMM, FakeSwitch, World

Presence.ensure = lambda self, attendee: None  # type: ignore[method-assign]  # no WebSockets to Switch
world = World()
main.hallway.switch = FakeSwitch(world)  # type: ignore[assignment]
main.hallway.mm = FakeMM(world)  # type: ignore[assignment]

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")
