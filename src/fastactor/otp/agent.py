"""Agent — a `GenServer` wrapper for "I just need to hold some state".

Factory returns the initial state. Interact via `get` / `update` / `get_and_update` /
`cast_update`. See `src/fastactor/otp/README.md#agent`.
"""

import typing as t

from fastactor.settings import settings
from fastactor.utils import maybe_await

from ._messages import Call, Cast
from .gen_server import GenServer

_GET = object()
_UPDATE = object()
_GET_AND_UPDATE = object()
_CAST_UPDATE = object()


class Agent[S](GenServer):
    state: S

    async def init(self, factory: t.Callable[[], S | t.Awaitable[S]]):
        self.state = await maybe_await(factory())

    async def handle_call(self, call: Call):
        tag, fn = call.message

        match tag:
            case _ if tag is _GET:
                return await maybe_await(fn(self.state))
            case _ if tag is _UPDATE:
                self.state = await maybe_await(fn(self.state))
                return True
            case _ if tag is _GET_AND_UPDATE:
                reply, self.state = await maybe_await(fn(self.state))
                return reply

        return None

    async def handle_cast(self, cast: Cast):
        tag, fn = cast.message

        if tag is _CAST_UPDATE:
            self.state = await maybe_await(fn(self.state))

    async def get[R](
        self,
        fn: t.Callable[[S], R | t.Awaitable[R]],
        timeout=settings.call_timeout,
    ) -> R:
        return await self.call((_GET, fn), timeout=timeout)

    async def update(
        self,
        fn: t.Callable[[S], S | t.Awaitable[S]],
        timeout=settings.call_timeout,
    ) -> None:
        await self.call((_UPDATE, fn), timeout=timeout)

    async def get_and_update[R](
        self,
        fn: t.Callable[[S], tuple[R, S] | t.Awaitable[tuple[R, S]]],
        timeout=settings.call_timeout,
    ) -> R:
        return await self.call((_GET_AND_UPDATE, fn), timeout=timeout)

    def cast_update(self, fn: t.Callable[[S], S | t.Awaitable[S]]) -> None:
        self.cast((_CAST_UPDATE, fn))
