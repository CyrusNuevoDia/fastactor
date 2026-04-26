"""Supervisor tests: lifecycle management and OTP §6 conformance."""

from typing import Any, cast

import pytest
from anyio import fail_after, sleep
from anyio.lowlevel import checkpoint
from .helpers import (
    BoomServer,
    CounterServer,
    OrderLog,
    SlowStopServer,
    await_child_restart,
)

from fastactor.otp import Failed, GenServer, Shutdown, Supervisor

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helper classes
# ---------------------------------------------------------------------------


class TerminationRecorder(GenServer):
    async def init(self, label: str) -> None:
        self.label = label

    async def terminate(self, reason: str | Shutdown | Exception) -> None:
        OrderLog.record(self.label)
        await super().terminate(reason)


class LabeledBoom(BoomServer):
    async def init(self, label: str, on_init: bool = False) -> None:  # ty: ignore[invalid-method-override]  # type: ignore[override]
        self.label = label
        await super().init(on_init=on_init)

    async def terminate(self, reason: str | Shutdown | Exception) -> None:
        OrderLog.record(self.label)
        await super().terminate(reason)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


async def await_process_stop(process, timeout: float = 2):
    with fail_after(timeout):
        while not process.has_stopped():
            await checkpoint()
        await process.stopped()
    return process


async def _crash(child) -> None:
    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await await_process_stop(child)


async def _start_boom_child(supervisor, child_id: str, restart: str = "permanent"):
    return await supervisor.start_child(
        supervisor.child_spec(child_id, BoomServer, restart=restart)
    )


# ---------------------------------------------------------------------------
# Lifecycle management
# ---------------------------------------------------------------------------


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


async def test_supervisor_seeds_children_at_start():
    """G: Supervisor.start with children=[(Cls, kwargs), ...]. W: the supervisor starts. T: seeded children are running and counted."""
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
    sup = await make_supervisor()

    await sup.start_child(sup.child_spec("w1", CounterServer))
    await sup.start_child(sup.child_spec("sub", Supervisor, type="supervisor"))

    counts = sup.count_children()

    assert counts["specs"] == 2
    assert counts["active"] == 2
    assert counts["workers"] == 1
    assert counts["supervisors"] == 1


# ---------------------------------------------------------------------------
# §6.1 init returns {ok, {SupFlags, ChildSpecs}} — defaults + strategies
# ---------------------------------------------------------------------------


async def test_6_1_default_strategy_is_one_for_one(make_supervisor) -> None:
    """SPEC §6.1: The default restart strategy is `one_for_one`.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#Module:init/1
    """
    sup = await make_supervisor()
    assert sup.strategy == "one_for_one"


async def test_6_1_default_intensity_is_1(make_supervisor) -> None:
    """SPEC §6.1: Default intensity (max_restarts) is 1.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#Module:init/1
    """
    sup = await make_supervisor()
    assert sup.max_restarts == 1


async def test_6_1_default_period_is_5(make_supervisor) -> None:
    """SPEC §6.1: Default period (max_seconds) is 5.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#Module:init/1
    """
    sup = await make_supervisor()
    assert sup.max_seconds == 5


@pytest.mark.parametrize("strategy", ["one_for_one", "one_for_all", "rest_for_one"])
async def test_6_1_accepts_three_standard_strategies(
    make_supervisor, strategy: str
) -> None:
    """SPEC §6.1: one_for_one, one_for_all, and rest_for_one are all accepted strategies.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-strategy
    """
    sup = await make_supervisor(strategy=strategy)
    assert sup.strategy == strategy


async def test_6_1_malformed_child_spec_is_rejected() -> None:
    """SPEC §6.1: Malformed child spec raises on startup.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    with pytest.raises((TypeError, Failed)):
        await Supervisor.start(children=[("worker", GenServer, "not-a-dict")])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# §6.2 child spec shape
# ---------------------------------------------------------------------------


async def test_6_2_child_spec_requires_id(make_supervisor) -> None:
    """SPEC §6.2: id is a mandatory field in the child spec.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    sup = await make_supervisor()
    with pytest.raises(ValueError, match="id"):
        sup.child_spec("", GenServer)


async def test_6_2_child_spec_default_restart_is_permanent(make_supervisor) -> None:
    """SPEC §6.2: restart defaults to `permanent`.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    sup = await make_supervisor()
    spec = sup.child_spec("worker", GenServer)
    assert spec.restart == "permanent"


async def test_6_2_child_spec_default_type_is_worker(make_supervisor) -> None:
    """SPEC §6.2: type defaults to `worker`.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    sup = await make_supervisor()
    spec = sup.child_spec("worker", GenServer)
    assert spec.type == "worker"


async def test_6_2_child_spec_default_worker_shutdown_is_5_seconds(
    make_supervisor,
) -> None:
    """SPEC §6.2: shutdown defaults to 5000 ms (5 s) for workers.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    sup = await make_supervisor()
    spec = sup.child_spec("worker", GenServer)
    assert spec.shutdown == 5


async def test_6_2_child_spec_default_supervisor_shutdown_is_infinity(
    make_supervisor,
) -> None:
    """SPEC §6.2: shutdown defaults to `infinity` for supervisor-type children.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    sup = await make_supervisor()
    spec = sup.child_spec("child", Supervisor, type="supervisor")
    assert spec.shutdown == "infinity"


async def test_6_2_duplicate_child_ids_are_rejected(make_supervisor) -> None:
    """SPEC §6.2: Two children with the same id cannot coexist.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec
    """
    sup = await make_supervisor()
    await sup.start_child(sup.child_spec("worker", GenServer, restart="temporary"))
    with pytest.raises(Failed, match="already exists"):
        await sup.start_child(sup.child_spec("worker", GenServer))


# ---------------------------------------------------------------------------
# §6.3 restart types — 3×3 matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("restart", ["permanent", "transient", "temporary"])
@pytest.mark.parametrize("exit_kind", ["normal", "shutdown", "crash"])
async def test_6_3_restart_type_matrix(
    make_supervisor, restart: str, exit_kind: str
) -> None:
    """SPEC §6.3: Restart only when the spec demands it given the exit reason category.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-restart
    """
    sup = await make_supervisor()
    child = await sup.start_child(sup.child_spec("worker", BoomServer, restart=restart))

    if exit_kind == "crash":
        await _crash(child)
    else:
        await child.stop(exit_kind)
        await await_process_stop(child)

    should_restart = restart == "permanent" or (
        restart == "transient" and exit_kind == "crash"
    )

    if should_restart:
        restarted = await await_child_restart(sup, "worker", child)
        assert restarted is not child
    else:
        await sleep(0.05)
        assert "worker" not in sup.children


# ---------------------------------------------------------------------------
# §6.4 restart strategies
# ---------------------------------------------------------------------------


async def test_6_4_one_for_one_restarts_only_failed_child(make_supervisor) -> None:
    """SPEC §6.4: one_for_one — only the failed child is restarted.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-strategy
    """
    sup = await make_supervisor(strategy="one_for_one")
    a = await sup.start_child(sup.child_spec("a", BoomServer, restart="permanent"))
    b = await sup.start_child(sup.child_spec("b", BoomServer, restart="permanent"))

    await _crash(a)
    restarted_a = await await_child_restart(sup, "a", a)

    assert restarted_a is not a
    assert sup.children["b"].process is b


async def test_6_4_one_for_all_restarts_every_child(make_supervisor) -> None:
    """SPEC §6.4: one_for_all — all children terminate (reverse) then restart (forward).

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-strategy
    """
    sup = await make_supervisor(strategy="one_for_all")
    a = await sup.start_child(sup.child_spec("a", BoomServer, restart="permanent"))
    b = await sup.start_child(sup.child_spec("b", BoomServer, restart="permanent"))

    await _crash(a)

    new_a = await await_child_restart(sup, "a", a)
    new_b = await await_child_restart(sup, "b", b)
    assert new_a is not a
    assert new_b is not b


async def test_6_4_rest_for_one_restarts_failed_and_later_siblings(
    make_supervisor,
) -> None:
    """SPEC §6.4: rest_for_one — children started AFTER the failure restart; earlier siblings untouched.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-strategy
    """
    sup = await make_supervisor(strategy="rest_for_one")
    first = await sup.start_child(
        sup.child_spec("first", BoomServer, restart="permanent")
    )
    middle = await sup.start_child(
        sup.child_spec("middle", BoomServer, restart="permanent")
    )
    last = await sup.start_child(
        sup.child_spec("last", BoomServer, restart="permanent")
    )

    await _crash(middle)

    assert sup.children["first"].process is first
    new_middle = await await_child_restart(sup, "middle", middle)
    new_last = await await_child_restart(sup, "last", last)
    assert new_middle is not middle
    assert new_last is not last


# ---------------------------------------------------------------------------
# §6.5 restart intensity window
# ---------------------------------------------------------------------------


async def test_6_5_intensity_under_limit_keeps_supervisor_alive(
    make_supervisor,
) -> None:
    """SPEC §6.5: Restarts within max_restarts threshold keep the supervisor alive.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html §"Tuning"
    """
    sup = await make_supervisor(max_restarts=3, max_seconds=5)
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    for _ in range(3):
        await _crash(child)
        child = await await_child_restart(sup, "worker", child)

    await sleep(0.05)
    assert not sup.has_stopped()


async def test_6_5_intensity_over_limit_exits_supervisor(make_supervisor) -> None:
    """SPEC §6.5: Exceeding intensity within the window terminates the supervisor.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html §"Tuning"
    """
    sup = await make_supervisor(max_restarts=2, max_seconds=5)
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    for _ in range(2):
        await _crash(child)
        child = await await_child_restart(sup, "worker", child)
    await _crash(child)

    with fail_after(2):
        while not sup.has_stopped():
            await checkpoint()

    assert sup.has_stopped()


async def test_6_5_restart_window_slides_with_real_time(make_supervisor) -> None:
    """SPEC §6.5: The restart window is a sliding count; elapsed time evicts old entries.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html §"Tuning"
    """
    sup = await make_supervisor(max_restarts=1, max_seconds=0.05)
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    await _crash(child)
    child = await await_child_restart(sup, "worker", child)
    await sleep(0.1)
    await _crash(child)
    child = await await_child_restart(sup, "worker", child)

    assert not sup.has_stopped()


# ---------------------------------------------------------------------------
# §6.6 shutdown semantics
# ---------------------------------------------------------------------------


async def test_6_6_shutdown_timeout_calls_terminate_on_child(make_supervisor) -> None:
    """SPEC §6.6: Integer shutdown — child receives `shutdown` signal and terminate runs first.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-shutdown
    """

    class CleanStop(GenServer):
        async def init(self) -> None:
            self.terminated_with: Any = None

        async def terminate(self, reason: str | Shutdown | Exception) -> None:
            self.terminated_with = reason
            await super().terminate(reason)

    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", CleanStop, restart="temporary", shutdown=1.0)
    )

    await sup.terminate_child("worker")

    assert child.has_stopped()
    assert child.terminated_with == "normal"


async def test_6_6_shutdown_timeout_falls_back_to_kill(make_supervisor) -> None:
    """SPEC §6.6: After the shutdown timeout expires, the child is killed.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-shutdown
    """
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", SlowStopServer, restart="temporary", shutdown=0)
    )

    await sup.terminate_child("worker")

    assert child.has_stopped()


async def test_6_6_brutal_kill_terminates_child_immediately(make_supervisor) -> None:
    """SPEC §6.6: brutal_kill ends the child immediately.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-shutdown
    """
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec(
            "worker", SlowStopServer, restart="temporary", shutdown="brutal_kill"
        )
    )

    await sup.terminate_child("worker")

    assert child.has_stopped()


async def test_6_6_brutal_kill_skips_terminate_callback(make_supervisor) -> None:
    """SPEC §6.6: brutal_kill — no terminate is observed on the killed child.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-shutdown
    """

    class ObservedTerminate(GenServer):
        async def init(self) -> None:
            self.terminated = False

        async def terminate(self, reason: str | Shutdown | Exception) -> None:
            self.terminated = True
            await super().terminate(reason)

    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec(
            "worker", ObservedTerminate, restart="temporary", shutdown="brutal_kill"
        )
    )

    await sup.terminate_child("worker")
    assert not child.terminated


async def test_6_6_infinity_shutdown_waits_for_graceful_stop(make_supervisor) -> None:
    """SPEC §6.6: `infinity` waits forever for the child to acknowledge shutdown.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-shutdown
    """

    class SlowerStop(SlowStopServer):
        """Same 50ms linger, but tests we wait rather than kill."""

    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", SlowerStop, restart="temporary", shutdown="infinity")
    )

    await sup.terminate_child("worker")

    assert child.has_stopped()


# ---------------------------------------------------------------------------
# §6.7 shutdown order is reverse of start order
# ---------------------------------------------------------------------------


async def test_6_7_children_shutdown_in_reverse_start_order(make_supervisor) -> None:
    """SPEC §6.7: On supervisor stop, children terminate in reverse-start order.

    Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html §"Shutdown"
    """
    OrderLog.reset()
    sup = await make_supervisor()
    await sup.start_child(
        sup.child_spec("a", LabeledBoom, args=("a",), restart="temporary")
    )
    await sup.start_child(
        sup.child_spec("b", LabeledBoom, args=("b",), restart="temporary")
    )
    await sup.start_child(
        sup.child_spec("c", LabeledBoom, args=("c",), restart="temporary")
    )

    await sup.stop("normal")

    assert OrderLog.snapshot() == ["c", "b", "a"]
