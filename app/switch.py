"""Thin client for the Switch agent protocol (HTTP side only).

Stand-in agents are registered `always_on` and Hallway holds one WebSocket per
stand-in (see presence.py), so Switch treats them as present: no "I read
asynchronously" auto-replies. The attendee's own agent reads and answers on its
next check-in through MCP.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

STANDIN_PROFILE = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
}


class SwitchError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"Switch answered {status}: {detail[:300]}")


@dataclass(frozen=True)
class AgentCreds:
    id: str
    key: str


class SwitchClient:
    def __init__(self, base_url: str = settings.switch_url):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, token: str, body: Any = None) -> Any:
        r = await self._http.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise SwitchError(r.status_code, r.text)
        return r.json() if r.content else None

    async def health(self) -> None:
        """Raises unless Switch answers its /health with a 2xx within 3 s."""
        r = await self._http.get("/health", timeout=3)
        if r.status_code >= 300:
            raise SwitchError(r.status_code, r.text)

    async def register_agent(self, name: str, display_name: str, description: str) -> AgentCreds:
        res = await self._send(
            "POST",
            "/agents",
            settings.switch_registration_key,
            {
                "name": name,
                "display_name": display_name,
                "description": description,
                "connector_type": "hallway",
                "integration_profile": STANDIN_PROFILE,
                "overwrite": True,
            },
        )
        return AgentCreds(res["id"], res["api_key"])

    async def op(self, agent: AgentCreds, op_name: str, /, **args: Any) -> Any:
        res = await self._send("POST", f"/agents/{agent.id}/ops/{op_name}", agent.key, args)
        return res.get("result") if isinstance(res, dict) else res

    async def post(self, agent: AgentCreds, room_id: str, content: str) -> dict:
        res: dict = await self._send(
            "POST", f"/agents/{agent.id}/message", agent.key, {"room_id": room_id, "content": content}
        )
        return res

    async def delete_agent(self, agent: AgentCreds) -> None:
        """Delete an agent Hallway registered, with its own key (an agent may
        delete itself). Switch drops its room memberships and bridge identity."""
        await self._send("DELETE", f"/agents/{agent.id}", agent.key)

    async def read_context(
        self, agent: AgentCreds, room_id: str, since: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Flattened, oldest first: messages only (joins dropped)."""
        args: dict[str, Any] = {"room_id": room_id, "limit": limit}
        if since:
            args["since"] = since
        res = await self.op(agent, "read_context", **args)
        out: list[dict] = []
        for thread in res.get("threads", []):
            root = thread["root"]
            for entry in [root, *thread.get("replies", [])]:
                if entry.get("kind") != "message":
                    continue
                out.append({**entry, "thread_id": root["id"]})
        out.sort(key=lambda e: e["timestamp"])
        return out

    async def create_room(
        self, agent: AgentCreds, name: str, description: str, agent_names: list[str], user_names: list[str]
    ) -> dict:
        # The creating agent is not added implicitly; the host must be in every
        # room it manages, so it always lists itself.
        room: dict = await self.op(
            agent,
            "create_room",
            name=name,
            description=description,
            agent_names=[settings.host_agent_name, *agent_names],
            user_names=user_names,
        )
        return room

    async def invite_agent(self, agent: AgentCreds, room_id: str, agent_name: str) -> Any:
        return await self.op(agent, "invite_agent_to_room", room_id=room_id, agent_name=agent_name)

    async def add_users(self, agent: AgentCreds, room_id: str, user_names: list[str]) -> Any:
        return await self.op(agent, "add_users_to_room", room_id=room_id, user_names=user_names)

    async def list_rooms(self, agent: AgentCreds) -> list[dict]:
        res = await self.op(agent, "list_rooms")
        return res if isinstance(res, list) else res.get("rooms", [])

    async def list_all_rooms(self, agent: AgentCreds) -> list[dict]:
        res = await self.op(agent, "list_all_rooms")
        return res if isinstance(res, list) else res.get("rooms", [])


class GatewayClient:
    """Cookie-authenticated admin calls (trust settings)."""

    def __init__(self, base_url: str = settings.switch_url):
        self._http = httpx.AsyncClient(base_url=base_url + "/gateway", timeout=30)

    async def login(self) -> None:
        r = await self._http.post(
            "/auth/login",
            json={"email": settings.gateway_admin_email, "password": settings.gateway_admin_password},
        )
        r.raise_for_status()

    async def get_trust_settings(self) -> dict:
        r = await self._http.get("/trust-settings")
        r.raise_for_status()
        settings_: dict = r.json()
        return settings_

    async def put_trust_settings(self, endpoint: str, policy_id: str, api_key: str) -> dict:
        r = await self._http.put(
            "/trust-settings", json={"endpoint": endpoint, "policy_id": policy_id, "api_key": api_key}
        )
        if r.status_code >= 400:
            raise SwitchError(r.status_code, r.text)
        updated: dict = r.json()
        return updated

    async def set_agent_greetings(self, enabled: bool) -> int:
        """Turn agents' join greetings on/off for every collaboration bridge."""
        r = await self._http.get("/collaborations")
        r.raise_for_status()
        data = r.json()
        bridges = data if isinstance(data, list) else data.get("bridges", data.get("items", []))
        for b in bridges:
            u = await self._http.patch(f"/collaborations/{b['bridge_id']}", json={"agent_greetings_enabled": enabled})
            if u.status_code >= 400:
                raise SwitchError(u.status_code, u.text)
        return len(bridges)

    async def close(self) -> None:
        await self._http.aclose()
