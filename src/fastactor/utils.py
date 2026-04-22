import inspect
import typing as t
from collections import deque

from ksuid import Ksuid
from pyee import EventEmitter

from fastactor.settings import settings


def id_generator(prefix: str = "") -> t.Callable[[], str]:
    def gen_id():
        return f"{prefix}:{Ksuid()}"

    return gen_id


def deque_factory(maxlen: int = settings.mailbox_size):
    def factory():
        return deque(maxlen=maxlen)

    return factory


async def maybe_await[T](x: T | t.Awaitable[T]) -> T:
    if inspect.isawaitable(x):
        return t.cast(T, await x)
    return t.cast(T, x)


async def emit_awaited(
    emitter: EventEmitter, event: str, *args: t.Any, **kwargs: t.Any
) -> None:
    listeners = emitter.listeners(event)
    if not listeners:
        return
    for listener in listeners:
        result = listener(*args, **kwargs)
        if inspect.isawaitable(result):
            await result
