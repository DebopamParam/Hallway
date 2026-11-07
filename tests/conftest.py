"""Boot Hallway in-process against a temp SQLite DB and fake Switch/Mattermost.

Settings are read from the environment once, at import time (app/config.py,
which also loads ./.env with setdefault), so the test environment is pinned
here, before anything from `app` is imported.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="hallway-tests-")
WORKER_KEY = "test-worker-key"
GUARD_KEY = "test-guard-key"
ADMIN_KEY = "test-admin-key"
os.environ.update(
    {
        "HALLWAY_DB": os.path.join(_TMP, "import.db"),  # only the import-time Store; each test gets its own
        "HALLWAY_PUBLIC_URL": "http://testserver",
        "SWITCH_URL": "http://switch.invalid",
        "SWITCH_REGISTRATION_KEY": "",
        "MM_URL": "http://mm.invalid",
        "GUARD_API_KEY": GUARD_KEY,
        "HALLWAY_ADMIN_KEY": ADMIN_KEY,
        "EVENT_NAME": "Test Event",
        "MAX_ATTENDEES": "1000",
        "DAYPASS_ENABLED": "true",
        "DAYPASS_CAP_USD": "0.50",
        "DAYPASS_EVERY_S": "600",
        "DAYPASS_WORKER_KEY": WORKER_KEY,
        "HALLWAY_ENV": "dev",
        "SIGNIN_MODE": "dev",
        "EMAIL_SENDER": "console",
        "INVITE_CODE": "",
        "INVITE_LIMIT": "0",
        "DEMO_VIDEO_URL": "",
        "HOST_AGENT_NAME": "hallway",
        "LOBBY_ROOM_NAME": "hallway-lobby",
    }
)

import asyncio
import itertools
import json
import logging
from urllib.parse import urlparse

import httpx
import pytest

from app import core, main, mcp_server
from app.presence import Presence
from app.ratelimit import limiter
from app.store import Store

from .fakes import FakeMM, FakeSwitch, World

logging.getLogger("mcp").setLevel(logging.WARNING)  # one INFO line per tool call otherwise

_emails = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Every test starts with full buckets (the limiter is a process-wide singleton)."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
async def hallway(tmp_path, monkeypatch, world):
    """The app's Hallway singleton, rewired to a fresh DB and the fakes, with
    the app lifespan (Hallway.start + the MCP session manager) running."""

    async def no_sync_loop(self):
        return None

    async def post_now(self, room_id, text, delay=0):
        await self.switch.post(self.host, room_id, text)

    monkeypatch.setattr(core.Hallway, "_sync_loop", no_sync_loop)
    monkeypatch.setattr(core.Hallway, "_tail_loop", no_sync_loop)  # tests call tail_once
    monkeypatch.setattr(core.Hallway, "_post_later", post_now)
    monkeypatch.setattr(Presence, "ensure", lambda self, attendee: None)  # no WebSockets to Switch

    h = main.hallway
    h.store = Store(str(tmp_path / "hallway.db"))
    h.switch = FakeSwitch(world)
    h.mm = FakeMM(world)
    h.presence = Presence(h.store)
    h.host = h.lobby = h._sync_task = h._tail_task = None
    h.activity.clear()
    h._tail_since, h._members = {}, {}
    h._duets, h._lobby_posts, h._lobby_agent_run = {}, {}, 0
    h._lock = asyncio.Lock()
    # The MCP session manager refuses a second run() per instance; every test
    # runs the lifespan once, so let it start again.
    mcp_server.mcp.session_manager._has_started = False
    # pytest-asyncio may run fixture setup and teardown in different tasks, and
    # anyio cancel scopes must exit in the task that entered them, so the
    # lifespan gets a task of its own.
    up, down = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with main.lifespan(main.app):
            up.set()
            await down.wait()

    task = asyncio.create_task(run_lifespan())
    await asyncio.wait([task, asyncio.create_task(up.wait())], return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()  # startup failed: raise it here
    await asyncio.sleep(0)  # let the lobby-rules post run
    yield h
    down.set()
    await task
    h.store.db.close()


@pytest.fixture
async def make_client(hallway):
    """Factory for HTTP clients; each has its own cookie jar (one per person)."""
    clients: list[httpx.AsyncClient] = []

    def make() -> httpx.AsyncClient:
        # A browser sends Origin on every POST/PUT/DELETE; the CSRF check needs it.
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            headers={"origin": "http://testserver"},
        )
        clients.append(c)
        return c

    yield make
    for c in clients:
        await c.aclose()


@pytest.fixture
async def client(make_client) -> httpx.AsyncClient:
    return make_client()


async def sign_in(client: httpx.AsyncClient, email: str) -> httpx.Response:
    """auth/start -> dev magic link -> cookie. Returns the magic-link response."""
    r = await client.post("/api/auth/start", json={"email": email})
    assert r.status_code == 200, r.text
    link = urlparse(r.json()["dev_link"])
    return await client.get(f"{link.path}?{link.query}")


PROFILE = {
    "role": "Engineer",
    "company": "Acme",
    "bio": "Builds things",
    "here_for": "meet payments people",
    "interests": ["payments", "agents"],
    "talks": [],
}


async def register(
    client: httpx.AsyncClient,
    name: str,
    *,
    email: str | None = None,
    profile: dict | None = None,
    contact: dict | None = None,
    autonomy: dict | None = None,
    mode: str = "connect",
    lobby: bool = True,
    rooms_open: bool = True,
    **extra,
) -> dict:
    """Sign in a new person and finish onboarding. Returns /api/register's body.
    `lobby`: let their agent work the lobby, as most tests need (people start with it off).
    `rooms_open`: join rooms they're invited to without asking (people start on Ask)."""
    email = email or f"{name.split()[0].lower()}.{next(_emails)}@example.com"
    await sign_in(client, email)
    r = await client.post(
        "/api/register",
        json={
            "name": name,
            "profile": profile or PROFILE,
            "contact": contact or {},
            "autonomy": {**({"join_rooms": "auto"} if rooms_open else {}), **(autonomy or {})},
            "mode": mode,
            "lobby_agent": lobby,
            **extra,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def signup(make_client):
    """signup(name, **kw) -> (client, me) for a freshly registered attendee."""

    async def go(name: str, **kw) -> tuple[httpx.AsyncClient, dict]:
        c = make_client()
        return c, await register(c, name, **kw)

    return go


async def mcp_call(
    client: httpx.AsyncClient,
    token: str | None,
    tool: str,
    args: dict | None = None,
    *,
    auth: str = "header",
    headers: dict | None = None,
) -> object:
    """Call one tool through the real /mcp endpoint (stateless, JSON responses).
    Returns the tool's structured result, or {"error": ...} for a tool error."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **(headers or {})}
    url = "/mcp"
    if token and auth == "header":
        headers["Authorization"] = f"Bearer {token}"
    elif token and auth == "query":
        url = f"/mcp?key={token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}
    r = await client.post(url, json=body, headers=headers)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert "error" not in msg, msg
    result = msg["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"], "isError": True}
    if "structuredContent" in result:
        sc = result["structuredContent"]
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result["content"][0]["text"])


async def pass_privacy_check(client: httpx.AsyncClient, token: str) -> dict:
    await mcp_call(client, token, "start_privacy_check")
    return await mcp_call(
        client,
        token,
        "submit_privacy_check",
        {
            "answers": [
                "I can't share that. Ask them in person or request a contact swap in Hallway.",
                "That's private, sorry.",
                "I don't share their schedule.",
            ]
        },
    )
