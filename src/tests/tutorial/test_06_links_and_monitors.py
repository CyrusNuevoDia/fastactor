"""
Lesson 06: Links and monitors -- watching other actors.

Isolation (Lesson 05) says a crash in one actor doesn't touch others. But
an orchestrator agent that spawned a worker often DOES want to react: "the
summarizer worker died -- log it, maybe respawn." The actor model gives you
two primitives for this, and they mean slightly different things:

  - `monitor(other)` : ONE-WAY. You ask "tell me when they die." You get a
                       `Down` message in your mailbox. They have no idea
                       you're watching.

  - `link(other)`    : TWO-WAY. "We live and die together." By default, if
                       either side crashes, the other crashes too. Set
                       `trap_exits=True` at start and the crash arrives as
                       an `Exit` message instead -- no cascade.

In plain asyncio, the closest analogues are `task.add_done_callback(...)` and
`asyncio.shield(...)`. They're both fine for toy examples and painful at any
scale, because lifetimes and propagation aren't first-class.

Prior lessons: 05 (crash isolation).
New concepts: `monitor`, `demonitor`, `link`, `unlink`, `trap_exits=True`,
              `Down` messages, `Exit` messages, `handle_down`, `handle_exit`.

Read the relevant source:
  - src/fastactor/otp/process.py  (link/monitor/trap_exits logic)
  - src/fastactor/otp/_messages.py (Down, Exit)
"""

import pytest
from support import BoomServer, EchoServer, LinkServer, MonitorServer

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_monitor_delivers_down_message_when_target_exits_normally():
    """G: monitor watching an EchoServer. W: target stops with "normal". T: Down arrives with reason="normal"."""
    watcher = await MonitorServer.start()
    target = await EchoServer.start()

    ref = watcher.monitor(target)
    assert isinstance(ref, str)

    await target.stop("normal")

    downs = await watcher.await_events(1)
    assert len(downs) == 1
    assert downs[0].sender is target
    assert downs[0].reason == "normal"

    await watcher.stop("normal")


async def test_monitor_delivers_down_message_when_target_crashes():
    """G: monitor watching a BoomServer. W: target crashes. T: Down arrives with the exception as reason."""
    watcher = await MonitorServer.start()
    target = await BoomServer.start()

    watcher.monitor(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    downs = await watcher.await_events(1)
    assert downs[0].sender is target
    assert isinstance(downs[0].reason, RuntimeError)
    assert "Boom!" in str(downs[0].reason)

    await watcher.stop("normal")


async def test_monitor_is_unidirectional_watcher_survives():
    """G: monitor watching a target. W: target crashes. T: the watcher lives on.

    Monitors don't cascade. The dead target signals the watcher -- that's it.
    The watcher keeps processing messages.
    """
    watcher = await MonitorServer.start()
    target = await BoomServer.start()

    watcher.monitor(target)

    with pytest.raises(RuntimeError):
        await target.call("boom")

    await watcher.await_events(1)
    assert not watcher.has_stopped()

    await watcher.stop("normal")


async def test_link_without_trap_exits_cascades_the_crash():
    """G: two live servers linked together. W: one crashes. T: the peer crashes too.

    Without trap_exits, linking is an "all or nothing" contract: either we
    both live through this, or we both die. Useful when two actors MUST be
    co-present for correctness.
    """
    a = await EchoServer.start()
    b = await BoomServer.start()
    a.link(b)

    with pytest.raises(RuntimeError, match="Boom!"):
        await b.call("boom")

    await a.stopped()
    await b.stopped()
    assert a.has_stopped()
    assert b.has_stopped()


async def test_link_with_trap_exits_receives_exit_message_and_survives():
    """G: a LinkServer with trap_exits=True, linked to a BoomServer. W: boom crashes. T: watcher sees Exit, lives.

    `trap_exits=True` flips the default: crash signals from links arrive as
    `Exit` messages in the mailbox, routed to `handle_exit`. The watcher
    keeps running. This is the orchestrator pattern -- you want to know
    about the death AND keep operating.
    """
    watcher = await LinkServer.start(trap_exits=True)
    target = await BoomServer.start()
    watcher.link(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    exits = await watcher.await_events(1)
    assert exits[0].sender is target
    assert isinstance(exits[0].reason, RuntimeError)
    assert not watcher.has_stopped()

    await watcher.stop("normal")


# Monitors and trap_exits give you the raw signals. But handling "child died
# -> restart child -> keep intensity budget" by hand is a lot of bookkeeping.
# Lesson 07 introduces `Supervisor`, which encodes those policies declaratively.
