"""GenServer — Process + synchronous `call` + fire-and-forget `cast`.

See `src/fastactor/otp/README.md#genserver` for the `handle_call` return contract
(value / None-means-NoReply / (value, Continue) / (value, Stop)), deferred-reply
pattern, and exception propagation rules.
"""

import logging
import typing as t

from anyio import BrokenResourceError, ClosedResourceError, create_memory_object_stream, move_on_after

from fastactor import telemetry
from fastactor.settings import settings

from ._exceptions import Crashed, Failed, Shutdown
from ._messages import Call, Cast, Continue, Ignore, Info, Stop
from .process import Process

logger = logging.getLogger(__name__)


def reply(call: "Call[t.Any, t.Any]", value: t.Any) -> None:
    """Erlang-style gen_server:reply/2 — unblock a deferred caller."""
    call.set_result(value)


class GenServer[Req = t.Any, Rep = t.Any](Process):
    _idle_timeout: float | None = None

    async def _init(self, *args, **kwargs) -> None:
        logger.debug("%s init", self)
        try:
            init_result = await self.init(*args, **kwargs)
        except Exception as error:
            raise Crashed(str(error)) from error

        match init_result:
            case Ignore():
                self._ignored = True
                self._started.set()
                self._stopped.set()
            case Stop(reason=reason):
                self._started.set()
                self._stopped.set()
                raise Shutdown(reason)
            case Continue() as continuation:
                self._set_pending_continue(continuation)
                self._inbox, self._mailbox = create_memory_object_stream(
                    settings.mailbox_size
                )
            case None:
                self._inbox, self._mailbox = create_memory_object_stream(
                    settings.mailbox_size
                )
            case (_, int() | float() as timeout_ms):
                # {ok, State, Timeout} — arm idle timer after init
                self._idle_timeout = timeout_ms / 1000.0
                self._inbox, self._mailbox = create_memory_object_stream(
                    settings.mailbox_size
                )
            case _:
                raise Crashed(
                    f"unsupported init return shape for {type(self).__name__}: "
                    f"{init_result!r}"
                )

    async def handle_call(
        self, call: Call[Req, Rep]
    ) -> Rep | tuple[Rep, Continue] | tuple[Rep, Stop] | None:
        return None

    async def handle_cast(self, cast: Cast[Req]) -> Continue | None:
        return None

    async def handle_info(self, message: Info) -> Continue | None:
        return None

    async def _receive_next(self) -> t.Any:
        assert self._mailbox is not None
        if self._idle_timeout is None:
            return await self._mailbox.receive()

        timeout = self._idle_timeout
        self._idle_timeout = None
        with move_on_after(timeout) as scope:
            return await self._mailbox.receive()
        if scope.cancelled_caught:
            return Info(self.supervisor, "timeout")
        # Should not be reached; satisfy type checker
        return await self._mailbox.receive()  # pragma: no cover

    async def _run_in_process_context[R](
        self,
        func: t.Callable[..., t.Awaitable[R]],
        *args,
        **kwargs,
    ) -> R:
        handle_continue_func = getattr(self.handle_continue, "__func__", None)
        is_handle_continue = (
            getattr(func, "__self__", None) is self
            and getattr(func, "__func__", None) is handle_continue_func
        )

        result = await super()._run_in_process_context(func, *args, **kwargs)
        if is_handle_continue:
            self._set_pending_continue(result)
        return result

    def _telemetry_attrs(self, *, timeout: float | None = None) -> dict[str, t.Any]:
        attrs: dict[str, t.Any] = {
            telemetry.ATTR_TARGET_ID: self.id,
            telemetry.ATTR_TARGET_CLASS: type(self).__name__,
            telemetry.ATTR_RPC_SYSTEM: telemetry.RPC_SYSTEM_VALUE,
        }
        if timeout is not None:
            attrs[telemetry.ATTR_CALL_TIMEOUT] = timeout
        return attrs

    async def call(
        self,
        request: Req,
        sender: Process | None = None,
        timeout: float = settings.call_timeout,
        *,
        metadata: dict[str, t.Any] | None = None,
    ) -> Rep:
        from .runtime import _current_process

        if self.has_stopped():
            raise Failed("noproc")

        sender = sender or _current_process.get() or self.supervisor

        if telemetry.is_enabled():
            with telemetry.get_tracer().start_as_current_span(
                "fastactor.gen_server.call",
                attributes=self._telemetry_attrs(timeout=timeout),
            ) as span:
                try:
                    callmsg: Call[Req, Rep] = Call(sender, request, metadata=metadata)
                    try:
                        self.send_nowait(callmsg)
                    except (BrokenResourceError, ClosedResourceError):
                        raise Failed("noproc")
                    return await callmsg.result(timeout)
                except BaseException as error:
                    telemetry.record_exception(span, error)
                    raise

        callmsg = Call(sender, request, metadata=metadata)
        try:
            self.send_nowait(callmsg)
        except (BrokenResourceError, ClosedResourceError):
            raise Failed("noproc")
        return await callmsg.result(timeout)

    def cast(
        self,
        request: Req,
        sender: Process | None = None,
        *,
        metadata: dict[str, t.Any] | None = None,
    ) -> None:
        from .runtime import _current_process

        sender = sender or _current_process.get() or self.supervisor
        try:
            self.send_nowait(Cast(sender, request, metadata=metadata))
        except (BrokenResourceError, ClosedResourceError):
            pass

    async def _do_handle_call(self, message: Call[Req, Rep]) -> None:
        try:
            reply = await self.handle_call(message)
            match reply:
                case None:
                    pass
                case (value, Continue() as continuation):
                    self._set_pending_continue(continuation)
                    message.set_result(value)
                case (value, Stop() as stop):
                    message.set_result(value)
                    raise Shutdown(stop.reason)
                case (value, int() | float() as timeout_ms):
                    # {reply, Reply, State, Timeout} — reply + arm idle timer
                    self._idle_timeout = timeout_ms / 1000.0
                    message.set_result(value)
                case Stop() as stop:
                    # {stop, Reason, State} — no reply; caller gets Shutdown
                    message.set_result(Shutdown(stop.reason))
                    raise Shutdown(stop.reason)
                case _:
                    message.set_result(t.cast(Rep, reply))
        except Shutdown:
            raise
        except Exception as error:
            message.set_result(error)
            logger.error(
                "%s encountered %r processing %s",
                self,
                error,
                message,
            )
            raise

    async def _do_handle_cast(self, message: Cast[Req]) -> None:
        try:
            result = await self.handle_cast(message)
            if isinstance(result, Continue):
                self._set_pending_continue(result)
        except Exception as error:
            logger.error(
                "%s encountered %r processing %s",
                self,
                error,
                message,
            )
            raise

    async def _handle_message(self, message: t.Any) -> None:
        match message:
            case Call():
                if telemetry.is_enabled():
                    with telemetry.get_tracer().start_as_current_span(
                        "fastactor.gen_server.handle_call",
                        attributes=self._telemetry_attrs(),
                    ) as span:
                        try:
                            await self._do_handle_call(message)
                        except Shutdown:
                            raise
                        except Exception as error:
                            telemetry.record_exception(span, error)
                            raise
                else:
                    await self._do_handle_call(message)
            case Cast():
                if telemetry.is_enabled():
                    with telemetry.get_tracer().start_as_current_span(
                        "fastactor.gen_server.handle_cast",
                        attributes=self._telemetry_attrs(),
                    ) as span:
                        try:
                            await self._do_handle_cast(message)
                        except Exception as error:
                            telemetry.record_exception(span, error)
                            raise
                else:
                    await self._do_handle_cast(message)
            case _:
                await super()._handle_message(message)
