import typing as t

from ._messages import Call, Cast
from .gen_server import GenServer

_GET = object()
_UPDATE = object()
_GET_AND_UPDATE = object()
_CAST_UPDATE = object()


async def _maybe_await[T](x: T | t.Awaitable[T]) -> T:
    import inspect

    if inspect.isawaitable(x):
        return t.cast(T, await x)
    return t.cast(T, x)


class Agent[S](GenServer):
    state: S

    async def init(self, factory: t.Callable[[], S | t.Awaitable[S]]):
        self.state = await _maybe_await(factory())

    async def handle_call(self, call: Call):
        tag, fn = call.message

        match tag:
            case _ if tag is _GET:
                return await _maybe_await(fn(self.state))
            case _ if tag is _UPDATE:
                self.state = await _maybe_await(fn(self.state))
                return True
            case _ if tag is _GET_AND_UPDATE:
                reply, self.state = await _maybe_await(fn(self.state))
                return reply

        return None

    async def handle_cast(self, cast: Cast):
        tag, fn = cast.message

        if tag is _CAST_UPDATE:
            self.state = await _maybe_await(fn(self.state))

    async def get[R](
        self,
        fn: t.Callable[[S], R | t.Awaitable[R]],
        timeout=5,
    ) -> R:
        return await self.call((_GET, fn), timeout=timeout)

    async def update(
        self,
        fn: t.Callable[[S], S | t.Awaitable[S]],
        timeout=5,
    ) -> None:
        await self.call((_UPDATE, fn), timeout=timeout)

    async def get_and_update[R](
        self,
        fn: t.Callable[[S], tuple[R, S] | t.Awaitable[tuple[R, S]]],
        timeout=5,
    ) -> R:
        return await self.call((_GET_AND_UPDATE, fn), timeout=timeout)

    def cast_update(self, fn: t.Callable[[S], S | t.Awaitable[S]]) -> None:
        self.cast((_CAST_UPDATE, fn))
