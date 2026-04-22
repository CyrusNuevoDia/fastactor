import pytest

from fastactor.otp import Agent

pytestmark = pytest.mark.anyio


async def test_agent_get_returns_projected_state():
    """G: an Agent with state. W: get projects it. T: the projection is returned."""
    agent = await Agent.start(lambda: {"count": 1})

    assert await agent.get(lambda state: state["count"]) == 1

    await agent.stop("normal")


async def test_agent_update_changes_state():
    """G: an Agent state. W: update mutates it. T: later get sees the new value."""
    agent = await Agent.start(lambda: 1)

    await agent.update(lambda state: state + 2)

    assert await agent.get(lambda state: state) == 3

    await agent.stop("normal")


async def test_agent_get_and_update_returns_reply_and_new_state():
    """G: an Agent state. W: get_and_update runs. T: it returns a reply and stores the new state."""
    agent = await Agent.start(lambda: 5)

    reply = await agent.get_and_update(lambda state: (state, state + 10))

    assert reply == 5
    assert await agent.get(lambda state: state) == 15

    await agent.stop("normal")


async def test_agent_cast_update_applies_before_following_get():
    """G: an Agent state. W: cast_update runs before a later get. T: FIFO ordering makes the get observe it."""
    agent = await Agent.start(lambda: 1)

    agent.cast_update(lambda state: state + 4)

    assert await agent.get(lambda state: state) == 5

    await agent.stop("normal")
