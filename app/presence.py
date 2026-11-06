"""Keeps every Connect-mode stand-in present in Switch.

One WebSocket per stand-in (scope=all, filter=addressed), answering pings. Being
present is what stops Switch auto-replying on the agent's behalf. Addressed
messages that arrive are turned into a person-facing notice ("someone asked your
agent something"), so the human sees activity even before their agent checks in.
"""
import asyncio
import json
import logging
import uuid

import websockets

from .config import settings
from .store import Store

log = logging.getLogger("hallway.presence")


class Presence:
    def __init__(self, store: Store):
        self.store = store
        self.tasks: dict[str, asyncio.Task] = {}
        self._ws_base = settings.switch_url.replace("http://", "ws://").replace("https://", "wss://")

    def ensure(self, attendee: dict) -> None:
        if attendee["mode"] != "connect" or not attendee.get("switch_agent_id"):
            return
        t = self.tasks.get(attendee["id"])
        if t is None or t.done():
            self.tasks[attendee["id"]] = asyncio.create_task(self._hold(attendee["id"]))

    def ensure_all(self) -> None:
        for a in self.store.attendees():
            self.ensure(a)

    def stop(self) -> None:
        for t in self.tasks.values():
            t.cancel()

    async def _hold(self, attendee_id: str) -> None:
        conn_key = f"conn:{attendee_id}"
        state = self.store.kv_get(conn_key) or {"id": str(uuid.uuid4())}
        cursor = state.get("cursor")
        backoff = 1
        while True:
            a = self.store.attendee(id=attendee_id)
            if not a or not a.get("switch_agent_id"):
                return
            url = (f"{self._ws_base}/agents/{a['switch_agent_id']}/connection/ws?connection_id={state['id']}"
                   f"&scope=all&filter=addressed&client=hallway&start_from={cursor if cursor else 'head'}")
            if state.get("generation") is not None:
                url += f"&expected_generation={state['generation']}"
            try:
                async with websockets.connect(
                    url, additional_headers={"Authorization": f"Bearer {a['switch_agent_key']}"},
                    ping_interval=None, open_timeout=15,
                ) as ws:
                    backoff = 1
                    async for raw in ws:
                        # Envelope: {"event": kind, "data": {...}, "id": sequence?}
                        frame = json.loads(raw)
                        kind = frame.get("event")
                        data = frame.get("data") or {}
                        if kind == "ping":
                            await ws.send(json.dumps({"type": "pong", "cursor": cursor or 0}))
                            continue
                        if isinstance(frame.get("id"), int):
                            cursor = frame["id"]
                            state["cursor"] = cursor
                        if kind == "connection_state":
                            state["generation"] = data.get("generation")
                            cursor = data.get("cursor", cursor)
                            state["cursor"] = cursor
                            self.store.kv_set(conn_key, state)
                            log.info("stand-in %s connected", a["agent_name"])
                        elif kind == "gap":
                            cursor = data.get("resumed_at", cursor)
                            state["cursor"] = cursor
                        elif kind == "evicted":
                            log.info("stand-in %s evicted: %s", a["agent_name"], data.get("code"))
                            if data.get("code") == "taken_over":
                                return
                        elif kind == "refused":
                            log.warning("stand-in %s refused: %s", a["agent_name"], data)
                            state.pop("generation", None)
                        elif kind == "message":
                            self._on_addressed(a, data)
                        if kind != "connection_state":
                            self.store.kv_set(conn_key, state)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("stand-in %s socket closed: %r", a["agent_name"], e)
            self.store.kv_set(conn_key, state)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _on_addressed(self, a: dict, event: dict) -> None:
        p = event.get("payload") or {}
        if not p.get("addressed", True):
            return
        sender = p.get("sender_name") or p.get("sender") or "someone"
        if sender in (a["agent_name"], a["handle"], settings.host_agent_name):
            return
        event_id = p.get("id") or event.get("sequence")
        seen_key = f"seen:{a['id']}:{event_id}"
        if event_id and self.store.kv_get(seen_key):
            return
        body = (p.get("body") or p.get("content") or "")[:160]
        who = self.store.one("SELECT name, handle FROM attendees WHERE agent_name = ? OR handle = ?", sender, sender)
        if who:
            sender = who["name"] if sender == who["handle"] else f"{who['name']} (agent)"
        self.store.notice(a["id"], "mention", f"{sender} → your agent: {body}", audience="person")
        if event_id:
            self.store.kv_set(seen_key, True)
