"""
Lesson 05: Crashes are isolated.

LLM APIs fail. Tool calls time out. JSON parses badly. Every real AI agent is a
long chain of things that can explode. The question every framework has to
answer: when one agent crashes, what happens to the other agents running
beside it?

In a plain `asyncio.gather(*tasks)`, the answer is "all the siblings get
cancelled". That's rarely what you want. With `return_exceptions=True` you
can paper over it, but now you're back to manual error routing.

In the actor model, each actor has its own mailbox and its own task. One
actor's crash takes down its OWN mailbox and state. The OS-of-actors running
beside it keep going. This is the foundation for everything in the next few
lessons (links, monitors, supervisors): fault containment first, then
optional policies for propagating or reacting to the fault.

Prior lessons: 02 (call/cast), 04 (Continue).
New concepts: crash isolation, `Failed("noproc")` after death, `has_stopped()`,
              contrast with `asyncio.gather` cancellation semantics.

Read the relevant source:
  - src/fastactor/otp/_exceptions.py  (Crashed, Failed, Shutdown)
  - src/fastactor/otp/process.py      (terminate, _handle_message error path)
"""

import asyncio

import pytest
from ..otp.helpers import BoomServer, EchoServer

from fastactor.otp import Failed

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# The "normal Python" way: asyncio.gather. One exception cancels the rest.
# ─────────────────────────────────────────────────────────────────────────────


async def _tool_succeed(name: str) -> str:
    await asyncio.sleep(0)  # pretend this is network IO
    return f"{name}-ok"


async def _tool_fail() -> str:
    raise RuntimeError("tool exploded")


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_asyncio_gather_cancels_siblings_on_error():
    """G: three tasks, one crashes. W: asyncio.gather raises. T: no successful results are returned by default.

    With plain gather, a single raise in any sibling propagates up and the
    gather returns nothing -- even if the other two tasks would have finished
    successfully. You can work around it with `return_exceptions=True`, but
    then YOU are responsible for separating wins from losses.
    """
    with pytest.raises(RuntimeError, match="tool exploded"):
        await asyncio.gather(
            _tool_succeed("a"),
            _tool_fail(),
            _tool_succeed("b"),
        )


async def test_asyncio_gather_return_exceptions_requires_manual_unpacking():
    """G: same three tasks. W: return_exceptions=True. T: we must sort successes from exceptions ourselves."""
    results = await asyncio.gather(
        _tool_succeed("a"),
        _tool_fail(),
        _tool_succeed("b"),
        return_exceptions=True,
    )

    wins = [r for r in results if not isinstance(r, BaseException)]
    losses = [r for r in results if isinstance(r, BaseException)]
    assert wins == ["a-ok", "b-ok"]
    assert len(losses) == 1
    assert isinstance(losses[0], RuntimeError)


async def test_actor_crash_does_not_touch_other_actors():
    """G: a BoomServer and an EchoServer. W: the BoomServer crashes. T: the EchoServer is unaffected.

    This is the core isolation property. The actor that crashed dies. Every
    OTHER actor in the runtime carries on with its own mailbox and state
    intact -- no cancellation cascade.
    """
    boom = await BoomServer.start()
    echo = await EchoServer.start()

    with pytest.raises(RuntimeError, match="Boom!"):
        await boom.call("boom")

    await boom.stopped()  # the crashed server is done
    assert boom.has_stopped()

    # The peer is completely untouched:
    assert await echo.call("hello") == "hello"
    assert await echo.call("still-alive") == "still-alive"

    await echo.stop("normal")


async def test_calling_a_dead_actor_raises_noproc():
    """G: a crashed BoomServer. W: another call. T: we get a `Failed("noproc")`, not hang or a misleading error.

    Once an actor is dead, its mailbox is closed. Subsequent sends fail fast
    with a clear reason. No silent hangs, no "why did this hang for 30
    seconds" mysteries.
    """
    boom = await BoomServer.start()

    with pytest.raises(RuntimeError, match="Boom!"):
        await boom.call("boom")

    await boom.stopped()

    with pytest.raises(Failed) as exc_info:
        await boom.call("boom")
    assert "noproc" in str(exc_info.value)


# Isolation is the baseline. But sometimes you DO want to react when an actor
# dies -- an orchestrator agent that spawned a worker wants to know when the
# worker is gone, and maybe respawn it. Lesson 06 introduces `monitor` and
# `link`, the two ways an actor can say "tell me when that other actor dies."
