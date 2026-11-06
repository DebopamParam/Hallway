"""In-memory stand-ins for Switch and Mattermost.

They share one `World` so the two sides stay consistent the way the real stack
is: a Switch room is a Mattermost channel, Switch agents show up as Mattermost
users named after the agent, and posts from either side land in the same
channel. Only the methods Hallway calls are implemented, with the same
signatures as app/switch.py and app/mm.py. Every call is recorded in
`World.calls` so tests can assert on what reached Switch.
"""
import itertools
import time
from datetime import datetime
from typing import Any

from app.mm import MMError
from app.switch import AgentCreds, SwitchError

_ids = itertools.count(1)


def _ms() -> int:
    # Strictly increasing, so ordering and `since` filters are deterministic.
    return int(time.time() * 1000) + next(_ids)


class World:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}        # mm user id -> user
        self.channels: dict[str, dict] = {}     # channel id -> channel (+ members, posts)
        self.rooms: dict[str, str] = {}         # switch room id -> channel id
        self.agents: dict[str, dict] = {}       # switch agent id -> {name, key, ...}
        self.calls: list[tuple[str, Any]] = []
        # Names Switch can't resolve right now. The live stack does this for
        # freshly created users: add_users answers 200 {"ok": false, "unresolved": [...]}
        # and invite_agent answers {"ok": true} without adding anyone.
        self.unresolvable: set[str] = set()

    # helpers
    def user_by_name(self, username: str) -> dict | None:
        return next((u for u in self.users.values() if u["username"] == username), None)

    def add_user(self, username: str, *, is_bot: bool = False, first_name: str = "", email: str = "") -> dict:
        u = self.user_by_name(username)
        if u is None:
            u = {"id": f"u_{username}", "username": username, "first_name": first_name, "email": email,
                 "is_bot": is_bot}
            self.users[u["id"]] = u
        return u

    def channel_for_room(self, room_id: str) -> dict:
        if room_id not in self.rooms:
            raise SwitchError(404, f"no room {room_id}")
        return self.channels[self.rooms[room_id]]

    def add_post(self, channel: dict, user_id: str, message: str, root_id: str | None = None) -> dict:
        p = {"id": f"p{next(_ids)}", "channel_id": channel["id"], "user_id": user_id, "message": message,
             "create_at": _ms(), "root_id": root_id or "", "type": "", "delete_at": 0}
        channel["posts"].append(p)
        return p

    def all_text_sent_by_agents(self) -> str:
        """Everything any Switch agent posted (what other agents can read)."""
        agent_users = {f"u_{a['name']}" for a in self.agents.values()}
        return "\n".join(p["message"] for c in self.channels.values() for p in c["posts"] if p["user_id"] in agent_users)


class FakeSwitch:
    def __init__(self, world: World) -> None:
        self.w = world

    async def close(self) -> None:
        pass

    def _agent(self, creds: AgentCreds) -> dict:
        a = self.w.agents.get(creds.id)
        if not a or a["key"] != creds.key:
            raise SwitchError(401, "bad agent key")
        return a

    async def register_agent(self, name: str, display_name: str, description: str) -> AgentCreds:
        self.w.calls.append(("register_agent", name))
        existing = next((k for k, a in self.w.agents.items() if a["name"] == name), None)
        aid = existing or f"ag_{name}"
        key = f"key_{name}_{next(_ids)}"  # overwrite=True rotates the key
        self.w.agents[aid] = {"id": aid, "name": name, "display_name": display_name, "key": key}
        self.w.add_user(name, is_bot=True, first_name=display_name)
        return AgentCreds(aid, key)

    async def create_room(self, agent: AgentCreds, name: str, description: str, agent_names: list[str],
                          user_names: list[str]) -> dict:
        host = self._agent(agent)
        self.w.calls.append(("create_room", {"name": name, "agents": agent_names, "users": user_names}))
        cid = f"ch{next(_ids)}"
        rid = f"room{next(_ids)}"
        ch = {"id": cid, "name": name, "display_name": name, "create_at": _ms(), "delete_at": 0,
              "members": set(), "posts": []}
        self.w.channels[cid] = ch
        self.w.rooms[rid] = cid
        for n in [host["name"], *agent_names, *user_names]:
            u = self.w.user_by_name(n)
            if u:
                ch["members"].add(u["id"])
        return {"id": rid, "name": name}

    async def invite_agent(self, agent: AgentCreds, room_id: str, agent_name: str) -> Any:
        self._agent(agent)
        self.w.calls.append(("invite_agent", (room_id, agent_name)))
        u = self.w.user_by_name(agent_name)
        if not u or not u["is_bot"]:
            raise SwitchError(404, f"no agent {agent_name}")
        if agent_name not in self.w.unresolvable:
            self.w.channel_for_room(room_id)["members"].add(u["id"])
        return {"ok": True}

    async def add_users(self, agent: AgentCreds, room_id: str, user_names: list[str]) -> Any:
        self._agent(agent)
        self.w.calls.append(("add_users", (room_id, user_names)))
        ch = self.w.channel_for_room(room_id)
        added, unresolved = [], []
        for n in user_names:
            u = self.w.user_by_name(n)
            if not u or n in self.w.unresolvable:
                unresolved.append(n)
                continue
            ch["members"].add(u["id"])
            added.append(n)
        # The real gateway's shape, recorded from Switch 0.31.0.
        return {"ok": not unresolved, "added": added, "unresolved": unresolved}

    async def post(self, agent: AgentCreds, room_id: str, content: str) -> dict:
        a = self._agent(agent)
        self.w.calls.append(("post", (a["name"], room_id, content)))
        ch = self.w.channel_for_room(room_id)
        uid = f"u_{a['name']}"
        if uid not in ch["members"]:
            raise SwitchError(403, "not a member")
        p = self.w.add_post(ch, uid, content)
        return {"event_id": p["id"]}

    async def read_context(self, agent: AgentCreds, room_id: str, since: str | None = None,
                           limit: int = 50) -> list[dict]:
        self._agent(agent)
        ch = self.w.channel_for_room(room_id)
        since_ms = datetime.fromisoformat(since).timestamp() * 1000 if since else None
        out = []
        for p in ch["posts"]:
            if since_ms is not None and p["create_at"] <= since_ms:
                continue
            out.append({"kind": "message", "id": p["id"], "thread_id": p["root_id"] or p["id"],
                        "sender_name": self.w.users[p["user_id"]]["username"], "body": p["message"],
                        "timestamp": p["create_at"]})
        return out[-limit:]

    async def list_rooms(self, agent: AgentCreds) -> list[dict]:
        return await self.list_all_rooms(agent)

    async def list_all_rooms(self, agent: AgentCreds) -> list[dict]:
        self._agent(agent)
        return [{"id": rid, "name": self.w.channels[cid]["name"]} for rid, cid in self.w.rooms.items()]

    async def op(self, agent: AgentCreds, op_name: str, /, **args: Any) -> Any:
        raise SwitchError(400, f"FakeSwitch has no generic op {op_name}")


class FakeMM:
    def __init__(self, world: World) -> None:
        self.w = world
        self.tokens: dict[str, str] = {}  # token -> user id
        self.passwords: dict[str, str] = {}

    async def close(self) -> None:
        pass

    async def login(self, login_id: str, password: str) -> str:
        u = self.w.user_by_name(login_id)
        if not u or self.passwords.get(u["id"]) != password:
            raise MMError(f"login {login_id}: 401")
        token = f"mmtok{next(_ids)}"
        self.tokens[token] = u["id"]
        return token

    async def _req(self, method: str, path: str, token: str | None = None, body: Any = None, params=None) -> Any:
        if method == "GET" and path.startswith("/users/username/"):
            u = self.w.user_by_name(path.rsplit("/", 1)[1])
            if u:
                return u
        raise MMError(f"{method} {path}: 404")

    async def team_id(self) -> str:
        return "team1"

    async def create_user(self, username: str, email: str, first_name: str) -> tuple[dict, str]:
        self.w.calls.append(("mm_create_user", username))
        u = self.w.add_user(username, first_name=first_name, email=email)
        password = f"pw-{username}-{next(_ids)}"
        self.passwords[u["id"]] = password
        return u, password

    async def join_team(self, user_id: str) -> None:
        self.w.calls.append(("mm_join_team", user_id))

    async def ensure_team_capacity(self, seats: int) -> int:
        return seats

    async def user_by_id(self, user_id: str) -> dict:
        if user_id not in self.w.users:
            raise MMError(f"GET /users/{user_id}: 404")
        return self.w.users[user_id]

    async def channel_by_name(self, name: str) -> dict | None:
        return next((c for c in self.w.channels.values() if c["name"] == name), None)

    async def channels_by_display_name(self, display_name: str) -> list[dict]:
        found = [c for c in self.w.channels.values() if c["display_name"] == display_name]
        return sorted(found, key=lambda c: c["create_at"], reverse=True)

    async def channel(self, channel_id: str) -> dict:
        return self.w.channels[channel_id]

    async def channel_member_ids(self, channel_id: str) -> set[str]:
        return set(self.w.channels[channel_id]["members"])

    async def add_to_channel(self, channel_id: str, user_id: str) -> None:
        self.w.channels[channel_id]["members"].add(user_id)

    async def posts(self, channel_id: str, since_ms: int | None = None, per_page: int = 100) -> list[dict]:
        posts = [p for p in self.w.channels[channel_id]["posts"] if not since_ms or p["create_at"] > since_ms]
        return sorted(posts, key=lambda p: p["create_at"])[-per_page:]

    async def post_as(self, token: str, channel_id: str, message: str, root_id: str | None = None) -> dict:
        uid = self.tokens.get(token)
        if not uid:
            raise MMError("POST /posts: 401")
        ch = self.w.channels[channel_id]
        if uid not in ch["members"]:
            raise MMError("POST /posts: 403")
        return self.w.add_post(ch, uid, message, root_id)
