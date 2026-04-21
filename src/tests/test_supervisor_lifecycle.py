from typing import cast

import pytest
import anyio
from anyio import fail_after
from anyio.lowlevel import checkpoint

from fastactor.otp import Failed, GenServer
from support import (
    BoomServer,
    CounterServer,
    OrderObserver,
    SlowStopServer,
    await_child_restart,
)


pytestmark = pytest.mark.anyio


class TerminationRecorder(GenServer):
    async def init(self, label: str) -> None:
        self.label = label

    async def terminate(self, reason) -> None:
        OrderObserver.record(self.label)
        await super().terminate(reason)


async def await_process_stop(process, timeout: float = 2):
    with fail_after(timeout):
        while not process.has_stopped():
            await checkpoint()
        await process.stopped()
    return process


async def crash_child(child) -> None:
    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await await_process_stop(child)


async def test_start_child_registers_spec_and_rejects_duplicate_ids(make_supervisor):
    """G: a supervisor with one child id in use. W: we start a duplicate id. T: the duplicate is rejected."""
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", GenServer, restart="temporary")
    )

    rows = sup.which_children()

    assert rows == [
        {
            "id": "worker",
            "process": child,
            "restart": "temporary",
            "shutdown": 5,
            "type": "worker",
            "significant": False,
        }
    ]
    with pytest.raises(Failed, match="Child with id=worker already exists"):
        await sup.start_child(sup.child_spec("worker", GenServer))


async def test_terminate_child_marks_process_undefined_and_allows_delete(
    make_supervisor,
):
    """G: a running child. W: terminate_child is called. T: the spec remains and the process becomes undefined."""
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", GenServer, restart="temporary")
    )

    await sup.terminate_child("worker")

    assert child.has_stopped()
    assert sup.which_children()[0]["process"] == ":undefined"
    sup.delete_child("worker")
    assert sup.which_children() == []


async def test_delete_child_rejects_running_process(make_supervisor):
    """G: a running child. W: delete_child is called too early. T: the supervisor rejects it."""
    sup = await make_supervisor()
    await sup.start_child(sup.child_spec("worker", GenServer, restart="temporary"))

    with pytest.raises(Failed, match="still running"):
        sup.delete_child("worker")


async def test_restart_child_replaces_terminated_process(make_supervisor):
    """G: a terminated child spec. W: restart_child is called. T: a fresh process replaces it."""
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", GenServer, restart="temporary")
    )

    await sup.terminate_child("worker")
    restarted = await sup.restart_child("worker")

    assert restarted is not child
    assert sup.children["worker"].process is restarted


async def test_slow_shutdown_child_falls_back_to_kill(make_supervisor):
    """G: a child that lingers in terminate. W: terminate_child uses a tiny shutdown timeout. T: it still finishes."""
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", SlowStopServer, restart="temporary", shutdown=0)
    )

    await sup.terminate_child("worker")

    assert child.has_stopped()
    assert sup.which_children()[0]["process"] == ":undefined"


async def test_max_restarts_stops_supervisor_after_limit(make_supervisor):
    """G: a supervisor with a low restart cap. W: the same child crashes twice quickly. T: the supervisor stops."""
    sup = await make_supervisor(max_restarts=1, max_seconds=5)
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    await crash_child(child)
    child = await await_child_restart(sup, "worker", child)
    await crash_child(child)

    with fail_after(2):
        while not sup.has_stopped():
            await checkpoint()

    assert sup.has_stopped()


async def test_max_restarts_window_resets_after_max_seconds(make_supervisor):
    """G: a low restart cap with a short window. W: crashes are spaced beyond the window. T: the supervisor survives."""
    sup = await make_supervisor(max_restarts=1, max_seconds=0.01)
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    await crash_child(child)
    child = await await_child_restart(sup, "worker", child)
    await anyio.sleep(
        0.02
    )  # Real time is required here to let the restart-intensity window expire.
    await crash_child(child)
    await await_child_restart(sup, "worker", child)

    assert not sup.has_stopped()


async def test_reverse_order_shutdown_on_supervisor_stop(make_supervisor):
    """G: three children started in order. W: the supervisor stops. T: children terminate in reverse order."""
    OrderObserver.reset()
    sup = await make_supervisor()
    await sup.start_child(
        sup.child_spec(
            "first", TerminationRecorder, args=("first",), restart="temporary"
        )
    )
    await sup.start_child(
        sup.child_spec(
            "second", TerminationRecorder, args=("second",), restart="temporary"
        )
    )
    await sup.start_child(
        sup.child_spec(
            "third", TerminationRecorder, args=("third",), restart="temporary"
        )
    )

    await sup.stop("normal")

    assert OrderObserver.snapshot() == ["third", "second", "first"]


async def test_supervisor_seeds_children_at_start():
    """G: Supervisor.start with children=[(Cls, kwargs), ...]. W: the supervisor starts. T: seeded children are running and counted."""
    from fastactor.otp import Supervisor

    sup = await Supervisor.start(
        children=[
            (CounterServer, {"count": 3}),
        ],
    )

    counts = sup.count_children()
    assert counts["active"] == 1
    assert counts["specs"] == 1
    assert counts["workers"] == 1
    assert counts["supervisors"] == 0

    child = cast(CounterServer, sup.children["CounterServer"].process)
    assert await child.call("get") == 3

    await sup.stop("normal")


async def test_count_children_categorizes_workers_and_supervisors(make_supervisor):
    """G: a supervisor with a worker and nested supervisor child. W: count_children runs. T: workers and supervisors are counted separately."""
    from fastactor.otp import Supervisor
    from support import CounterServer

    sup = await make_supervisor()

    await sup.start_child(sup.child_spec("w1", CounterServer))
    await sup.start_child(sup.child_spec("sub", Supervisor, type="supervisor"))

    counts = sup.count_children()

    assert counts["specs"] == 2
    assert counts["active"] == 2
    assert counts["workers"] == 1
    assert counts["supervisors"] == 1
