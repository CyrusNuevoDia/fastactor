"""
Lesson 00: Why actors?

If you're here because you want to build AI agents and someone mentioned "the
actor model", this is the lesson that says why you should care. An AI agent is
long-running, stateful, takes messages, produces replies. In plain async Python,
sharing that state across concurrent callers is trickier than it looks --
specifically because operations that LOOK atomic stop being atomic the moment
you `await` something in the middle.

This file demonstrates the pain and the fix. The "normal Python" ChatAgent
loses messages under concurrent access because a read-modify-write crosses an
`await` point. The "actor way" ChatAgent -- same logic, same await -- never
loses messages, because the mailbox serializes handling: the next message is
not read until the current handler returns.

New concepts: mailbox, serialized handling, `GenServer`, `handle_cast`,
`handle_call`, call-after-cast drain.

Read the relevant source:
  - src/fastactor/otp/process.py   (mailbox + message loop)
  - src/fastactor/otp/gen_server.py (call / cast / handle_*)
"""

from anyio import create_task_group
from anyio.lowlevel import checkpoint
import pytest

from fastactor.otp import Call, Cast, GenServer

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# The "normal Python" way: a plain class with shared mutable state.
# The bug: `add_message` reads -> awaits -> writes. Any other coroutine that
# touches `self.history` during the await will see stale data and clobber us.
# ─────────────────────────────────────────────────────────────────────────────


class ChatAgent:
    def __init__(self) -> None:
        self.history: list[str] = []

    async def _persist(self, snapshot: list[str]) -> None:
        # Stand-in for a real IO write (DB, file, remote store). Any await here
        # is a scheduling point where the loop can run another coroutine.
        await checkpoint()

    async def add_message(self, msg: str) -> None:
        current = list(self.history)  # read
        await self._persist(current)  # ← scheduling point
        current.append(msg)
        self.history = current  # write (clobbers any interleaved write)


# ─────────────────────────────────────────────────────────────────────────────
# The "actor way": wrap the same state in a GenServer. The handler body below
# is byte-for-byte the same read-await-write, but the mailbox guarantees the
# next cast isn't even dequeued until this handler returns -- so there is no
# other coroutine to interleave with us.
# ─────────────────────────────────────────────────────────────────────────────


class ChatAgentActor(GenServer):
    async def init(self) -> None:
        self.history: list[str] = []

    async def _persist(self, snapshot: list[str]) -> None:
        await checkpoint()

    async def handle_cast(self, cast: Cast) -> None:
        tag, msg = cast.message
        if tag == "add":
            current = list(self.history)  # read
            await self._persist(current)  # ← same scheduling point -- but safe
            current.append(msg)
            self.history = current  # write

    async def handle_call(self, call: Call) -> list[str]:
        # A non-None reply on "sync" lets callers use call-after-cast as a drain.
        if call.message == "sync":
            return list(self.history)
        raise ValueError(call.message)


async def _enqueue(agent: ChatAgentActor, msg: str) -> None:
    agent.cast(("add", msg))


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_normal_way_loses_messages():
    """G: a plain ChatAgent. W: 10 concurrent add_message calls. T: most messages are lost.

    All 10 coroutines read an empty history before any of them reach their write.
    Each then writes back a list of length 1 (just their own message). The final
    history has far fewer than 10 entries -- this is the silent read-modify-write
    race that shows up as soon as state mutation crosses an `await` boundary.
    """
    agent = ChatAgent()

    async with create_task_group() as tg:
        for i in range(10):
            tg.start_soon(agent.add_message, f"msg-{i}")

    assert len(agent.history) < 10, (
        f"expected a race to lose messages; got {len(agent.history)}/10 preserved"
    )


async def test_the_actor_way_preserves_every_message():
    """G: a ChatAgentActor. W: 10 concurrent casts. T: all 10 messages end up in history.

    Casts are fire-and-forget; they queue in the mailbox. The actor handles them
    one at a time -- the `await _persist(...)` inside the handler cannot be
    interrupted by another handler, because the mailbox is single-consumer.
    """
    agent = await ChatAgentActor.start()

    async with create_task_group() as tg:
        for i in range(10):
            tg.start_soon(_enqueue, agent, f"msg-{i}")

    # call-after-cast: a successful call reply proves every prior cast has drained.
    history = await agent.call("sync")
    assert len(history) == 10
    assert sorted(history) == sorted(f"msg-{i}" for i in range(10))

    await agent.stop("normal")


async def test_mailbox_serializes_handlers():
    """G: a ChatAgentActor. W: two casts whose handlers each read-await-write. T: both complete cleanly.

    This is the invariant in one sentence: one mailbox message is handled to
    completion before the next is pulled. No locks; no "remember to guard".
    """
    agent = await ChatAgentActor.start()

    agent.cast(("add", "first"))
    agent.cast(("add", "second"))

    history = await agent.call("sync")
    assert history == ["first", "second"]

    await agent.stop("normal")


# Next up -> test_01_your_first_actor.py. You've just used a `GenServer` (an
# actor with `call` / `cast`). Lesson 01 zooms in on the bare `Process`
# underneath it -- the loop, the mailbox, and the lifecycle hooks that every
# other primitive in fastactor is built on.
