"""Base actor — bounded mailbox, lifecycle events, links/monitors, trap_exits.

Override `init`, `handle_info`, `handle_exit`, `handle_continue`, `on_terminate`
on your subclass. See `src/fastactor/otp/README.md#process` for the full callback
contract and lifecycle semantics, and the `handle_continue` section for post-callback
continuations.
"""

import logging
import typing as t
from dataclasses import dataclass, field

import anyio
from anyio import (
    BrokenResourceError,
    ClosedResourceError,
    EndOfStream,
    Event,
    create_memory_object_stream,
    fail_after,
)
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pyee import EventEmitter

from fastactor import telemetry
from fastactor.settings import settings
from fastactor.utils import emit_awaited, id_generator

from ._exceptions import Crashed, Failed, Shutdown, is_normal_shutdown_reason
from ._messages import (
    Call,
    Continue,
    Down,
    Exit,
    Ignore,
    Info,
    Message,
    Stop,
    _call_ref,
    _pid_of,
)

if t.TYPE_CHECKING:
    from .supervisor import Supervisor

logger = logging.getLogger(__name__)
_monitor_ref = id_generator("monitor")


@dataclass(frozen=True, slots=True)
class TimerRef:
    """Opaque handle returned by send_after / start_interval."""

    _id: int


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
    _pending_calls: dict[str, Event] = field(default_factory=dict, init=False)
    _pending_results: dict[str, t.Any] = field(default_factory=dict, init=False)
    _proc_timer_tg: TaskGroup | None = field(default=None, init=False)
    _proc_timers: dict[int, anyio.CancelScope] = field(
        default_factory=dict, init=False
    )
    _proc_next_timer_id: int = field(default=0, init=False)
    _proc_timer_fire_times: dict[int, float] = field(
        default_factory=dict, init=False
    )
    _proc_interval_timers: set[int] = field(default_factory=set, init=False)
    _proc_named_timers: dict[str, TimerRef] = field(default_factory=dict, init=False)

    emitter: EventEmitter = field(default_factory=EventEmitter, init=False)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other: object):
        if not isinstance(other, Process):
            return NotImplemented
        return self.id == other.id

    async def _emit(self, event: str, **payload: t.Any) -> None:
        await emit_awaited(self.emitter, event, self, **payload)
        from .runtime import Runtime

        await emit_awaited(Runtime.current().emitter, event, self, **payload)

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

    async def handle_continue(self, term: t.Any) -> Continue | Stop | None:
        return None

    async def on_terminate(self, reason: t.Any) -> None:
        return None

    async def terminate(self, reason: t.Any) -> None:
        """User-overridable lifecycle callback. Override for custom cleanup."""
        self._teardown_proc_timers()
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
                    await process.send(Down(pid=_pid_of(self), reason=reason, ref=ref))
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
                        await process.send(Exit(pid=_pid_of(self), reason=reason))
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

        from .runtime import Runtime

        await Runtime.current().unregister(self)

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

    def _deliver_reply(self, ref: str, result: t.Any) -> None:
        """Wire a Call reply into the caller's pending-calls side-table.

        Silently drops if the caller already exited (timed out / cancelled).
        Otherwise a late reply would leak a `_pending_results` entry.
        """
        event = self._pending_calls.get(ref)
        if event is None:
            return
        self._pending_results[ref] = result
        event.set()

    async def _perform_call(
        self,
        request: t.Any,
        caller: "Process | None",
        timeout: float,
        metadata: dict[str, t.Any] | None,
    ) -> t.Any:
        """Shared call machinery — used by both GenServer and GenStateMachine.

        `self` is the target, `caller` is the awaiting process. The reply is
        delivered into `caller._pending_{calls,results}` by the target's
        `Call.set_result(value)` → `caller._deliver_reply(ref, value)` path.
        """
        if caller is None:
            raise Failed("no_caller")

        ref = _call_ref()
        event = Event()
        caller._pending_calls[ref] = event
        callmsg = Call(
            pid=_pid_of(caller),
            message=request,
            ref=ref,
            metadata=metadata,
        )
        try:
            try:
                self.send_nowait(callmsg)
            except (BrokenResourceError, ClosedResourceError):
                raise Failed("noproc")

            with fail_after(timeout):
                await event.wait()

            result = caller._pending_results.pop(ref, None)
        finally:
            caller._pending_calls.pop(ref, None)
            caller._pending_results.pop(ref, None)

        if isinstance(result, Exception):
            raise result
        return result

    def _monitor_refs(self, other_or_ref: "Process | str") -> list[str]:
        if isinstance(other_or_ref, Process):
            return [
                ref for ref, process in self.monitors.items() if process == other_or_ref
            ]
        return [other_or_ref]

    def monitor(self, other: "Process") -> str:
        ref = _monitor_ref()
        if other.has_stopped():
            self.send_nowait(Down(pid=_pid_of(other), reason="noproc", ref=ref))
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
        if not isinstance(message, Message):
            return message
        if telemetry.is_enabled():
            message.metadata = telemetry.inject(message.metadata)
        sid = message.pid
        if sid is not None and not sid.node and "_sender_ref" not in message.__dict__:
            from .runtime import Runtime

            sender_proc = Runtime.current().processes.get(sid.id)
            if sender_proc is not None:
                message.__dict__["_sender_ref"] = sender_proc
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
        self.send_nowait(Stop(pid=_pid_of(sender or self.supervisor), reason=reason))
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
        self.send_nowait(Info(pid=_pid_of(sender), message=message, metadata=metadata))

    async def handle_info(self, message: Info) -> Continue | None:
        return None

    def _next_proc_timer_ref(self) -> TimerRef:
        self._proc_next_timer_id += 1
        return TimerRef(self._proc_next_timer_id)

    def _require_proc_timer_tg(self) -> TaskGroup:
        if self._proc_timer_tg is None:
            raise RuntimeError("process not running")
        return self._proc_timer_tg

    def _send_timer_info(
        self,
        target: "Process",
        message: object,
        *,
        kind: str,
        ack: Event | None = None,
    ) -> bool:
        info = Info(pid=_pid_of(self), message=message)
        if ack is not None:
            info.__dict__["_timer_ack"] = ack

        if telemetry.is_enabled():
            with telemetry.get_tracer().start_as_current_span(
                "fastactor.process.timer_fire",
                attributes={
                    telemetry.ATTR_TARGET_ID: target.id,
                    telemetry.ATTR_TARGET_CLASS: type(target).__name__,
                    telemetry.ATTR_TIMER_KIND: kind,
                },
            ) as span:
                try:
                    target.send_nowait(info)
                except (BrokenResourceError, ClosedResourceError):
                    return False
                except BaseException as err:
                    telemetry.record_exception(span, err)
                    raise
            return True

        try:
            target.send_nowait(info)
        except (BrokenResourceError, ClosedResourceError):
            return False
        return True

    async def _run_send_after_timer(
        self,
        timer_id: int,
        delay_s: float,
        message: object,
        target: "Process",
        scope: anyio.CancelScope,
    ) -> None:
        try:
            with scope:
                await anyio.sleep(delay_s)
                self._send_timer_info(target, message, kind="send_after")
        finally:
            if self._proc_timers.get(timer_id) is scope:
                self._proc_timers.pop(timer_id, None)
                self._proc_timer_fire_times.pop(timer_id, None)
                self._proc_interval_timers.discard(timer_id)

    async def _run_interval_timer(
        self,
        timer_id: int,
        interval_s: float,
        message: object,
        target: "Process",
        scope: anyio.CancelScope,
    ) -> None:
        try:
            with scope:
                while True:
                    self._proc_timer_fire_times[timer_id] = (
                        anyio.current_time() + interval_s
                    )
                    await anyio.sleep(interval_s)
                    ack = (
                        Event()
                        if getattr(target._loop, "__func__", None) is Process._loop
                        else None
                    )
                    if self._send_timer_info(target, message, kind="interval", ack=ack):
                        if ack is not None:
                            await ack.wait()
        finally:
            if self._proc_timers.get(timer_id) is scope:
                self._proc_timers.pop(timer_id, None)
                self._proc_timer_fire_times.pop(timer_id, None)
                self._proc_interval_timers.discard(timer_id)

    def send_after(
        self,
        delay_ms: int,
        message: object,
        *,
        target: "Process | None" = None,
    ) -> TimerRef:
        timer_tg = self._require_proc_timer_tg()
        target = target or self
        ref = self._next_proc_timer_ref()
        scope = anyio.CancelScope()
        delay_s = max(delay_ms, 0) / 1000
        self._proc_timers[ref._id] = scope
        self._proc_timer_fire_times[ref._id] = anyio.current_time() + delay_s
        timer_tg.start_soon(
            self._run_send_after_timer,
            ref._id,
            delay_s,
            message,
            target,
            scope,
        )
        return ref

    def cancel_timer(self, ref: TimerRef) -> int | None:
        scope = self._proc_timers.get(ref._id)
        if scope is None:
            return None

        fire_time = self._proc_timer_fire_times.get(ref._id)
        if fire_time is None:
            scope.cancel()
            self._proc_timers.pop(ref._id, None)
            return None

        remaining_s = fire_time - anyio.current_time()
        if remaining_s <= 0 and ref._id not in self._proc_interval_timers:
            return None

        scope.cancel()
        self._proc_timers.pop(ref._id, None)
        self._proc_timer_fire_times.pop(ref._id, None)
        self._proc_interval_timers.discard(ref._id)
        if remaining_s <= 0:
            return None
        return int(max(0, remaining_s * 1000))

    def reschedule(self, name: str, delay_ms: int, message: object) -> TimerRef:
        """Cancel any prior timer registered under `name` for this process and
        schedule a new one. Idempotent -- calling reschedule("tick", 1000, ...)
        repeatedly always leaves exactly one pending timer named "tick"."""
        prior = self._proc_named_timers.get(name)
        if prior is not None:
            self.cancel_timer(prior)
        ref = self.send_after(delay_ms, message)
        self._proc_named_timers[name] = ref
        return ref

    def start_interval(
        self,
        interval_ms: int,
        message: object,
        *,
        target: "Process | None" = None,
    ) -> TimerRef:
        timer_tg = self._require_proc_timer_tg()
        target = target or self
        ref = self._next_proc_timer_ref()
        scope = anyio.CancelScope()
        interval_s = max(interval_ms, 0) / 1000
        self._proc_timers[ref._id] = scope
        self._proc_interval_timers.add(ref._id)
        self._proc_timer_fire_times[ref._id] = anyio.current_time() + interval_s
        timer_tg.start_soon(
            self._run_interval_timer,
            ref._id,
            interval_s,
            message,
            target,
            scope,
        )
        return ref

    def _teardown_proc_timers(self) -> None:
        for scope in list(self._proc_timers.values()):
            scope.cancel()
        self._proc_timers = {}
        self._proc_timer_fire_times = {}
        self._proc_interval_timers = set()
        self._proc_named_timers = {}
        self._proc_timer_tg = None

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
        try:
            async with anyio.create_task_group() as tg:
                self._proc_timer_tg = tg
                try:
                    if not await self._run_init_safe(*args, **kwargs):
                        return
                    await self._loop()
                finally:
                    self._teardown_proc_timers()
                    tg.cancel_scope.cancel()
        finally:
            self._proc_timer_tg = None
            self._proc_timers = {}
            self._proc_timer_fire_times = {}
            self._proc_interval_timers = set()
            self._proc_named_timers = {}

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
        match result:
            case Continue() as continuation:
                self._pending_continue = continuation
            case Stop(reason=reason):
                raise Shutdown(reason)

    async def _receive_next(self) -> t.Any:
        assert self._mailbox is not None
        return await self._mailbox.receive()

    async def _loop(self):
        if self._mailbox is None:
            return

        reason: t.Any = "normal"

        try:
            async with self._inbox, self._mailbox:
                logger.debug("%s loop started", self)
                await self._emit("started")
                self._started.set()
                while True:
                    if self._pending_continue is not None:
                        continuation = self._pending_continue
                        self._pending_continue = None
                        result = await self._run_in_process_context(
                            self.handle_continue,
                            continuation.term,
                        )
                        self._set_pending_continue(result)
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
            self._teardown_proc_timers()
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
        try:
            match message:
                case Stop() | Exit() | Down():
                    await Process._handle_message(self, message)
                case _:
                    await self._handle_message(message)
        finally:
            if isinstance(message, Message):
                ack = message.__dict__.pop("_timer_ack", None)
                if ack is not None:
                    ack.set()

    async def _handle_message(self, message: Message) -> None:
        match message:
            case Stop(reason=reason):
                raise Shutdown(reason)
            case Exit(reason=reason):
                if not self.trap_exits and not is_normal_shutdown_reason(reason):
                    sender = message.sender
                    if sender is not None:
                        self.unlink(sender)
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
