"""
Lesson 02: Synchronous RPC vs. fire-and-forget.

You've seen a Process take messages one-way (Lesson 01). Real agent code
mostly isn't one-way: you want to ask the agent something and wait for the
reply. That's synchronous RPC. You also sometimes want to push telemetry or
log something without blocking on a response. That's fire-and-forget.

Without the actor model, implementing request/reply on an `asyncio.Queue`
means correlating each request with its response by hand -- usually with a
UUID and a `dict[uuid, Future]`. It works. It's also a lot of bookkeeping.

`GenServer` is just `Process` + two well-typed channels bolted on:
  - `await server.call(request)`  -> synchronous, returns the reply, raises on error
  - `server.cast(request)`        -> fire-and-forget, not awaitable, no reply

Prior lesson: 01 (first Process).
New concepts: `GenServer`, `call`, `cast`, `handle_call`, `handle_cast`,
              exception propagation across call().

Read the relevant source:
  - src/fastactor/otp/gen_server.py
"""

from uuid import uuid4

import anyio
import pytest
from anyio import create_task_group
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from helpers import BoomServer, CounterServer, EchoServer

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# The "normal Python" way: RPC on top of an asyncio.Queue.
#
# The worker reads (request_id, payload) tuples; the client stores a Future
# keyed by request_id and awaits it. The worker writes the reply back through
# a reply map. You can see the bookkeeping cost: two data structures, two
# event loops of awareness, and any leak of Futures is a slow memory leak.
# ─────────────────────────────────────────────────────────────────────────────


class ManualRpcWorker:
    def __init__(self) -> None:
        self._inbox_tx: MemoryObjectSendStream[tuple[str, str]]
        self._inbox_rx: MemoryObjectReceiveStream[tuple[str, str]]
        self._inbox_tx, self._inbox_rx = anyio.create_memory_object_stream(
            max_buffer_size=100
        )
        self._pending: dict[str, anyio.Event] = {}
        self._replies: dict[str, str] = {}
        self._stop = anyio.Event()

    async def run(self) -> None:
        async for req_id, payload in self._inbox_rx:
            self._replies[req_id] = payload.upper()
            self._pending[req_id].set()

    async def call(self, payload: str) -> str:
        req_id = uuid4().hex
        done = anyio.Event()
        self._pending[req_id] = done
        await self._inbox_tx.send((req_id, payload))
        await done.wait()
        reply = self._replies.pop(req_id)
        del self._pending[req_id]
        return reply

    async def stop(self) -> None:
        await self._inbox_tx.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_normal_way_needs_manual_correlation():
    """G: a hand-rolled RPC worker. W: we call it. T: it works -- at the cost of a Future registry."""
    worker = ManualRpcWorker()

    async with create_task_group() as tg:
        tg.start_soon(worker.run)

        # The caller must juggle request ids, Future storage, and teardown by hand.
        assert await worker.call("hello") == "HELLO"
        assert await worker.call("world") == "WORLD"

        await worker.stop()


async def test_the_actor_way_has_call_built_in():
    """G: an EchoServer GenServer. W: we call it. T: the reply comes back -- no UUID bookkeeping."""
    server = await EchoServer.start()

    assert await server.call("hello") == "hello"
    assert await server.call({"user": "alice"}) == {"user": "alice"}

    await server.stop("normal")


async def test_cast_is_fire_and_forget():
    """G: an EchoServer. W: we cast three values, then call("sync"). T: all three casts were handled FIFO.

    `cast()` returns immediately -- it does not await a reply. The follow-up
    call acts as a drain: it cannot be handled until every earlier cast has.
    This is the standard "call-after-cast" synchronization pattern.
    """
    server = await EchoServer.start()

    server.cast("telemetry-1")
    server.cast("telemetry-2")
    server.cast("telemetry-3")

    await server.call("sync-me")  # drains the mailbox

    assert server.log == ["telemetry-1", "telemetry-2", "telemetry-3"]

    await server.stop("normal")


async def test_exception_in_handle_call_raises_on_caller():
    """G: a BoomServer. W: we call("boom"). T: the caller sees the handler's RuntimeError.

    Exceptions inside `handle_call` propagate to the caller -- they behave like
    any other function call. (A crash may also kill the server; see Lesson 05
    on crash isolation.)
    """
    server = await BoomServer.start()

    with pytest.raises(RuntimeError, match="Boom!"):
        await server.call("boom")

    # The server is dead after a crash; we won't try to talk to it again.


async def test_call_and_cast_compose_for_counter_pattern():
    """G: a CounterServer. W: mutate via casts, read via call. T: reads see the latest state.

    This is the shape you'll reach for most often: mutate with casts (cheap,
    async), read with calls (synchronous, authoritative). The mailbox FIFO
    guarantees every earlier cast is applied before any later call replies.
    """
    counter = await CounterServer.start(count=0)

    counter.cast(("add", 10))
    counter.cast(("mul", 3))
    counter.cast(("sub", 5))

    assert await counter.call("get") == 25

    await counter.stop("normal")


# You can now talk to an actor both ways: `call` for "I need the answer",
# `cast` for "just do it". Lesson 03 builds on this to show the Agent primitive
# -- a tiny wrapper that makes the most common "hold some state, mutate it
# safely" use case a one-liner.
