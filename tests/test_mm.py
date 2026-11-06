"""MMClient against a mocked Mattermost: team membership is what lets Switch
resolve a person, so a failed team join must never pass silently."""
import json

import httpx
import pytest

from app.mm import TEAM_HEADROOM, MMClient, MMError

FULL = {"id": "app.team.join_user_to_team.save_member.max_accounts.app_error",
        "message": "the team has reached the limit of members", "status_code": 400}


def mattermost(cap: int, members: set[str], *, join_error: dict | None = None):
    state = {"cap": cap, "members": members, "patches": []}

    def handle(req: httpx.Request) -> httpx.Response:
        path, body = req.url.path.removeprefix("/api/v4"), json.loads(req.content or b"null")
        if path == "/users/login":
            return httpx.Response(200, headers={"Token": "admintok"}, json={})
        if path == "/teams/name/switch":
            return httpx.Response(200, json={"id": "t1"})
        if path == "/users" and req.method == "POST":
            return httpx.Response(201, json={"id": "u_" + body["username"], "username": body["username"]})
        if path == "/teams/t1/members":
            if join_error:
                return httpx.Response(400, json=join_error)
            if len(state["members"]) >= state["cap"]:
                return httpx.Response(400, json=FULL)
            state["members"].add(body["user_id"])
            return httpx.Response(201, json={})
        if path == "/teams/t1/stats":
            return httpx.Response(200, json={"total_member_count": len(state["members"])})
        if path == "/config":
            return httpx.Response(200, json={"TeamSettings": {"MaxUsersPerTeam": state["cap"]}})
        if path == "/config/patch":
            state["patches"].append(body)
            state["cap"] = body["TeamSettings"]["MaxUsersPerTeam"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "unexpected " + path})

    return state, httpx.MockTransport(handle)


def client(transport: httpx.MockTransport) -> MMClient:
    mm = MMClient("http://mm.test")
    mm._http = httpx.AsyncClient(transport=transport)
    return mm


async def test_create_user_raises_a_full_team_cap():
    state, transport = mattermost(cap=2, members={"a", "b"})
    mm = client(transport)
    user, _ = await mm.create_user("asha", "asha@example.com", "Asha")
    assert user["id"] in state["members"]
    assert state["cap"] == 2 + TEAM_HEADROOM


async def test_create_user_surfaces_other_team_join_failures():
    _, transport = mattermost(cap=10, members=set(), join_error={"message": "nope", "status_code": 403})
    with pytest.raises(MMError):
        await client(transport).create_user("asha", "asha@example.com", "Asha")


async def test_ensure_team_capacity_only_ever_raises_the_cap():
    state, transport = mattermost(cap=5000, members=set())
    mm = client(transport)
    assert await mm.ensure_team_capacity(2100) == 5000 and state["patches"] == []
    state["cap"] = 50
    assert await mm.ensure_team_capacity(2100) == 2100
    assert state["patches"] == [{"TeamSettings": {"MaxUsersPerTeam": 2100}}]
