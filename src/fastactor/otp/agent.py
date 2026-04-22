"""Agent — a `GenServer` wrapper for "I just need to hold some state".

Factory returns the initial state. Interact via `get` / `update` / `get_and_update` /
`cast_update`. See `src/fastactor/otp/README.md#agent`.
"""

import typing as t
from dataclasses import dataclass

from fastactor.settings import settings
from fastactor.utils import maybe_await

from ._messages import Call, Cast
from .gen_server import GenServer


@dataclass(slots=True)
class _Get:
    fn: t.Any


@dataclass(slots=True)
class _Update:
    fn: t.Any


@dataclass(slots=True)
class _GetAndUpdate:
    fn: t.Any


@dataclass(slots=True)
class _CastUpdate:
    fn: t.Any


class Agent[S](GenServer):
    state: S

    async def init(self, factory: t.Callable[[], S | t.Awaitable[S]]):
        self.state = await maybe_await(factory())

    async def handle_call(self, call: Call):
        match call.message:
            case _Get(fn):
                return await maybe_await(fn(self.state))
            case _Update(fn):
                self.state = await maybe_await(fn(self.state))
                return True
            case _GetAndUpdate(fn):
                reply, self.state = await maybe_await(fn(self.state))
                return reply

        return None

    async def handle_cast(self, cast: Cast):
        match cast.message:
            case _CastUpdate(fn):
                self.state = await maybe_await(fn(self.state))

    async def get[R](
        self,
        fn: t.Callable[[S], R | t.Awaitable[R]],
        timeout=settings.call_timeout,
    ) -> R:
        return await self.call(_Get(fn), timeout=timeout)

    async def update(
        self,
        fn: t.Callable[[S], S | t.Awaitable[S]],
        timeout=settings.call_timeout,
    ) -> None:
        await self.call(_Update(fn), timeout=timeout)

    async def get_and_update[R](
        self,
        fn: t.Callable[[S], tuple[R, S] | t.Awaitable[tuple[R, S]]],
        timeout=settings.call_timeout,
    ) -> R:
        return await self.call(_GetAndUpdate(fn), timeout=timeout)

    def cast_update(self, fn: t.Callable[[S], S | t.Awaitable[S]]) -> None:
        self.cast(_CastUpdate(fn))

    @classmethod
    async def start[T](  # type: ignore[override]
        cls,
        factory: t.Callable[[], T | t.Awaitable[T]],
        **kwargs,
    ) -> "Agent[T]":
        return t.cast("Agent[T]", await cls._spawn(factory, link=False, **kwargs))

    @classmethod
    async def start_link[T](  # type: ignore[override]
        cls,
        factory: t.Callable[[], T | t.Awaitable[T]],
        **kwargs,
    ) -> "Agent[T]":
        return t.cast("Agent[T]", await cls._spawn(factory, link=True, **kwargs))
