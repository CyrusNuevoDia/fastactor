import pytest
from anyio import fail_after
from anyio.lowlevel import checkpoint
from support import BoomServer, LinkServer, MonitorServer, OrderObserver

from fastactor.otp import Call, GenServer

pytestmark = pytest.mark.anyio


class OrderedCrashServer(GenServer):
    async def init(self, label: str) -> None:
        self.label = label

    async def handle_call(self, call: Call) -> None:
        if call.message == "boom":
            raise RuntimeError(self.label)

    async def terminate(self, reason):
        OrderObserver.record(self.label)
        await super().terminate(reason)


async def await_process_stop(process, timeout: float = 2):
    with fail_after(timeout):
        while not process.has_stopped():
            await checkpoint()
        await process.stopped()
    return process


async def test_abnormal_linked_exit_crashes_untrapped_process():
    """G: two linked untrapped processes. W: one crashes. T: the peer crashes too."""
    left = await GenServer.start()
    right = await BoomServer.start()
    left.link(right)

    with pytest.raises(RuntimeError, match="Boom!"):
        await right.call("boom")

    await await_process_stop(right)
    await await_process_stop(left)

    assert isinstance(left._crash_exc, RuntimeError)


async def test_normal_linked_exit_does_not_crash_untrapped_process():
    """G: two linked untrapped processes. W: one exits normally. T: the peer stays alive."""
    left = await GenServer.start()
    right = await GenServer.start()
    left.link(right)

    await right.stop("normal")
    await await_process_stop(right)
    for _ in range(3):
        await checkpoint()

    assert not left.has_stopped()

    await left.stop("normal")


async def test_trap_exits_server_collects_exit_messages():
    """G: a trapping LinkServer linked to a crashing peer. W: the peer crashes. T: an Exit is recorded."""
    watcher = await LinkServer.start(trap_exits=True)
    target = await BoomServer.start()
    watcher.link(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    await await_process_stop(target)
    exits = await watcher.await_events(1)

    assert exits[0].sender is target
    assert str(exits[0].reason) == "Boom!"
    assert not watcher.has_stopped()

    await watcher.stop("normal")


async def test_monitor_server_collects_down_on_normal_stop():
    """G: a monitor observing a peer. W: the peer stops normally. T: a Down arrives with reason normal."""
    watcher = await MonitorServer.start()
    target = await GenServer.start()
    watcher.monitor(target)

    await target.stop("normal")
    await await_process_stop(target)
    downs = await watcher.await_events(1)

    assert downs[0].sender is target
    assert downs[0].reason == "normal"

    await watcher.stop("normal")


async def test_monitor_server_collects_down_on_crash():
    """G: a monitor observing a peer. W: the peer crashes. T: a Down arrives with the exception reason."""
    watcher = await MonitorServer.start()
    target = await BoomServer.start()
    watcher.monitor(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    await await_process_stop(target)
    downs = await watcher.await_events(1)

    assert downs[0].sender is target
    assert isinstance(downs[0].reason, RuntimeError)

    await watcher.stop("normal")


async def test_demonitor_prevents_future_down_messages():
    """G: a monitor that demonitor()s first. W: the target stops. T: no Down is delivered."""
    watcher = await MonitorServer.start()
    target = await GenServer.start()
    watcher.monitor(target)
    watcher.demonitor(target)

    await target.stop("normal")
    await await_process_stop(target)
    for _ in range(5):
        await checkpoint()

    assert watcher.down_msgs == []

    await watcher.stop("normal")


async def test_unlink_prevents_future_exit_propagation():
    """G: two linked processes that unlink. W: one crashes. T: the other is unaffected."""
    watcher = await LinkServer.start(trap_exits=True)
    target = await BoomServer.start()
    watcher.link(target)
    watcher.unlink(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    await await_process_stop(target)
    for _ in range(5):
        await checkpoint()

    assert watcher.exits_received == []
    assert not watcher.has_stopped()

    await watcher.stop("normal")


async def test_multiple_monitors_each_receive_down_message():
    """G: several monitors observing one peer. W: the peer crashes. T: every monitor receives Down."""
    monitors = [await MonitorServer.start() for _ in range(3)]
    target = await BoomServer.start()
    for watcher in monitors:
        watcher.monitor(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    await await_process_stop(target)
    for watcher in monitors:
        downs = await watcher.await_events(1)
        assert downs[0].sender is target
        assert isinstance(downs[0].reason, RuntimeError)
        await watcher.stop("normal")


async def test_trapped_link_and_monitor_both_receive_notifications():
    """G: one process both links and monitors a peer. W: the peer crashes. T: it receives Exit and Down."""
    monitor = await MonitorServer.start(trap_exits=True)
    linker = await LinkServer.start(trap_exits=True)
    target = await BoomServer.start()
    linker.link(target)
    monitor.monitor(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    await await_process_stop(target)
    downs = await monitor.await_events(1)
    exits = await linker.await_events(1)

    assert downs[0].sender is target
    assert exits[0].sender is target
    assert not monitor.has_stopped()
    assert not linker.has_stopped()

    await monitor.stop("normal")
    await linker.stop("normal")


async def test_chain_crash_propagates_in_link_order():
    """G: a linked X -> Y -> Z chain. W: Z crashes. T: shutdown propagates Z then Y then X."""
    OrderObserver.reset()
    x = await OrderedCrashServer.start("x")
    y = await OrderedCrashServer.start("y")
    z = await OrderedCrashServer.start("z")
    x.link(y)
    y.link(z)

    with pytest.raises(RuntimeError, match="z"):
        await z.call("boom")

    await await_process_stop(z)
    await await_process_stop(y)
    await await_process_stop(x)

    assert OrderObserver.snapshot() == ["z", "y", "x"]
    assert str(x._crash_exc) == "z"
    assert str(y._crash_exc) == "z"
    assert str(z._crash_exc) == "z"
