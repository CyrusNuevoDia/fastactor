"""GenEvent — event manager hosting multiple handlers with their own state.

Handlers are `EventHandler` instances installed via `add_handler`. `notify` /
`sync_notify` dispatch an event to every installed handler in install order.
A handler that raises from a callback is removed from the manager; the
manager process itself stays alive (SPEC §7.3).
"""

import logging
import typing as t
from dataclasses import dataclass

from fastactor.settings import settings

from ._messages import Call, Cast
from .gen_server import GenServer

logger = logging.getLogger(__name__)


class HandlerNotInstalled(Exception):
    """Raised when an operation references a handler not currently installed."""


class RemoveHandler:
    """Return from a handler callback to have the handler removed."""


@dataclass
class SwapHandler:
    """Return from a handler callback to swap it for another handler.

    The old handler's `terminate(args1)` is called; its return value is paired
    with `args2` and passed to `handler2.init((args2, terminate_result))`.
    """

    args1: t.Any
    handler2: "EventHandler"
    args2: t.Any


class EventHandler:
    """Base class for `GenEvent` handlers. Subclass and override callbacks.

    Handler state lives on `self` — set attributes in `init`. Callbacks may
    return `None` (keep), `RemoveHandler()` (drop), or a `SwapHandler(...)` to
    atomically replace this handler with another.
    """

    async def init(self, *args: t.Any, **kwargs: t.Any) -> None:
        return None

    async def handle_event(self, event: t.Any) -> None | RemoveHandler | SwapHandler:
        return None

    async def handle_call(
        self, request: t.Any
    ) -> t.Any | tuple[t.Any, RemoveHandler | SwapHandler]:
        return None

    async def terminate(self, arg: t.Any) -> t.Any:
        return None


@dataclass(slots=True)
class _Add:
    handler: "EventHandler"
    args: tuple[t.Any, ...]
    kwargs: dict[str, t.Any]


@dataclass(slots=True)
class _Delete:
    handler: "EventHandler"
    arg: t.Any


@dataclass(slots=True)
class _Swap:
    old: tuple["EventHandler", t.Any]
    new: tuple["EventHandler", t.Any]


@dataclass(slots=True)
class _Notify:
    event: t.Any


@dataclass(slots=True)
class _SyncNotify:
    event: t.Any


@dataclass(slots=True)
class _CallHandler:
    handler: "EventHandler"
    request: t.Any


@dataclass(slots=True)
class _WhichHandlers:
    pass


class GenEvent(GenServer):
    """OTP gen_event — hosts multiple `EventHandler`s, each with its own state."""

    _handlers: list[EventHandler]

    async def init(self) -> None:
        self._handlers = []

    async def add_handler(
        self, handler: EventHandler, *args: t.Any, **kwargs: t.Any
    ) -> None:
        await self.call(_Add(handler, args, kwargs))

    async def delete_handler(self, handler: EventHandler, arg: t.Any = None) -> t.Any:
        return await self.call(_Delete(handler, arg))

    async def swap_handler(
        self,
        old: tuple[EventHandler, t.Any],
        new: tuple[EventHandler, t.Any],
    ) -> None:
        await self.call(_Swap(old, new))

    async def which_handlers(self) -> list[EventHandler]:
        return await self.call(_WhichHandlers())

    def notify(self, event: t.Any) -> None:
        self.cast(_Notify(event))

    async def sync_notify(
        self, event: t.Any, timeout: float = settings.call_timeout
    ) -> None:
        await self.call(_SyncNotify(event), timeout=timeout)

    async def call_handler(
        self,
        handler: EventHandler,
        request: t.Any,
        timeout: float = settings.call_timeout,
    ) -> t.Any:
        return await self.call(_CallHandler(handler, request), timeout=timeout)

    async def handle_call(self, call: Call) -> None:
        # Always reply via `call.set_result` so `None` results don't get
        # interpreted as a deferred reply by GenServer.
        match call.message:
            case _WhichHandlers():
                call.set_result(list(self._handlers))
            case _Add(handler, args, kwargs):
                call.set_result(await self._do_add_handler(handler, args, kwargs))
            case _Delete(handler, arg):
                call.set_result(await self._do_delete_handler(handler, arg))
            case _Swap(old, new):
                call.set_result(await self._do_swap_handler(old, new))
            case _SyncNotify(event):
                await self._dispatch_event(event)
                call.set_result(None)
            case _CallHandler(handler, request):
                call.set_result(await self._do_call_handler(handler, request))
            case _:
                call.set_result(None)
        return None

    async def handle_cast(self, cast: Cast) -> None:
        match cast.message:
            case _Notify(event):
                await self._dispatch_event(event)

    async def on_terminate(self, reason: t.Any) -> None:
        for handler in list(self._handlers):
            try:
                await handler.terminate("stop")
            except Exception as error:
                logger.error(
                    "%s terminate for %r raised %r during shutdown",
                    self,
                    handler,
                    error,
                )
        self._handlers.clear()

    async def _do_add_handler(
        self,
        handler: EventHandler,
        args: tuple[t.Any, ...],
        kwargs: dict[str, t.Any],
    ) -> t.Any:
        if handler in self._handlers:
            return HandlerNotInstalled(f"{handler!r} already installed")
        try:
            await handler.init(*args, **kwargs)
        except Exception as error:
            logger.debug("%s add_handler init failed: %r", self, error)
            return error
        self._handlers.append(handler)
        return None

    async def _do_delete_handler(self, handler: EventHandler, arg: t.Any) -> t.Any:
        if handler not in self._handlers:
            return HandlerNotInstalled(f"{handler!r} is not installed")
        self._handlers.remove(handler)
        try:
            return await handler.terminate(arg)
        except Exception as error:
            logger.error(
                "%s terminate for %r raised %r during delete_handler",
                self,
                handler,
                error,
            )
            return error

    async def _do_swap_handler(
        self,
        old: tuple[EventHandler, t.Any],
        new: tuple[EventHandler, t.Any],
    ) -> t.Any:
        old_handler, args1 = old
        new_handler, args2 = new

        if old_handler in self._handlers:
            self._handlers.remove(old_handler)
            try:
                terminate_result = await old_handler.terminate(args1)
            except Exception as error:
                logger.error(
                    "%s terminate for %r raised %r during swap_handler",
                    self,
                    old_handler,
                    error,
                )
                terminate_result = error
        else:
            terminate_result = "error"

        try:
            await new_handler.init((args2, terminate_result))
        except Exception as error:
            logger.debug("%s swap_handler init failed: %r", self, error)
            return error
        self._handlers.append(new_handler)
        return None

    async def _do_call_handler(self, handler: EventHandler, request: t.Any) -> t.Any:
        if handler not in self._handlers:
            return HandlerNotInstalled(f"{handler!r} is not installed")
        try:
            result = await handler.handle_call(request)
        except Exception as error:
            logger.warning(
                "%s handle_call for %r raised %r; removing handler",
                self,
                handler,
                error,
            )
            await self._remove_after_crash(handler, error)
            return error

        match result:
            case (reply, RemoveHandler()):
                self._handlers.remove(handler)
                try:
                    await handler.terminate("remove_handler")
                except Exception as error:
                    logger.error(
                        "%s terminate for %r raised %r during remove",
                        self,
                        handler,
                        error,
                    )
                return reply
            case (reply, SwapHandler() as swap):
                await self._perform_swap(handler, swap)
                return reply
            case reply:
                return reply

    async def _dispatch_event(self, event: t.Any) -> None:
        for handler in list(self._handlers):
            if handler not in self._handlers:
                continue
            try:
                result = await handler.handle_event(event)
            except Exception as error:
                logger.warning(
                    "%s handle_event for %r raised %r; removing handler",
                    self,
                    handler,
                    error,
                )
                await self._remove_after_crash(handler, error)
                continue
            match result:
                case RemoveHandler():
                    if handler in self._handlers:
                        self._handlers.remove(handler)
                    try:
                        await handler.terminate("remove_handler")
                    except Exception as error:
                        logger.error(
                            "%s terminate for %r raised %r during remove",
                            self,
                            handler,
                            error,
                        )
                case SwapHandler() as swap:
                    await self._perform_swap(handler, swap)
                case None:
                    pass
                case _:
                    pass

    async def _remove_after_crash(
        self, handler: EventHandler, error: Exception
    ) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)
        try:
            await handler.terminate(("error", error))
        except Exception as terminate_error:
            logger.error(
                "%s terminate for %r raised %r after crash",
                self,
                handler,
                terminate_error,
            )

    async def _perform_swap(self, old_handler: EventHandler, swap: SwapHandler) -> None:
        if old_handler in self._handlers:
            self._handlers.remove(old_handler)
        try:
            terminate_result = await old_handler.terminate(swap.args1)
        except Exception as error:
            logger.error(
                "%s terminate for %r raised %r during swap",
                self,
                old_handler,
                error,
            )
            terminate_result = error
        try:
            await swap.handler2.init((swap.args2, terminate_result))
        except Exception as error:
            logger.debug("%s swap init for new handler failed: %r", self, error)
            return
        self._handlers.append(swap.handler2)
