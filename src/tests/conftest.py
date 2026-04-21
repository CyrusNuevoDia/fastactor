from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import count
from pathlib import Path
import sys

import pytest
from anyio import move_on_after

from fastactor.otp import Runtime, Supervisor


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture(autouse=True)
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def runtime() -> AsyncIterator[Runtime]:
    async with Runtime() as rt:
        yield rt


@pytest.fixture(autouse=True)
async def _active_runtime(runtime: Runtime) -> AsyncIterator[None]:
    yield


@pytest.fixture
def supervisor(runtime: Runtime) -> Supervisor:
    assert runtime.supervisor is not None
    return runtime.supervisor


@pytest.fixture
async def make_supervisor(
    runtime: Runtime,
) -> AsyncIterator[Callable[..., Awaitable[Supervisor]]]:
    created: list[Supervisor] = []
    ids = count(1)

    async def factory(
        *,
        strategy: str = "one_for_one",
        max_restarts: int = 3,
        max_seconds: float = 5.0,
    ) -> Supervisor:
        _ = f"supervisor_{next(ids)}"
        sup = await Supervisor.start(
            strategy=strategy,
            max_restarts=max_restarts,
            max_seconds=max_seconds,
            supervisor=runtime.supervisor,
        )
        created.append(sup)
        return sup

    yield factory

    for sup in reversed(created):
        if sup.has_stopped():
            continue
        with move_on_after(2):
            await sup.stop("normal")
