import pytest
from anyio import Event, fail_after
from anyio.lowlevel import checkpoint

from fastactor.otp import Task, TaskSupervisor

pytestmark = pytest.mark.anyio


async def return_value():
    return 42


async def raise_error():
    raise RuntimeError("task boom")


async def checkpoint_then_return():
    await checkpoint()
    return "done"


async def wait_forever(event: Event):
    await event.wait()
    return "released"


async def test_task_start_returns_awaitable_result():
    """G: a started Task. W: we await it. T: the task result is returned."""
    task = await Task.start(return_value)

    assert await task == 42


async def test_task_await_reraises_task_exception():
    """G: a crashing Task. W: we await it. T: the original exception is reraised."""
    task = await Task.start(raise_error)

    with pytest.raises(RuntimeError, match="task boom"):
        await task


async def test_task_start_is_unlinked_so_runtime_survives_crash(runtime):
    """G: an unlinked Task that crashes. W: we await it. T: the runtime supervisor keeps running."""
    task = await Task.start(raise_error)

    with pytest.raises(RuntimeError, match="task boom"):
        await task

    assert runtime.supervisor is not None
    assert not runtime.supervisor.has_stopped()


async def test_task_can_be_awaited_with_fail_after_timeout():
    """G: a waiting Task. W: fail_after expires first. T: the await times out without extra sleeps."""
    gate = Event()
    task = await Task.start(lambda: wait_forever(gate))

    with pytest.raises(TimeoutError):
        with fail_after(0):
            await task

    gate.set()
    assert await task == "released"


async def test_task_supervisor_run_returns_awaitable_task():
    """G: a TaskSupervisor. W: it runs a task. T: the returned task is awaitable and yields the result."""
    sup = await TaskSupervisor.start()
    task = await sup.run(checkpoint_then_return)

    assert await task == "done"

    await sup.stop("normal")


async def test_task_yield_tri_state():
    """G: Task.yield_. W: we poll before completion, after completion, and on crash. T: it returns None, the result, and the exception object."""
    gate = Event()
    slow = await Task.start(lambda: wait_forever(gate))

    assert await slow.yield_(timeout=0) is None

    gate.set()
    assert await slow.yield_(timeout=1) == "released"

    crasher = await Task.start(raise_error)
    result = await crasher.yield_(timeout=1)

    assert isinstance(result, RuntimeError)
    assert "task boom" in str(result)


async def test_task_shutdown_cancels_running_task():
    """G: a Task sleeping forever. W: shutdown() is called. T: the task stops promptly."""
    gate = Event()
    task = await Task.start(lambda: wait_forever(gate))

    await task.shutdown(timeout=1)

    assert task.has_stopped()
