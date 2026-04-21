import pytest
from anyio import Event, fail_after
from anyio.lowlevel import checkpoint

from fastactor.otp import Continue, Crashed, Exit, Ignore, Info, Process, Runtime


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
    """G: a process whose init returns Ignore. W: runtime spawns it. T: spawn aborts cleanly."""
    proc = IgnoreProcess(supervisor=runtime.supervisor)

    with pytest.raises(RuntimeError, match="Process crashed before it could start"):
        await runtime.spawn(proc)

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
    from fastactor.otp import Process, Stop
    from fastactor.otp._exceptions import Shutdown

    class StopEarly(Process):
        async def init(self):
            return Stop(None, "bad_config")

    with pytest.raises(Shutdown) as exc_info:
        await StopEarly.start()

    assert exc_info.value.reason == "bad_config"


async def test_on_terminate_runs_before_monitors_receive_down():
    """G: a Process that records cleanup in on_terminate. W: it stops. T: monitors observe Down after cleanup ran."""
    from fastactor.otp import Process
    from support import MonitorServer

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
