import pytest
from anyio import fail_after
from anyio.lowlevel import checkpoint

from .helpers import BoomServer, CounterServer, await_child_restart

from fastactor.otp import DynamicSupervisor, Failed

pytestmark = pytest.mark.anyio


async def await_child_spec_cleanup(sup, child_id: str, timeout: float = 2) -> None:
    with fail_after(timeout):
        while child_id in sup.child_specs:
            await checkpoint()


async def test_dynamic_supervisor_starts_children_with_temporary_default():
    """G: a DynamicSupervisor. W: a default child crashes. T: it is not restarted."""
    sup = await DynamicSupervisor.start()
    child = await sup.start_child(DynamicSupervisor.child_spec("worker", BoomServer))

    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await child.stopped()

    assert "worker" not in sup.children

    await sup.stop("normal")


async def test_dynamic_supervisor_rejects_non_one_for_one_strategy():
    """G: a DynamicSupervisor strategy request. W: it is not one_for_one. T: startup is rejected."""
    with pytest.raises(ValueError, match="one_for_one"):
        await DynamicSupervisor.start(strategy="one_for_all")


async def test_dynamic_supervisor_enforces_max_children():
    """G: a max_children limit. W: we exceed it. T: the extra child is rejected."""
    sup = await DynamicSupervisor.start(max_children=1)
    await sup.start_child(DynamicSupervisor.child_spec("first", CounterServer))

    with pytest.raises((Failed, ValueError), match="max"):
        await sup.start_child(DynamicSupervisor.child_spec("second", CounterServer))

    await sup.stop("normal")


async def test_dynamic_supervisor_prepends_extra_arguments():
    """G: extra_arguments are configured. W: a child starts. T: the extra arguments reach init first."""
    sup = await DynamicSupervisor.start(extra_arguments=(10,))
    child = await sup.start_child(
        DynamicSupervisor.child_spec("counter", CounterServer)
    )

    assert await child.call("get") == 10

    await sup.stop("normal")


async def test_dynamic_supervisor_restart_child_after_crash():
    """G: a permanent dynamic child. W: it crashes. T: it is restarted."""
    sup = await DynamicSupervisor.start()
    child = await sup.start_child(
        DynamicSupervisor.child_spec("worker", BoomServer, restart="permanent")
    )

    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await child.stopped()

    assert await await_child_restart(sup, "worker", child) is not child

    await sup.stop("normal")


async def test_dynamic_supervisor_accepts_tuple_child_spec():
    """G: a DynamicSupervisor. W: start_child receives a (cls, kwargs) tuple. T: the child starts with those kwargs."""
    sup = await DynamicSupervisor.start()

    child = await sup.start_child((CounterServer, {"count": 7}))

    assert await child.call("get") == 7

    await sup.stop("normal")


async def test_temporary_child_spec_is_dropped_after_normal_exit():
    """G: a temporary dynamic child. W: it exits normally. T: the id can be re-used."""
    sup = await DynamicSupervisor.start()
    child = await sup.start_child(DynamicSupervisor.child_spec("x", CounterServer))

    await child.stop("normal")
    await child.stopped()
    await await_child_spec_cleanup(sup, "x")

    assert "x" not in sup.children
    assert "x" not in sup.child_specs

    restarted = await sup.start_child(DynamicSupervisor.child_spec("x", CounterServer))

    assert restarted is not child
    assert sup.children["x"].process is restarted

    await sup.stop("normal")


async def test_temporary_child_spec_is_dropped_after_crash():
    """G: a temporary dynamic child. W: it crashes. T: the id can be re-used."""
    sup = await DynamicSupervisor.start()
    child = await sup.start_child(DynamicSupervisor.child_spec("x", BoomServer))

    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await child.stopped()
    await await_child_spec_cleanup(sup, "x")

    assert "x" not in sup.children
    assert "x" not in sup.child_specs

    restarted = await sup.start_child(DynamicSupervisor.child_spec("x", BoomServer))

    assert restarted is not child
    assert sup.children["x"].process is restarted

    await sup.stop("normal")


async def test_transient_child_normal_exit_drops_spec():
    """G: a transient dynamic child. W: it exits normally. T: the id can be re-used."""
    sup = await DynamicSupervisor.start()
    child = await sup.start_child(
        DynamicSupervisor.child_spec("x", CounterServer, restart="transient")
    )

    await child.stop("normal")
    await child.stopped()
    await await_child_spec_cleanup(sup, "x")

    assert "x" not in sup.children
    assert "x" not in sup.child_specs

    restarted = await sup.start_child(
        DynamicSupervisor.child_spec("x", CounterServer, restart="transient")
    )

    assert restarted is not child
    assert sup.children["x"].process is restarted

    await sup.stop("normal")
