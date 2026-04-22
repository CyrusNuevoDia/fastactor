"""
Lesson 03: State without locks, with `Agent`.

The #1 thing every AI agent needs: persistent conversation/memory state that
multiple handlers read and write. You could write a `GenServer` every time
(Lesson 02 style), but 80% of the time the shape is just "I want to hold this
value and let callers mutate it safely." `Agent` is the shorthand for that.

Contrast: the "normal Python" version is a shared dict guarded by an
`anyio.Lock`. Works -- if you remember to hold the lock across every read,
await, and write. Forget once, and you lose data silently. This lesson shows
both the honest race (forgetting to hold the lock) and the fixed version
(holding it correctly), then shows that the actor-model equivalent needs no
lock at all.

Prior lessons: 00 (why), 02 (call/cast).
New concepts: `Agent`, `Agent.start(factory)`, `get`, `update`,
              `get_and_update`, `cast_update`.

Read the relevant source:
  - src/fastactor/otp/agent.py
"""

from anyio import Lock, create_task_group
from anyio.lowlevel import checkpoint
import pytest

from fastactor.otp import Agent

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# The "normal Python" way: a dict + anyio.Lock. NEVER threading.Lock in an
# async coroutine -- that blocks the event loop and defeats the purpose.
#
# The bug below is the classic one: if you only grab the lock for the write
# but not for the read-await-write as a whole, two coroutines can both read
# the same stale value and one wins the clobber race.
# ─────────────────────────────────────────────────────────────────────────────


class LockyMemory:
    def __init__(self) -> None:
        self.store: dict[str, list[str]] = {"messages": []}
        self.lock = Lock()

    async def _trim_to_token_budget(self, msgs: list[str]) -> list[str]:
        # Stand-in for a real async step (e.g. counting tokens, summarizing).
        await checkpoint()
        return msgs

    async def append_forgetful(self, msg: str) -> None:
        # BUG: lock only held for the write. The read-await step before it is
        # unprotected, so two coroutines can interleave at `_trim_to_token_budget`.
        current = self.store["messages"]
        trimmed = await self._trim_to_token_budget(list(current))
        async with self.lock:
            self.store["messages"] = trimmed + [msg]

    async def append_correctly(self, msg: str) -> None:
        # Hold the lock for the WHOLE critical section. This works -- at the
        # cost of remembering to do it on every handler.
        async with self.lock:
            current = self.store["messages"]
            trimmed = await self._trim_to_token_budget(list(current))
            self.store["messages"] = trimmed + [msg]


# ─────────────────────────────────────────────────────────────────────────────
# The "actor way": Agent.start(factory). The factory is called once inside the
# actor's init. Callers use `get` / `update` / `get_and_update`. Each call is
# serialized by the actor's mailbox -- no lock, no possibility of forgetting.
# ─────────────────────────────────────────────────────────────────────────────


async def _trim_to_token_budget(msgs: list[str]) -> list[str]:
    await checkpoint()
    return msgs


async def _append_to_state(
    state: dict[str, list[str]], msg: str
) -> dict[str, list[str]]:
    trimmed = await _trim_to_token_budget(list(state["messages"]))
    return {"messages": trimmed + [msg]}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_normal_way_loses_data_when_lock_is_forgotten():
    """G: LockyMemory. W: 10 concurrent append_forgetful. T: messages are lost to the read-await-write race.

    This is the honest asyncio race: the await at `_trim_to_token_budget` is a
    scheduling point. Every coroutine reads the same stale list, then each
    writes back its own "snapshot + one". Only the last writer's entry survives.
    """
    memory = LockyMemory()

    async with create_task_group() as tg:
        for i in range(10):
            tg.start_soon(memory.append_forgetful, f"msg-{i}")

    assert len(memory.store["messages"]) < 10


async def test_the_lock_works_when_held_correctly():
    """G: LockyMemory. W: 10 concurrent append_correctly (lock held across the await). T: all 10 present.

    This is the "it's fine if you do it right" version. The lesson isn't that
    locks don't work -- they do -- it's that they require discipline on every
    handler, and the failure mode is silent.
    """
    memory = LockyMemory()

    async with create_task_group() as tg:
        for i in range(10):
            tg.start_soon(memory.append_correctly, f"msg-{i}")

    assert len(memory.store["messages"]) == 10


async def test_the_actor_way_preserves_every_write():
    """G: an Agent holding a message list. W: 10 concurrent updates. T: all 10 present. No lock in sight.

    `update(fn)` runs `fn(state)` inside the actor and stores the return as
    the new state. The mailbox serializes everything -- the next `update`
    won't run until this one returns.
    """
    memory = await Agent.start(lambda: {"messages": []})

    async def _push(msg: str) -> None:
        await memory.update(lambda state, m=msg: _append_to_state(state, m))

    async with create_task_group() as tg:
        for i in range(10):
            tg.start_soon(_push, f"msg-{i}")

    snapshot = await memory.get(lambda state: list(state["messages"]))
    assert len(snapshot) == 10
    assert sorted(snapshot) == sorted(f"msg-{i}" for i in range(10))

    await memory.stop("normal")


async def test_get_and_update_is_atomic_read_then_write():
    """G: an Agent holding a counter. W: get_and_update "increment, return old". T: caller sees the OLD value.

    `get_and_update(fn)` returns `(reply, new_state)` atomically. Useful when
    you need the prior value as part of the same operation -- e.g. "give me
    the next sequence number and bump the counter."
    """
    counter = await Agent.start(lambda: 0)

    first = await counter.get_and_update(lambda n: (n, n + 1))
    second = await counter.get_and_update(lambda n: (n, n + 1))
    third = await counter.get_and_update(lambda n: (n, n + 1))

    assert (first, second, third) == (0, 1, 2)
    assert await counter.get(lambda n: n) == 3

    await counter.stop("normal")


async def test_cast_update_is_fire_and_forget():
    """G: an Agent. W: we cast_update then call get to drain. T: the cast mutation is visible.

    `cast_update` is the cheap cousin of `update`: no reply, so the caller
    doesn't wait. Useful for hot paths where you don't need confirmation.
    The following `get` call drains the mailbox before replying.
    """
    memory = await Agent.start(lambda: {"messages": []})

    memory.cast_update(lambda state: {"messages": state["messages"] + ["cheap"]})
    memory.cast_update(lambda state: {"messages": state["messages"] + ["fast"]})

    snapshot = await memory.get(lambda state: list(state["messages"]))
    assert snapshot == ["cheap", "fast"]

    await memory.stop("normal")


# Next: `handle_continue`. Sometimes an actor needs to do work AFTER replying
# but BEFORE taking the next message -- e.g. reply to the caller, then run a
# cleanup step while the mailbox is still paused. Lesson 04 covers that.
