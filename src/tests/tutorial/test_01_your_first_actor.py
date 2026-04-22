"""
Lesson 01: A Process is just a loop.

Every actor framework, every agent framework, eventually rebuilds the same
shape: a loop that reads messages off a queue and dispatches them to a handler.
If you've written an LLM agent with asyncio and an `asyncio.Queue` plus a
`while True: msg = await q.get(); ...` loop, you've built a Process. The
fastactor `Process` is that loop, already written, with lifecycle hooks.

This lesson introduces the bare `Process` subclass -- no `call` / `cast` yet,
just the fundamental shape: initialize, receive messages, clean up on exit.
Every other primitive in the library (`GenServer`, `Agent`, `Supervisor`,
`Task`...) is built on top of this.

Prior lesson: 00 (why actors).
New concepts: `Process`, `handle_info`, `terminate`, `info()`, `stop()`,
              FIFO mailbox ordering, the `started()` / `stopped()` events.

Read the relevant source:
  - src/fastactor/otp/process.py
"""

from typing import Any

from anyio import Event, fail_after
import pytest

from fastactor.otp import Info, Process

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# A minimal Process: three hooks, one mailbox.
#
# - init(): runs exactly once before the loop reads any messages. Good place
#           for state setup. (It can also return Stop / Ignore / Continue --
#           those are later lessons.)
# - handle_info(): runs once per incoming Info message, FIFO.
# - terminate(): runs exactly once when the process is stopping, regardless of
#                whether it stopped cleanly or crashed.
# ─────────────────────────────────────────────────────────────────────────────


class LoggingProcess(Process):
    async def init(self, label: str = "proc") -> None:
        self.label = label
        self.messages: list[Any] = []
        self.terminated_with: Any = None

    async def handle_info(self, message: Info) -> None:
        self.messages.append(message.message)

    async def terminate(self, reason: Any) -> None:
        self.terminated_with = reason
        await super().terminate(reason)


class SignalOnMessage(Process):
    """A Process that sets an Event once it has seen N messages. Useful for
    deterministic synchronization -- we never sleep to wait for mailbox state."""

    async def init(self, target: int = 1) -> None:
        self.target = target
        self.messages: list[Any] = []
        self.reached = Event()

    async def handle_info(self, message: Info) -> None:
        self.messages.append(message.message)
        if len(self.messages) >= self.target:
            self.reached.set()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_process_starts_and_stops_cleanly():
    """G: a LoggingProcess. W: we start and stop it with reason "normal". T: terminate sees "normal"."""
    proc = await LoggingProcess.start(label="hello")

    assert proc.label == "hello"
    assert proc.messages == []

    await proc.stop("normal")

    assert proc.terminated_with == "normal"


async def test_info_message_routes_to_handle_info():
    """G: a SignalOnMessage waiting for 1 message. W: we send one Info. T: handle_info records it."""
    proc = await SignalOnMessage.start(target=1)

    proc.info("hello")

    with fail_after(2):
        await proc.reached.wait()

    assert proc.messages == ["hello"]

    await proc.stop("normal")


async def test_mailbox_preserves_fifo_order():
    """G: a SignalOnMessage waiting for 3 messages. W: send three Info messages. T: they arrive in the order sent."""
    proc = await SignalOnMessage.start(target=3)

    proc.info("first")
    proc.info("second")
    proc.info("third")

    with fail_after(2):
        await proc.reached.wait()

    assert proc.messages == ["first", "second", "third"]

    await proc.stop("normal")


async def test_terminate_runs_exactly_once_on_stop():
    """G: a LoggingProcess. W: we stop it. T: terminate saw the reason once -- not repeatedly."""
    proc = await LoggingProcess.start()

    await proc.stop("normal")
    # stop() is idempotent; calling it again on an already-stopped process is a no-op.
    await proc.stop("normal")

    assert proc.terminated_with == "normal"


# You now have the raw actor shape: a mailbox + a loop + a handler + a
# terminator. This is everything a single agent needs. But we haven't shown
# how to talk TO the actor in the request/reply sense yet -- right now we only
# have one-way `info`. Lesson 02 upgrades to `GenServer`, which adds `call`
# (synchronous RPC) and `cast` (fire-and-forget with a typed reply contract).
