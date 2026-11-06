"""Anyone can address a stand-in (QA #12). Switch registers agents answering
their owner only, and people post from Mattermost accounts no Switch user has
claimed, so a person tagging someone's agent drew Switch's public refusal in
that agent's name instead of an addressed event (and a mention notice)."""

from app import core


async def test_a_new_stand_in_is_opened_to_anyone(signup, world):
    await signup("Asha Rao")
    [agent] = [a for a in world.agents.values() if a["name"] == "asha.agent"]
    assert agent["addressing"] == "anyone"
    [call] = [c for c in world.calls if c[0] == "op" and c[3].get("agent_id") == agent["id"]]
    assert call[1:3] == ("update_agent_detail", "hallway")  # by the host: an agent can't widen its own


async def test_stand_ins_registered_before_are_opened_once_at_start(signup, hallway, world):
    await signup("Asha Rao")
    [agent] = [a for a in world.agents.values() if a["name"] == "asha.agent"]
    agent["addressing"] = "owner_and_owner_agents"  # as registered by an older Hallway
    hallway.store.kv_pop(core.ADDRESSING_OPENED)
    await hallway.start()
    assert agent["addressing"] == "anyone"
    assert hallway.store.kv_get(core.ADDRESSING_OPENED)
    agent["addressing"] = "owner_and_owner_agents"
    await hallway.start()  # done once: the flag is set
    assert agent["addressing"] == "owner_and_owner_agents"
