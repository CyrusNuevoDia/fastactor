from time import monotonic

import pytest
from anyio import Event, fail_after, sleep
from anyio.lowlevel import checkpoint
from .helpers import BoomServer, LinkServer, MonitorServer, OrderLog

from fastactor.otp import (
    Call,
    Continue,
    Crashed,
    Exit,
    GenServer,
    Ignore,
    Info,
    Process,
    Runtime,
)

pytestmark = pytest.mark.anyio


class RecordingProcess(Process):
    async def init(self) -> None:
        self.events: list[object] = []
        self.flushed = Event()

    async def handle_info(self, message: Info) -> None:
        self.events.append(message.message)
        if message.message == "flush":
            self.flushed.set()


class IgnoreProcess(Process):
    async def init(self) -> Ignore:
        return Ignore()


class InitContinueProcess(Process):
    async def init(self, payload: str = "boot") -> Continue:
        self.events: list[object] = ["init"]
        self.flushed = Event()
        return Continue(payload)

    async def handle_continue(self, term: str) -> None:
        self.events.append(("continue", term))

    async def handle_info(self, message: Info) -> None:
        self.events.append(("info", message.message))
        if message.message == "flush":
            self.flushed.set()


class InfoContinueProcess(Process):
    async def init(self) -> None:
        self.events: list[object] = []
        self.flushed = Event()

    async def handle_continue(self, term: str) -> None:
        self.events.append(("continue", term))

    async def handle_info(self, message: Info):
        if message.message == "continue":
            self.events.append(("info", message.message))
            return Continue("from-info")
        self.events.append(("info", message.message))
        if message.message == "flush":
            self.flushed.set()
        return None


class ExitRecorderProcess(Process):
    async def init(self) -> None:
        self.exit_msgs: list[Exit] = []
        self.exit_event = Event()

    async def handle_exit(self, message: Exit) -> None:
        self.exit_msgs.append(message)
        self.exit_event.set()


class OrderedCrashServer(GenServer):
    async def init(self, label: str) -> None:
        self.label = label

    async def handle_call(self, call: Call) -> None:
        if call.message == "boom":
            raise RuntimeError(self.label)

    async def terminate(self, reason):
        OrderLog.record(self.label)
        await super().terminate(reason)


async def await_process_stop(process: Process, timeout: float = 2) -> Process:
    with fail_after(timeout):
        while not process.has_stopped():
            await checkpoint()
        await process.stopped()
    return process


async def test_process_start_stop_unregisters_from_runtime(runtime: Runtime):
    """G: a started process. W: it stops normally. T: runtime unregisters it."""
    proc = await RecordingProcess.start()

    assert proc.id in runtime.processes

    await proc.stop("normal")

    assert proc.has_stopped()
    assert proc.id not in runtime.processes
    assert proc._crash_exc is None


async def test_process_kill_unregisters_from_runtime(runtime: Runtime):
    """G: a started process. W: it is killed. T: runtime unregisters it."""
    proc = await RecordingProcess.start()

    await proc.kill()

    assert proc.has_stopped()
    assert proc.id not in runtime.processes


async def test_process_init_ignore_short_circuits_spawn(runtime: Runtime):
    """G: a process whose init returns Ignore. W: runtime spawns it. T: spawn returns 'ignore' cleanly."""
    proc = IgnoreProcess(supervisor=runtime.supervisor)

    result = await runtime.spawn(proc)

    assert result == "ignore"
    assert proc.has_started()
    assert proc.has_stopped()
    assert proc.id not in runtime.processes


async def test_process_info_routes_to_handle_info():
    """G: a recording process. W: it receives infos. T: handle_info records them in order."""
    proc = await RecordingProcess.start()

    proc.info("hello")
    proc.info("flush")
    await proc.flushed.wait()

    assert proc.events == ["hello", "flush"]

    await proc.stop("normal")


async def test_process_init_continue_runs_before_first_info():
    """G: a process whose init returns Continue. W: the first info arrives. T: continue runs first."""
    proc = await InitContinueProcess.start("boot")

    proc.info("flush")
    await proc.flushed.wait()

    assert proc.events == ["init", ("continue", "boot"), ("info", "flush")]

    await proc.stop("normal")


async def test_process_handle_info_continue_runs_before_next_info():
    """G: a process returning Continue from handle_info. W: two infos arrive. T: continue runs between them."""
    proc = await InfoContinueProcess.start()

    proc.info("continue")
    proc.info("flush")
    await proc.flushed.wait()

    assert proc.events == [
        ("info", "continue"),
        ("continue", "from-info"),
        ("info", "flush"),
    ]

    await proc.stop("normal")


async def test_process_abnormal_link_exit_crashes_untrapped_peer():
    """G: two linked untrapped processes. W: one stops fatally. T: the peer crashes too."""
    left = await RecordingProcess.start()
    right = await RecordingProcess.start()
    left.link(right)

    await right.stop("fatal")
    await await_process_stop(right)
    await await_process_stop(left)

    assert isinstance(left._crash_exc, Crashed)
    assert left._crash_exc.reason == "fatal"


async def test_process_trap_exits_receives_exit_message_and_survives():
    """G: a trapped process linked to a peer. W: the peer stops fatally. T: an Exit arrives and it survives."""
    watcher = await ExitRecorderProcess.start(trap_exits=True)
    target = await RecordingProcess.start()
    watcher.link(target)

    await target.stop("fatal")
    await await_process_stop(target)
    await watcher.exit_event.wait()

    assert not watcher.has_stopped()
    assert [message.reason for message in watcher.exit_msgs] == ["fatal"]

    await watcher.stop("normal")


async def test_process_normal_link_exit_does_not_crash_peer():
    """G: two linked untrapped processes. W: one stops normally. T: the peer keeps running."""
    left = await RecordingProcess.start()
    right = await RecordingProcess.start()
    left.link(right)

    await right.stop("normal")
    await await_process_stop(right)
    for _ in range(3):
        await checkpoint()

    assert not left.has_stopped()

    await left.stop("normal")


async def test_init_returning_stop_crashes_with_reason():
    """G: a Process whose init returns Stop(sender, reason). W: start is called. T: the process stops with that shutdown reason."""
    from fastactor.otp import Stop
    from fastactor.otp._exceptions import Shutdown

    class StopEarly(Process):
        async def init(self):
            return Stop(None, "bad_config")

    with pytest.raises(Shutdown) as exc_info:
        await StopEarly.start()

    assert exc_info.value.reason == "bad_config"


async def test_on_terminate_runs_before_monitors_receive_down():
    """G: a Process that records cleanup in on_terminate. W: it stops. T: monitors observe Down after cleanup ran."""
    records = []

    class Recorder(Process):
        async def on_terminate(self, reason):
            records.append(("cleanup", reason))

    proc = await Recorder.start()
    monitor = await MonitorServer.start()
    monitor.monitor(proc)

    await proc.stop("normal")
    downs = await monitor.await_events(1, timeout=2)

    assert records == [("cleanup", "normal")]
    assert len(downs) == 1

    await monitor.stop("normal")


# ---------------------------------------------------------------------------
# §2 Process primitives conformance
# ---------------------------------------------------------------------------


async def test_2_1_spawn_returns_live_pid() -> None:
    """SPEC §2.1: Spawn returns a live process identifier.

    Source: https://www.erlang.org/doc/system/ref_man_processes.html
    """
    start = monotonic()
    proc = await GenServer.start()
    elapsed = monotonic() - start

    assert elapsed < 0.5  # generous envelope; spec suggests 10ms
    assert proc.has_started()
    assert not proc.has_stopped()

    await proc.stop("normal")


async def test_2_2_normal_exit_does_not_propagate_across_links() -> None:
    """SPEC §2.2: Normal exit does not propagate across links.

    Source: https://www.erlang.org/doc/system/ref_man_processes.html
    """
    a = await GenServer.start()
    b = await GenServer.start()
    a.link(b)

    await b.stop("normal")
    await await_process_stop(b)
    await sleep(0.1)

    assert not a.has_stopped()

    await a.stop("normal")


async def test_2_3_abnormal_exit_propagates_from_b_to_a() -> None:
    """SPEC §2.3: Abnormal exit propagates across links (B crashes → A exits).

    Source: https://www.erlang.org/doc/system/ref_man_processes.html
    """
    a = await GenServer.start()
    b = await BoomServer.start()
    a.link(b)

    with pytest.raises(RuntimeError, match="Boom!"):
        await b.call("boom")

    await await_process_stop(b)
    await await_process_stop(a)

    assert isinstance(a._crash_exc, RuntimeError)


async def test_2_3_abnormal_exit_propagates_from_a_to_b() -> None:
    """SPEC §2.3: Abnormal exit propagates across links (A crashes → B exits).

    Source: https://www.erlang.org/doc/system/ref_man_processes.html
    """
    a = await BoomServer.start()
    b = await GenServer.start()
    a.link(b)

    with pytest.raises(RuntimeError, match="Boom!"):
        await a.call("boom")

    await await_process_stop(a)
    await await_process_stop(b)

    assert isinstance(b._crash_exc, RuntimeError)


async def test_2_4_trap_exit_converts_abnormal_signal_to_mailbox_message() -> None:
    """SPEC §2.4: With trap_exit, abnormal exit signals become Exit mailbox messages.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#process_flag/2
    """
    watcher = await LinkServer.start(trap_exits=True)
    target = await BoomServer.start()
    watcher.link(target)

    with pytest.raises(RuntimeError, match="Boom!"):
        await target.call("boom")

    await await_process_stop(target)
    exits = await watcher.await_events(1)

    assert exits[0].sender is target
    assert isinstance(exits[0].reason, RuntimeError)
    assert not watcher.has_stopped()

    await watcher.stop("normal")


async def test_2_4_trap_exit_converts_normal_signal_when_linked() -> None:
    """SPEC §2.4: Trapping converts ALL exit signals (including 'normal') when linked.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#process_flag/2
    """
    watcher = await LinkServer.start(trap_exits=True)
    target = await GenServer.start()
    watcher.link(target)

    await target.stop("normal")
    await await_process_stop(target)
    exits = await watcher.await_events(1)

    assert exits[0].sender is target
    assert exits[0].reason == "normal"

    await watcher.stop("normal")


async def test_2_5_kill_terminates_recipient_even_when_trapping() -> None:
    """SPEC §2.5: kill signal always terminates the recipient regardless of trap_exit.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#exit/2
    """
    victim = await GenServer.start(trap_exits=True)

    await victim.kill()
    await await_process_stop(victim)

    assert victim.has_stopped()


async def test_2_5_linked_trap_receives_killed_reason() -> None:
    """SPEC §2.5: A linked trapping third party sees the reason as 'killed' (not 'kill').

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#exit/2
    """
    watcher = await LinkServer.start(trap_exits=True)
    target = await GenServer.start()
    watcher.link(target)

    await target.kill()
    await await_process_stop(target)
    exits = await watcher.await_events(1)

    assert exits[0].sender is target
    assert exits[0].reason == "killed"

    await watcher.stop("normal")


async def test_2_6_monitor_returns_unique_ref() -> None:
    """SPEC §2.6: monitor(process, Pid) returns a unique reference.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#monitor/2
    """
    watcher = await MonitorServer.start()
    target = await GenServer.start()
    ref = watcher.monitor(target)

    assert isinstance(ref, str)

    await target.stop("normal")
    await watcher.stop("normal")


async def test_2_6_monitor_delivers_single_down_on_normal_exit() -> None:
    """SPEC §2.6: Monitor is one-shot — exactly one DOWN on pid's exit.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#monitor/2
    """
    watcher = await MonitorServer.start()
    target = await GenServer.start()
    ref = watcher.monitor(target)

    await target.stop("normal")
    await await_process_stop(target)
    downs = await watcher.await_events(1)

    assert len(downs) == 1
    assert downs[0].ref == ref
    assert downs[0].sender is target
    assert downs[0].reason == "normal"

    await watcher.stop("normal")


async def test_2_6_monitor_on_already_dead_pid_immediate_down_noproc() -> None:
    """SPEC §2.6: Monitoring a non-existent pid yields immediate DOWN with reason `noproc`.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#monitor/2
    """
    target = await GenServer.start()
    await target.stop("normal")
    await await_process_stop(target)

    watcher = await MonitorServer.start()
    ref = watcher.monitor(target)

    downs = await watcher.await_events(1, timeout=0.5)
    assert len(downs) == 1
    assert downs[0].ref == ref
    assert downs[0].reason == "noproc"

    await watcher.stop("normal")


async def test_2_6_monitor_same_pid_twice_yields_two_refs_and_two_downs() -> None:
    """SPEC §2.6: Monitoring same pid twice yields two distinct refs and two DOWN messages.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#monitor/2
    """
    watcher = await MonitorServer.start()
    target = await GenServer.start()
    ref_a = watcher.monitor(target)
    ref_b = watcher.monitor(target)

    assert ref_a is not None and ref_b is not None
    assert ref_a != ref_b

    await target.stop("normal")
    await await_process_stop(target)
    downs = await watcher.await_events(2, timeout=0.5)
    assert len(downs) == 2
    assert {down.ref for down in downs} == {ref_a, ref_b}

    await watcher.stop("normal")


async def test_2_6_demonitor_with_flush_prevents_down_delivery() -> None:
    """SPEC §2.6: demonitor(ref, [flush]) prevents any DOWN reaching the mailbox.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#demonitor/2
    """
    watcher = await MonitorServer.start()
    target = await GenServer.start()
    ref = watcher.monitor(target)
    watcher.demonitor(ref, flush=True)

    await target.stop("normal")
    await await_process_stop(target)
    await sleep(0.1)

    assert watcher.down_msgs == []

    await watcher.stop("normal")


async def test_2_7_unlink_is_symmetric() -> None:
    """SPEC §2.7: After unlink, neither party receives exit signals from the other.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#unlink/1
    """
    a = await GenServer.start()
    b = await BoomServer.start()
    a.link(b)
    a.unlink(b)

    with pytest.raises(RuntimeError, match="Boom!"):
        await b.call("boom")

    await await_process_stop(b)
    await sleep(0.1)

    assert not a.has_stopped()

    await a.stop("normal")


# ---------------------------------------------------------------------------
# Links and monitors — additional scenarios
# ---------------------------------------------------------------------------


async def test_monitor_delivers_down_on_crash() -> None:
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


async def test_multiple_monitors_each_receive_down_message() -> None:
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


async def test_trapped_link_and_monitor_both_receive_notifications() -> None:
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


async def test_chain_crash_propagates_in_link_order() -> None:
    """G: a linked X -> Y -> Z chain. W: Z crashes. T: shutdown propagates Z then Y then X."""
    OrderLog.reset()
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

    assert OrderLog.snapshot() == ["z", "y", "x"]
    assert str(x._crash_exc) == "z"
    assert str(y._crash_exc) == "z"
    assert str(z._crash_exc) == "z"
