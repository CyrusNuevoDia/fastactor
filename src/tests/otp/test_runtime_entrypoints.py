import pytest

import fastactor
from fastactor.otp import GenServer, Runtime


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _active_runtime():
    """Override the conftest autouse runtime so these tests manage Runtime themselves."""
    assert Runtime._current is None, "Runtime leaked from a previous test"
    yield
    if Runtime._current is not None:
        raise AssertionError(f"Runtime leaked after test: {Runtime._current}")


@pytest.fixture
def runtime():
    """Override conftest's `runtime` fixture — these tests opt into Runtime explicitly."""
    yield None


async def test_runtime_start_stop_basic():
    """Given no active runtime, When Runtime.start()/stop() is called, Then singleton lifecycle is clean."""
    rt = await Runtime.start()
    try:
        assert Runtime._current is rt
        assert Runtime.current() is rt
        assert rt.supervisor is not None
        assert rt.supervisor.has_started()
    finally:
        await rt.stop()

    assert Runtime._current is None


async def test_runtime_start_twice_raises():
    """Given an active Runtime, When Runtime.start() is called again, Then it raises."""
    rt = await Runtime.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await Runtime.start()
    finally:
        await rt.stop()


async def test_runtime_start_without_signal_traps():
    """Given Runtime.start(trap_signals=False), When started, Then the flag reflects and runtime runs."""
    rt = await Runtime.start(trap_signals=False)
    try:
        assert rt.trap_signals is False
        g = await GenServer.start()
        await g.stop()
    finally:
        await rt.stop()


async def test_runtime_start_with_signal_traps_default():
    """Given Runtime.start() with default args, When started, Then trap_signals is True and runtime runs."""
    rt = await Runtime.start()
    try:
        assert rt.trap_signals is True
        g = await GenServer.start()
        await g.stop()
    finally:
        await rt.stop()


async def test_runtime_stop_is_idempotent_after_first_call():
    """Given a stopped Runtime, When stop() is called again, Then it is a no-op (supervisor already gone)."""
    rt = await Runtime.start(trap_signals=False)
    await rt.stop()
    assert Runtime._current is None
    # A second stop should not raise — the state is already cleared.
    await rt.stop()
    assert Runtime._current is None


def test_fastactor_run_returns_value_and_cleans_up():
    """Given fastactor.run(main), When main returns 42, Then run() returns 42 and the runtime is released."""

    async def main():
        rt = Runtime.current()
        assert rt.supervisor is not None
        return 42

    result = fastactor.run(main, trap_signals=False)
    assert result == 42
    assert Runtime._current is None


def test_fastactor_run_propagates_exceptions():
    """Given fastactor.run(main), When main raises, Then run() re-raises and still cleans up."""

    async def main():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        fastactor.run(main, trap_signals=False)

    assert Runtime._current is None


def test_fastactor_run_can_be_called_twice():
    """Given fastactor.run(main) has completed, When it is called again, Then a fresh runtime starts."""

    async def main():
        g = await GenServer.start()
        await g.stop()
        return "ok"

    assert fastactor.run(main, trap_signals=False) == "ok"
    assert fastactor.run(main, trap_signals=False) == "ok"
    assert Runtime._current is None


def test_fastactor_run_swallows_cancellation_when_trap_signals_on():
    """Given main raises CancelledError, When fastactor.run(trap_signals=True), Then it returns None (clean shutdown)."""
    import asyncio

    async def main():
        raise asyncio.CancelledError()

    result = fastactor.run(main, trap_signals=True)
    assert result is None
    assert Runtime._current is None


def test_fastactor_run_reraises_cancellation_when_trap_signals_off():
    """Given main raises CancelledError, When trap_signals=False, Then the error propagates."""
    import asyncio

    async def main():
        raise asyncio.CancelledError()

    with pytest.raises(BaseException) as excinfo:
        fastactor.run(main, trap_signals=False)

    error = excinfo.value
    assert isinstance(error, asyncio.CancelledError) or (
        isinstance(error, BaseExceptionGroup)
        and any(isinstance(e, asyncio.CancelledError) for e in error.exceptions)
    )
