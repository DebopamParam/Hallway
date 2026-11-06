"""Mattermost REST client: Hallway's hidden message engine.

People post and read through Mattermost (so Switch sees every human message via
its bridge); the admin session handles users, membership and reads.
"""

import contextlib
import secrets
from typing import Any

import httpx

from .config import settings

# Extra team seats to add when Mattermost's per-team cap is hit mid-event.
TEAM_HEADROOM = 200


class MMError(RuntimeError):
    pass


class MMClient:
    def __init__(self, base_url: str = settings.mm_url):
        self._base = base_url.rstrip("/") + "/api/v4"
        self._http = httpx.AsyncClient(timeout=30)
        self._admin_token: str | None = None
        self._team_id: str | None = None
        self._user_cache: dict[str, dict] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def login(self, login_id: str, password: str) -> str:
        r = await self._http.post(f"{self._base}/users/login", json={"login_id": login_id, "password": password})
        if r.status_code >= 400:
            raise MMError(f"login {login_id}: {r.status_code} {r.text[:200]}")
        return r.headers["Token"]

    async def _req(
        self, method: str, path: str, token: str | None = None, body: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        if token is None:
            if self._admin_token is None:
                self._admin_token = await self.login(settings.mm_admin_user, settings.mm_admin_password)
            token = self._admin_token
        r = await self._http.request(
            method, self._base + path, json=body, params=params, headers={"Authorization": f"Bearer {token}"}
        )
        if r.status_code == 401 and token == self._admin_token:
            self._admin_token = None
            return await self._req(method, path, None, body, params)
        if r.status_code >= 400:
            raise MMError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    async def team_id(self) -> str:
        if self._team_id is None:
            team = await self._req("GET", f"/teams/name/{settings.mm_team}")
            self._team_id = team["id"]
        return self._team_id

    async def create_user(self, username: str, email: str, first_name: str) -> tuple[dict, str]:
        password = "Hw!" + secrets.token_urlsafe(18)
        try:
            user = await self._req(
                "POST",
                "/users",
                body={"username": username, "email": email, "password": password, "first_name": first_name},
            )
        except MMError as e:
            if "exists" not in str(e):
                raise
            user = await self._req("GET", f"/users/username/{username}")
            if user.get("delete_at"):  # deactivated when an organiser removed its last owner
                await self._req("PUT", f"/users/{user['id']}/active", body={"active": True})
            await self._req("PUT", f"/users/{user['id']}/password", body={"new_password": password})
        await self.join_team(user["id"])
        return user, password

    async def join_team(self, user_id: str) -> None:
        """Put a user in the event team (idempotent).

        Switch resolves `add_users_to_room` names through a directory search
        scoped to this team, and Mattermost only lets team members into its
        channels, so a user left outside the team can never join a room. The
        usual cause is Mattermost's per-team cap (TeamSettings.MaxUsersPerTeam,
        50 by default), which every attendee spends two seats of: the person
        and their agent's bot account. Hitting it raises the cap and tries once
        more; any other failure is raised, never swallowed."""
        tid = await self.team_id()
        body = {"team_id": tid, "user_id": user_id}
        try:
            await self._req("POST", f"/teams/{tid}/members", body=body)
        except MMError as e:
            if "max_accounts" not in str(e):
                raise
            stats = await self._req("GET", f"/teams/{tid}/stats")
            await self.ensure_team_capacity(int(stats.get("total_member_count") or 0) + TEAM_HEADROOM)
            await self._req("POST", f"/teams/{tid}/members", body=body)

    async def deactivate_user(self, user_id: str) -> None:
        """Take a user out of the event team (freeing its seat) and deactivate
        it: it can't sign in, and its posts stay. A user already gone is fine."""
        tid = await self.team_id()
        with contextlib.suppress(MMError):  # not (or no longer) in the team
            await self._req("DELETE", f"/teams/{tid}/members/{user_id}")
        await self._req("DELETE", f"/users/{user_id}")
        self._user_cache.pop(user_id, None)

    async def retire_user(self, user_id: str) -> None:
        """Free a removed attendee's email (its account keeps its posts, under
        a placeholder address), so the same person can join again as a new user."""
        await self._req("PUT", f"/users/{user_id}/patch", body={"email": f"removed-{user_id}@hallway.invalid"})
        self._user_cache.pop(user_id, None)

    async def disable_bot(self, user_id: str) -> None:
        """Deactivate an agent's bot account (Switch leaves it active when the
        agent is deleted)."""
        await self._req("POST", f"/bots/{user_id}/disable")
        self._user_cache.pop(user_id, None)

    async def user_by_username(self, username: str) -> dict | None:
        try:
            user: dict = await self._req("GET", f"/users/username/{username}")
        except MMError:
            return None
        return user

    async def ensure_team_capacity(self, seats: int) -> int:
        """Raise Mattermost's per-team user cap to at least `seats`; returns the cap."""
        cfg = await self._req("GET", "/config")
        cap = int((cfg.get("TeamSettings") or {}).get("MaxUsersPerTeam") or 0)
        if cap < seats:
            await self._req("PUT", "/config/patch", body={"TeamSettings": {"MaxUsersPerTeam": seats}})
            cap = seats
        return cap

    async def user_by_id(self, user_id: str) -> dict:
        if user_id not in self._user_cache:
            self._user_cache[user_id] = await self._req("GET", f"/users/{user_id}")
        return self._user_cache[user_id]

    async def channel_by_name(self, name: str) -> dict | None:
        tid = await self.team_id()
        try:
            channel: dict = await self._req("GET", f"/teams/{tid}/channels/name/{name}")
        except MMError:
            return None
        return channel

    async def channels_by_display_name(self, display_name: str) -> list[dict]:
        """Switch suffixes the channel name when a room name is reused, so the
        display name is the only stable key. Newest first."""
        tid = await self.team_id()
        found = await self._req("POST", f"/teams/{tid}/channels/search", body={"term": display_name})
        found = [c for c in found or [] if c["display_name"] == display_name and not c.get("delete_at")]
        return sorted(found, key=lambda c: c["create_at"], reverse=True)

    async def channel(self, channel_id: str) -> dict:
        channel: dict = await self._req("GET", f"/channels/{channel_id}")
        return channel

    async def channel_member_ids(self, channel_id: str) -> set[str]:
        members = await self._req("GET", f"/channels/{channel_id}/members", params={"per_page": 200})
        return {m["user_id"] for m in members}

    async def add_to_channel(self, channel_id: str, user_id: str) -> None:
        await self._req("POST", f"/channels/{channel_id}/members", body={"user_id": user_id})

    async def remove_from_channel(self, channel_id: str, user_id: str) -> None:
        await self._req("DELETE", f"/channels/{channel_id}/members/{user_id}")

    async def posts(self, channel_id: str, since_ms: int | None = None, per_page: int = 100) -> list[dict]:
        params: dict[str, Any] = {"per_page": per_page}
        if since_ms:
            params["since"] = since_ms
        data = await self._req("GET", f"/channels/{channel_id}/posts", params=params)
        posts = [data["posts"][pid] for pid in data.get("order", []) if pid in data["posts"]]
        if since_ms:  # `since` returns an unordered map without `order` on some versions
            posts = list(data["posts"].values())
        posts = [p for p in posts if not p.get("delete_at")]
        posts.sort(key=lambda p: p["create_at"])
        return posts

    async def post_as(self, token: str, channel_id: str, message: str, root_id: str | None = None) -> dict:
        body = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        post: dict = await self._req("POST", "/posts", token=token, body=body)
        return post
