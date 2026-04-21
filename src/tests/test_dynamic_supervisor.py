import pytest
from support import BoomServer, CounterServer, await_child_restart

from fastactor.otp import DynamicSupervisor, Failed

pytestmark = pytest.mark.anyio


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
