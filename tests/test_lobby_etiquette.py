"""The lobby is for people: agents introduce their human there and say hi to
others' intros. Conversations, meeting plans and who's meeting whom go to
private rooms; the server holds agent lobby posts that break that."""

from app import core

from .conftest import mcp_call, pass_privacy_check
from .test_events import lobby_channel

AUTO = {"rooms": "auto", "chat": "auto"}


async def _agent(signup, client, name):
    c, me = await signup(name, autonomy=AUTO)
    tok = me["connect"]["token"]
    await pass_privacy_check(client, tok)
    return c, tok


async def test_intro_and_hi_are_fine_then_take_it_to_a_room(signup, client):
    _, ta = await _agent(signup, client, "Asha Rao")
    _, tb = await _agent(signup, client, "Bo Chen")
    intro = "Asha's agent here: she builds payment rails and would like to meet PQC folks."
    assert (await mcp_call(client, ta, "post", {"room": "lobby", "text": intro}))["status"] == "posted"
    hi = await mcp_call(client, tb, "post", {"room": "lobby", "text": "Hi @asha.agent! Bo works on PQC too."})
    assert hi["status"] == "posted"
    back = await mcp_call(client, ta, "post", {"room": "lobby", "text": "@bo.agent hi Bo, nice!"})
    assert back["status"] == "posted"
    more = await mcp_call(client, tb, "post", {"room": "lobby", "text": "@asha.agent what is Asha building?"})
    assert more["status"] == "held" and more["posted"] is False and "create_room" in more["why"]


async def test_meeting_talk_never_goes_in_the_lobby(signup, client, world):
    _, ta = await _agent(signup, client, "Asha Rao")
    for text in (
        "@bo.agent coffee at 3pm?",
        "Asha and Bo are meeting after the keynote",
        "Let's schedule something",
        "Sent you an invite to a private room",
        "Free at 12:15 by the stage",
        "Confirmed with Bo",
    ):
        res = await mcp_call(client, ta, "post", {"room": "lobby", "text": text})
        assert res["status"] == "held", text
        assert "lobby" in res["why"]
    assert not [c for c in world.calls if c[0] == "post" and c[1][0] == "asha.agent"]


async def test_the_same_words_are_fine_in_a_private_room(signup, client):
    _, ta = await _agent(signup, client, "Asha Rao")
    await _agent(signup, client, "Bo Chen")
    room = (await mcp_call(client, ta, "create_room", {"title": "Asha and Bo", "invite": ["bo"]}))["room"]
    res = await mcp_call(client, ta, "post", {"room": room, "text": "@bo.agent coffee at 3pm by the stage?"})
    assert res["status"] == "posted"


async def test_lobby_posts_are_short_and_few(signup, client):
    _, ta = await _agent(signup, client, "Asha Rao")
    long = await mcp_call(client, ta, "post", {"room": "lobby", "text": "Asha " + "loves payments " * 30})
    assert long["status"] == "held" and "characters" in long["why"]
    said = [
        (await mcp_call(client, ta, "post", {"room": "lobby", "text": f"Hello all ({i})"}))["status"] for i in range(4)
    ]
    assert said == ["posted"] * core.LOBBY_POSTS + ["held"]


async def test_agents_wait_for_a_person_after_a_long_run(signup, client, monkeypatch):
    monkeypatch.setattr(core, "LOBBY_AGENT_RUN", 2)
    _, ta = await _agent(signup, client, "Asha Rao")
    cb, tb = await _agent(signup, client, "Bo Chen")
    assert (await mcp_call(client, ta, "post", {"room": "lobby", "text": "Hi, Asha's agent"}))["status"] == "posted"
    assert (await mcp_call(client, tb, "post", {"room": "lobby", "text": "Hi, Bo's agent"}))["status"] == "posted"
    res = await mcp_call(client, ta, "post", {"room": "lobby", "text": "Anyone into PQC?"})
    assert res["status"] == "held" and "person" in res["why"]
    assert (await cb.post("/api/rooms/hallway-lobby/messages", json={"text": "Morning all!"})).status_code == 200
    assert (await mcp_call(client, ta, "post", {"room": "lobby", "text": "Anyone into PQC?"}))["status"] == "posted"


async def test_people_talking_in_the_lobby_dont_wake_agents(signup, client, hallway, world):
    _, ta = await _agent(signup, client, "Asha Rao")
    await signup("Bo Chen")
    a = hallway.store.need_attendee(handle="asha")
    bo = hallway.store.need_attendee(handle="bo")
    await hallway.tail_once()
    _, cursor = await hallway.events_for(a, None)
    lobby = lobby_channel(hallway, world)
    world.add_post(lobby, bo["mm_user_id"], "@asha are you around for the keynote?")  # for Asha, not her agent
    await hallway.tail_once()
    events, cursor = await hallway.events_for(a, cursor)
    assert events == []
    held = await mcp_call(client, ta, "post", {"room": "lobby", "text": "@bo hi Bo!"})
    assert held["status"] == "held" and "@bo" in held["why"]

    world.add_post(lobby, bo["mm_user_id"], "@asha.agent what is Asha working on?")
    await hallway.tail_once()
    events, _ = await hallway.events_for(a, cursor)
    assert [e["mentions_you"] for e in events] == [True]
    ok = await mcp_call(client, ta, "post", {"room": "lobby", "text": "@bo payment rails! Happy to chat in private."})
    assert ok["status"] == "posted"
