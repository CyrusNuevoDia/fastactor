import pytest
from anyio import fail_after
from anyio.lowlevel import checkpoint

from support import BoomServer, await_child_restart


pytestmark = pytest.mark.anyio


async def await_process_stop(process, timeout: float = 2):
    with fail_after(timeout):
        while not process.has_stopped():
            await checkpoint()
        await process.stopped()
    return process


async def _start_boom_child(supervisor, child_id: str, restart: str = "permanent"):
    return await supervisor.start_child(
        supervisor.child_spec(child_id, BoomServer, restart=restart)
    )


async def _crash_child(child) -> None:
    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await await_process_stop(child)


async def test_one_for_one_restarts_permanent_child_after_crash(make_supervisor):
    """G: a one_for_one supervisor with a permanent child. W: the child crashes. T: it restarts."""
    sup = await make_supervisor(strategy="one_for_one")
    child = await _start_boom_child(sup, "worker", restart="permanent")

    await _crash_child(child)
    restarted = await await_child_restart(sup, "worker", child)

    assert restarted is not child


async def test_one_for_one_keeps_siblings_running_after_crash(make_supervisor):
    """G: one_for_one siblings are running. W: one child crashes. T: the sibling keeps its identity."""
    sup = await make_supervisor(strategy="one_for_one")
    left = await _start_boom_child(sup, "left", restart="permanent")
    right = await _start_boom_child(sup, "right", restart="permanent")

    await _crash_child(left)
    restarted = await await_child_restart(sup, "left", left)

    assert restarted is not left
    assert sup.children["right"].process is right


async def test_one_for_one_transient_child_not_restarted_after_normal_exit(
    make_supervisor,
):
    """G: a transient one_for_one child. W: it exits normally. T: it is removed without restart."""
    sup = await make_supervisor(strategy="one_for_one")
    child = await _start_boom_child(sup, "worker", restart="transient")

    await child.stop("normal")
    await await_process_stop(child)
    for _ in range(5):
        await checkpoint()

    assert "worker" not in sup.children


async def test_one_for_one_temporary_child_not_restarted_after_crash(make_supervisor):
    """G: a temporary one_for_one child. W: it crashes. T: it is removed without restart."""
    sup = await make_supervisor(strategy="one_for_one")
    child = await _start_boom_child(sup, "worker", restart="temporary")

    await _crash_child(child)
    for _ in range(5):
        await checkpoint()

    assert "worker" not in sup.children


async def test_one_for_one_transient_child_restarts_after_crash(make_supervisor):
    """G: a transient one_for_one child. W: it crashes. T: it restarts."""
    sup = await make_supervisor(strategy="one_for_one")
    child = await _start_boom_child(sup, "worker", restart="transient")

    await _crash_child(child)

    assert await await_child_restart(sup, "worker", child) is not child


async def test_one_for_all_restarts_all_children_after_crash(make_supervisor):
    """G: a one_for_all supervisor. W: one child crashes. T: all children restart together."""
    sup = await make_supervisor(strategy="one_for_all")
    first = await _start_boom_child(sup, "first", restart="permanent")
    second = await _start_boom_child(sup, "second", restart="permanent")

    await _crash_child(first)

    assert await await_child_restart(sup, "first", first) is not first
    assert await await_child_restart(sup, "second", second) is not second


async def test_one_for_all_transient_normal_exit_stops_group_without_restart(
    make_supervisor,
):
    """G: one_for_all transient children are running. W: one exits normally. T: the group stops without restart."""
    sup = await make_supervisor(strategy="one_for_all")
    first = await _start_boom_child(sup, "first", restart="transient")
    second = await _start_boom_child(sup, "second", restart="transient")

    await first.stop("normal")
    await await_process_stop(first)
    await await_process_stop(second)
    for _ in range(5):
        await checkpoint()

    assert sup.children == {}


async def test_one_for_all_temporary_crash_removes_all_children(make_supervisor):
    """G: one_for_all temporary children are running. W: one crashes. T: the group stops without restart."""
    sup = await make_supervisor(strategy="one_for_all")
    first = await _start_boom_child(sup, "first", restart="temporary")
    second = await _start_boom_child(sup, "second", restart="temporary")

    await _crash_child(first)
    await await_process_stop(second)
    for _ in range(5):
        await checkpoint()

    assert sup.children == {}


async def test_rest_for_one_restarts_failed_child_and_later_siblings(make_supervisor):
    """G: a rest_for_one supervisor. W: the middle child crashes. T: it and later siblings restart."""
    sup = await make_supervisor(strategy="rest_for_one")
    first = await _start_boom_child(sup, "first", restart="permanent")
    middle = await _start_boom_child(sup, "middle", restart="permanent")
    last = await _start_boom_child(sup, "last", restart="permanent")

    await _crash_child(middle)

    assert sup.children["first"].process is first
    assert await await_child_restart(sup, "middle", middle) is not middle
    assert await await_child_restart(sup, "last", last) is not last


async def test_rest_for_one_keeps_earlier_siblings_running(make_supervisor):
    """G: a rest_for_one supervisor. W: a later child crashes. T: earlier siblings keep running."""
    sup = await make_supervisor(strategy="rest_for_one")
    first = await _start_boom_child(sup, "first", restart="permanent")
    middle = await _start_boom_child(sup, "middle", restart="permanent")

    await _crash_child(middle)
    await await_child_restart(sup, "middle", middle)

    assert sup.children["first"].process is first


async def test_rest_for_one_restarts_tail_in_spec_order(make_supervisor):
    """G: a rest_for_one supervisor. W: the first child crashes. T: the whole tail is replaced in place."""
    sup = await make_supervisor(strategy="rest_for_one")
    first = await _start_boom_child(sup, "first", restart="permanent")
    second = await _start_boom_child(sup, "second", restart="permanent")

    await _crash_child(first)

    assert await await_child_restart(sup, "first", first) is not first
    assert await await_child_restart(sup, "second", second) is not second


@pytest.mark.parametrize("strategy", ["one_for_one", "one_for_all", "rest_for_one"])
@pytest.mark.parametrize("restart", ["permanent", "transient", "temporary"])
@pytest.mark.parametrize("exit_type", ["normal", "shutdown", "crash"])
async def test_restart_on_exit_matrix(
    make_supervisor,
    strategy: str,
    restart: str,
    exit_type: str,
):
    """G: a strategy/restart/exit combination. W: the child exits. T: restart policy matches OTP expectations."""
    sup = await make_supervisor(strategy=strategy)
    child = await _start_boom_child(sup, "worker", restart=restart)

    if exit_type == "crash":
        await _crash_child(child)
    else:
        await child.stop(exit_type)
        await await_process_stop(child)

    should_restart = restart == "permanent" or (
        restart == "transient" and exit_type == "crash"
    )

    if should_restart:
        restarted = await await_child_restart(sup, "worker", child)
        assert restarted is not child
    else:
        for _ in range(5):
            await checkpoint()
        assert "worker" not in sup.children
