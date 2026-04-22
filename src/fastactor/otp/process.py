"""Base actor — bounded mailbox, lifecycle events, links/monitors, trap_exits.

Override `init`, `handle_info`, `handle_exit`, `handle_continue`, `on_terminate`
on your subclass. See `src/fastactor/otp/README.md#process` for the full callback
contract and lifecycle semantics, and the `handle_continue` section for post-callback
continuations.
"""

import logging
import typing as t
from dataclasses import dataclass, field

from anyio import (
    ClosedResourceError,
    EndOfStream,
    Event,
    create_memory_object_stream,
    fail_after,
)
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pyee import EventEmitter

from fastactor import telemetry
from fastactor.settings import settings
from fastactor.utils import emit_awaited, id_generator

from ._exceptions import Crashed, Shutdown, is_normal_shutdown_reason
from ._messages import Continue, Down, Exit, Ignore, Info, Message, Stop

if t.TYPE_CHECKING:
    from .supervisor import Supervisor

logger = logging.getLogger(__name__)
_monitor_ref = id_generator("monitor")


@dataclass(repr=False)
class Process:
    id: str = field(default_factory=id_generator("process"))
    supervisor: "Supervisor | None" = None
    trap_exits: bool = False

    _inbox: MemoryObjectSendStream[Message] | None = field(default=None, init=False)
    _mailbox: MemoryObjectReceiveStream[Message] | None = field(
        default=None, init=False
    )

    _started: Event = field(default_factory=Event, init=False)
    _stopped: Event = field(default_factory=Event, init=False)
    _crash_exc: Exception | None = field(default=None, init=False)
    _pending_continue: Continue | None = field(default=None, init=False)
    _forced_exit_reason: t.Any = field(default=None, init=False)
    _skip_terminate: bool = field(default=False, init=False)
    _ignored: bool = field(default=False, init=False)

    links: set["Process"] = field(default_factory=set, init=False)
    monitors: dict[str, "Process"] = field(default_factory=dict, init=False)
    monitored_by: dict[str, "Process"] = field(default_factory=dict, init=False)
    _ignored_down_refs: set[str] = field(default_factory=set, init=False)

    emitter: EventEmitter = field(default_factory=EventEmitter, init=False)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other: object):
        if not isinstance(other, Process):
            return NotImplemented
        return self.id == other.id

    async def _emit(self, event: str, **payload: t.Any) -> None:
        await emit_awaited(self.emitter, event, self, **payload)
        try:
            from .runtime import Runtime

            rt = Runtime.current()
        except RuntimeError:
            return
        await emit_awaited(rt.emitter, event, self, **payload)

    async def _init(self, *args, **kwargs):
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
            case _:
                self._inbox, self._mailbox = create_memory_object_stream(
                    settings.mailbox_size
                )

    async def init(self, *args, **kwargs) -> Ignore | Stop | Continue | None:
        return None

    def has_started(self) -> bool:
        return self._started.is_set()

    async def started(self) -> t.Self:
        await self._started.wait()
        return self

    def has_stopped(self) -> bool:
        return self._stopped.is_set()

    async def stopped(self) -> t.Self:
        await self._stopped.wait()
        return self

    async def handle_exit(self, message: Exit) -> None:
        pass

    async def handle_down(self, message: Down) -> None:
        pass

    async def handle_continue(self, term: t.Any) -> Continue | None:
        return None

    async def on_terminate(self, reason: t.Any) -> None:
        return None

    async def terminate(self, reason: t.Any) -> None:
        """User-overridable lifecycle callback. Override for custom cleanup."""
        logger.debug("%s terminate reason=%s", self, reason)
        try:
            await self.on_terminate(reason)
        except Exception as error:
            logger.error("%r in on_terminate of %s: %s", error, self, reason)

    async def _system_teardown(self, reason: t.Any) -> None:
        """System cleanup: always runs on process exit regardless of trap_exits."""
        abnormal = not is_normal_shutdown_reason(reason)

        if self.monitored_by:
            logger.debug("%s notifying monitors", self)
            for ref, process in list(self.monitored_by.items()):
                self.monitored_by.pop(ref, None)
                process._drop_monitor_ref(ref)
                try:
                    await process.send(Down(self, reason, ref))
                except Exception as error:
                    logger.error(
                        "%r sending Down(%s, %s, %s) to monitor: %s",
                        error,
                        self,
                        reason,
                        ref,
                        process,
                    )

        if self.links:
            logger.debug("%s notifying links", self)
            for process in list(self.links):
                self.unlink(process)
                if process.trap_exits:
                    try:
                        await process.send(Exit(self, reason))
                    except Exception as error:
                        logger.error(
                            "%r sending Exit(%s, %s) to linked actor: %s",
                            error,
                            self,
                            reason,
                            process,
                        )
                elif abnormal:
                    try:
                        await process.stop(reason, sender=self)
                    except Exception as error:
                        logger.error("%r killing %s: %s", error, process, reason)

        for ref, process in list(self.monitors.items()):
            self.monitors.pop(ref, None)
            process.monitored_by.pop(ref, None)

        try:
            from .runtime import Runtime

            await Runtime.current().unregister(self)
        except RuntimeError:
            pass

        logger.debug("%s terminated reason=%s", self, reason)

        if abnormal:
            await self._emit("crashed", exc=self._crash_exc, reason=reason)
        await self._emit("stopped", reason=reason)

    def link(self, other: "Process"):
        self.links.add(other)
        other.links.add(self)

    def unlink(self, other: "Process"):
        self.links.discard(other)
        other.links.discard(self)

    def _drop_monitor_ref(self, ref: str) -> None:
        self.monitors.pop(ref, None)

    def _monitor_refs(self, other_or_ref: "Process | str") -> list[str]:
        if isinstance(other_or_ref, Process):
            return [
                ref for ref, process in self.monitors.items() if process == other_or_ref
            ]
        return [other_or_ref]

    def monitor(self, other: "Process") -> str:
        ref = _monitor_ref()
        if other.has_stopped():
            self.send_nowait(Down(other, "noproc", ref))
            return ref

        self.monitors[ref] = other
        other.monitored_by[ref] = self
        return ref

    def demonitor(self, other_or_ref: "Process | str", *, flush: bool = False) -> None:
        refs = self._monitor_refs(other_or_ref)

        for ref in refs:
            other = self.monitors.pop(ref, None)
            if other is not None:
                other.monitored_by.pop(ref, None)
            elif flush:
                self._ignored_down_refs.add(ref)

    def _prepare_outbound(self, message: t.Any) -> t.Any:
        if telemetry.is_enabled() and isinstance(message, Message):
            message.metadata = telemetry.inject(message.metadata)
        return message

    async def send(self, message: t.Any):
        assert self._inbox is not None
        await self._inbox.send(self._prepare_outbound(message))

    def send_nowait(self, message: t.Any):
        assert self._inbox is not None
        self._inbox.send_nowait(self._prepare_outbound(message))

    async def stop(
        self,
        reason: t.Any = "normal",
        timeout: float | None = settings.stop_timeout,
        sender: "Process | None" = None,
    ):
        if self.has_stopped():
            return

        logger.debug("%s stop %s", self, reason)
        self.send_nowait(Stop(sender or self.supervisor, reason))
        with fail_after(timeout):
            await self.stopped()

    async def kill(self):
        if self.has_stopped():
            return

        logger.debug("%s kill", self)
        self._forced_exit_reason = "killed"
        self._skip_terminate = True
        if self._inbox is not None:
            await self._inbox.aclose()
        await self.stopped()

    def info(
        self,
        message: t.Any,
        sender: "Process | None" = None,
        *,
        metadata: dict[str, t.Any] | None = None,
    ):
        sender = sender or self.supervisor
        self.send_nowait(Info(sender, message, metadata=metadata))

    async def handle_info(self, message: Info) -> Continue | None:
        return None

    async def _run_init_safe(self, *args, **kwargs) -> bool:
        try:
            await self._init(*args, **kwargs)
        except (Crashed, Shutdown) as error:
            self._crash_exc = error
            self._started.set()
            self._stopped.set()
            return False
        return True

    async def loop(self, *args, **kwargs):
        if not await self._run_init_safe(*args, **kwargs):
            return
        await self._loop()

    async def _run_in_process_context[R](
        self,
        func: t.Callable[..., t.Awaitable[R]],
        *args,
        **kwargs,
    ) -> R:
        from .runtime import current_process

        token = current_process.set(self)
        try:
            return await func(*args, **kwargs)
        finally:
            current_process.reset(token)

    def _set_pending_continue(self, result: t.Any):
        if isinstance(result, Continue):
            self._pending_continue = result

    async def _receive_next(self) -> t.Any:
        assert self._mailbox is not None
        return await self._mailbox.receive()

    async def _loop(self):
        if self._mailbox is None:
            return

        reason: t.Any = "normal"

        try:
            async with self._mailbox:
                logger.debug("%s loop started", self)
                await self._emit("started")
                self._started.set()
                while True:
                    if self._pending_continue is not None:
                        continuation = self._pending_continue
                        self._pending_continue = None
                        await self._run_in_process_context(
                            self.handle_continue,
                            continuation.term,
                        )
                        continue

                    try:
                        message = await self._receive_next()
                        logger.debug("%s received message %s", self, message)
                    except (EndOfStream, ClosedResourceError):
                        break

                    await self._emit("message:received", message=message)
                    if telemetry.is_enabled():
                        parent_ctx = None
                        if isinstance(message, Message):
                            parent_ctx = telemetry.extract(message.metadata)
                        attrs: dict[str, t.Any] = {
                            telemetry.ATTR_PROCESS_ID: self.id,
                            telemetry.ATTR_PROCESS_CLASS: type(self).__name__,
                            telemetry.ATTR_MESSAGE_TYPE: type(message).__name__,
                        }
                        sender = getattr(message, "sender", None)
                        if sender is not None and hasattr(sender, "id"):
                            attrs[telemetry.ATTR_MESSAGE_SENDER_ID] = sender.id
                        with telemetry.get_tracer().start_as_current_span(
                            "fastactor.process.handle_message",
                            context=parent_ctx,
                            attributes=attrs,
                        ) as span:
                            try:
                                await self._run_in_process_context(
                                    self._dispatch_message,
                                    message,
                                )
                            except BaseException as err:
                                telemetry.record_exception(span, err)
                                raise
                    else:
                        await self._run_in_process_context(
                            self._dispatch_message,
                            message,
                        )
                    await self._emit("message:handled", message=message)
                if self._forced_exit_reason is not None:
                    reason = self._forced_exit_reason
        except ClosedResourceError:
            reason = self._forced_exit_reason or "killed"
        except Shutdown as error:
            reason = error.reason
            if not is_normal_shutdown_reason(reason):
                self._crash_exc = (
                    reason if isinstance(reason, Exception) else Crashed(str(reason))
                )
        except Exception as error:
            self._crash_exc = error
            reason = error
        finally:
            # Call user terminate() only when:
            # - not brutal-killed (_skip_terminate=False), AND
            # - trap_exits=True, OR reason is "normal", OR reason is a crash
            # (Erlang skips terminate/2 for supervisor-shutdown on non-trapping procs)
            _should_call_terminate = not self._skip_terminate and (
                self.trap_exits
                or reason == "normal"
                or not is_normal_shutdown_reason(reason)
            )
            try:
                if _should_call_terminate:
                    await self.terminate(reason)
            finally:
                try:
                    await self._system_teardown(reason)
                finally:
                    # Always flip _stopped before _started so spawn()'s has_stopped()
                    # check is accurate when a "started" listener raises (otherwise
                    # spawn would wake on _started, see has_stopped=False, and return
                    # a dying process). Both must fire even if terminate/teardown raised.
                    self._stopped.set()
                    self._started.set()

    async def _dispatch_message(self, message: t.Any) -> None:
        # Reserve runtime control messages even when subclasses consume raw
        # mailbox items by overriding _handle_message directly.
        match message:
            case Stop() | Exit() | Down():
                await Process._handle_message(self, message)
            case _:
                await self._handle_message(message)

    async def _handle_message(self, message: Message) -> None:
        match message:
            case Stop(_, reason):
                raise Shutdown(reason)
            case Exit(_, reason):
                if not self.trap_exits and not is_normal_shutdown_reason(reason):
                    if message.sender is not None:
                        self.unlink(message.sender)
                    raise Shutdown(reason)
                await self.handle_exit(message)
            case Down(ref=ref):
                if ref is not None and ref in self._ignored_down_refs:
                    self._ignored_down_refs.discard(ref)
                    return
                await self.handle_down(message)
            case Info():
                result = await self.handle_info(message)
                self._set_pending_continue(result)
            case _:
                logger.warning("%s dropped unexpected message %r", self, message)

    @classmethod
    def _configure_before_spawn(
        cls, process: "Process", kwargs: dict[str, t.Any]
    ) -> None:
        return

    @classmethod
    async def _spawn(
        cls,
        *args,
        trap_exits: bool | None = None,
        supervisor: "Supervisor | None" = None,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        link: bool = False,
        **kwargs,
    ) -> t.Self:
        from .runtime import Runtime

        runtime = Runtime.current()
        supervisor = supervisor or runtime.supervisor

        if trap_exits is None:
            process = cls(supervisor=supervisor)
        else:
            process = cls(supervisor=supervisor, trap_exits=trap_exits)

        cls._configure_before_spawn(process, kwargs)

        if link and supervisor is not None:
            process.link(supervisor)

        return await runtime.spawn(process, *args, name=name, via=via, **kwargs)

    @classmethod
    async def start(
        cls,
        *args,
        **kwargs,
    ) -> t.Self:
        return await cls._spawn(*args, link=False, **kwargs)

    @classmethod
    async def start_link(
        cls,
        *args,
        **kwargs,
    ) -> t.Self:
        return await cls._spawn(*args, link=True, **kwargs)
