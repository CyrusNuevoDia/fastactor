import asyncio
import typing as t

import anyio

from . import telemetry
from .otp import Runtime
from .settings import settings


def _is_clean_cancellation(error: BaseException) -> bool:
    if isinstance(error, asyncio.CancelledError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return all(_is_clean_cancellation(sub) for sub in error.exceptions)
    return False


def run(
    main_fn: t.Callable[[], t.Awaitable[t.Any]],
    *,
    trap_signals: bool = True,
    backend: str = "asyncio",
) -> t.Any:
    async def wrapper():
        async with Runtime(trap_signals=trap_signals):
            return await main_fn()

    try:
        return anyio.run(wrapper, backend=backend)
    except BaseException as error:
        if trap_signals and _is_clean_cancellation(error):
            return None
        raise


__all__ = [
    "run",
    "settings",
    "telemetry",
]
