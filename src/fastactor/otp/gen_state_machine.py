import logging
import typing as t
from collections import deque
from dataclasses import dataclass, field

import anyio
from anyio import (
    CancelScope,
    ClosedResourceError,
    EndOfStream,
    create_memory_object_stream,
)

from fastactor import telemetry
from fastactor.settings import settings

from ._exceptions import Crashed, Shutdown, is_normal_shutdown_reason
from ._messages import Call, Cast, Down, Exit, Info, Stop
from .process import Process

logger = logging.getLogger(__name__)


@dataclass
class Reply:
    """Reply to a call. `to` is the Call message; calls to.set_result(reply)."""

    to: Call
    reply: t.Any


@dataclass
class Postpone:
    """Re-enqueue current event; replayed after the next state change."""

    pass


@dataclass
class NextEvent:
    """Inject an event before the next mailbox message."""

    event_type: t.Any
    event_content: t.Any


@dataclass
class StateTimeout:
    """Fires 'state_timeout' event after `time` seconds. Cancelled on state change."""

    time: float | None
    content: t.Any = None


@dataclass
class Timeout:
    """Generic timeout. Named timeouts persist across state changes until cancelled."""

    time: float | None
    content: t.Any = None
    name: t.Any = None


@dataclass
class Hibernate:
    """No-op in Python/asyncio. Supported for API compatibility."""

    pass


@dataclass
class _TimerMsg:
    event_type: t.Any
    content: t.Any


@dataclass(repr=False, eq=False)
class GenStateMachine(Process):
    callback_mode: t.ClassVar[str | tuple[str, ...]] = "handle_event_function"

    _gsm_state: t.Any = field(default=None, init=False)
    _gsm_data: t.Any = field(default=None, init=False)
    _init_actions: list[t.Any] = field(default_factory=list, init=False)
    _next_events: deque[tuple[t.Any, t.Any]] = field(default_factory=deque, init=False)
    _postponed: list[tuple[t.Any, t.Any]] = field(default_factory=list, init=False)
    _timer_tg: t.Any = field(default=None, init=False)
    _state_timeout_scope: CancelScope | None = field(default=None, init=False)
    _named_timers: dict[t.Any, CancelScope] = field(default_factory=dict, init=False)

    @property
    def _gsm_state_enter(self) -> bool:
        mode = self.callback_mode
        if isinstance(mode, tuple):
            return "state_enter" in mode
        return False

    @property
    def _gsm_callback_mode(self) -> str:
        mode = self.callback_mode
        if isinstance(mode, tuple):
            return next(m for m in mode if m != "state_enter")
        return mode

    @property
    def state(self) -> t.Any:
        return self._gsm_state

    @property
    def data(self) -> t.Any:
        return self._gsm_data

    async def init(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        raise NotImplementedError

    async def handle_event(
        self,
        event_type: t.Any,
        event_content: t.Any,
        state: t.Any,
        data: t.Any,
    ) -> t.Any:
        return "keep_state_and_data"

    async def terminate(
        self, reason: t.Any, state: t.Any = None, data: t.Any = None
    ) -> None:
        pass

    async def on_terminate(self, reason: t.Any) -> None:
        await self.terminate(reason, self._gsm_state, self._gsm_data)

    async def loop(self, *args: t.Any, **kwargs: t.Any) -> None:
        self._next_events = deque()
        self._postponed = []
        self._state_timeout_scope = None
        self._named_timers = {}
        self._init_actions = []
        try:
            async with anyio.create_task_group() as tg:
                self._timer_tg = tg
                try:
                    await super().loop(*args, **kwargs)
                finally:
                    tg.cancel_scope.cancel()
        finally:
            self._timer_tg = None
            self._state_timeout_scope = None
            self._named_timers = {}

    async def _init(self, *args: t.Any, **kwargs: t.Any) -> None:
        logger.debug("%s init", self)
        try:
            result = await self.init(*args, **kwargs)
        except Exception as error:
            raise Crashed(str(error)) from error

        match result:
            case (state, data, actions):
                self._gsm_state = state
                self._gsm_data = data
                self._init_actions = list(actions)
            case (state, data):
                self._gsm_state = state
                self._gsm_data = data
            case _:
                raise Crashed(
                    "init must return (state, data) or "
                    f"(state, data, [actions]), got: {result!r}"
                )

        self._inbox, self._mailbox = create_memory_object_stream(settings.mailbox_size)

    async def _run_in_process_context[R](
        self,
        func: t.Callable[..., t.Awaitable[R]],
        *args: t.Any,
        **kwargs: t.Any,
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

    async def _loop(self) -> None:
        if self._mailbox is None:
            return

        reason: t.Any = "normal"

        try:
            async with self._mailbox:
                logger.debug("%s loop started", self)
                await self._emit("started")
                self._started.set()

                if self._gsm_state_enter:
                    await self._run_in_process_context(
                        self._process_gsm_event,
                        "enter",
                        self._gsm_state,
                    )

                if self._init_actions:
                    actions = self._init_actions[:]
                    self._init_actions = []
                    await self._apply_actions(actions, None, None)

                while True:
                    if self._pending_continue is not None:
                        continuation = self._pending_continue
                        self._pending_continue = None
                        await self._run_in_process_context(
                            self.handle_continue,
                            continuation.term,
                        )
                        continue

                    if self._next_events:
                        ev_type, ev_content = self._next_events.popleft()
                        await self._run_in_process_context(
                            self._process_gsm_event,
                            ev_type,
                            ev_content,
                        )
                        continue

                    try:
                        message = await self._mailbox.receive()
                        logger.debug("%s received message %s", self, message)
                    except (EndOfStream, ClosedResourceError):
                        break

                    await self._emit("message:received", message=message)
                    if telemetry.is_enabled():
                        parent_ctx = telemetry.extract(
                            getattr(message, "metadata", None)
                        )
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
                                    self._handle_message,
                                    message,
                                )
                            except BaseException as error:
                                telemetry.record_exception(span, error)
                                raise
                    else:
                        await self._run_in_process_context(
                            self._handle_message,
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
            try:
                await super().terminate(reason)
            finally:
                self._stopped.set()
                self._started.set()

    async def _handle_message(self, message: t.Any) -> None:
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
            case Call():
                try:
                    await self._process_gsm_event(("call", message), message.message)
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
            case Cast():
                await self._process_gsm_event("cast", message.message)
            case Info():
                await self._process_gsm_event("info", message.message)
            case _TimerMsg(event_type=ev_type, content=content):
                await self._process_gsm_event(ev_type, content)
            case _:
                logger.warning("%s dropped unexpected message %r", self, message)

    async def _dispatch_gsm_callback(
        self, event_type: t.Any, event_content: t.Any
    ) -> t.Any:
        mode = self._gsm_callback_mode
        if mode == "handle_event_function":
            return await self.handle_event(
                event_type,
                event_content,
                self._gsm_state,
                self._gsm_data,
            )
        if mode == "state_functions":
            handler = getattr(self, str(self._gsm_state), None)
            if handler is None:
                raise NotImplementedError(
                    f"{type(self).__name__} has no state function for state: "
                    f"{self._gsm_state!r}"
                )
            if event_type == "enter":
                return await handler("enter", event_content, self._gsm_data)
            return await handler(event_type, event_content, self._gsm_data)
        raise ValueError(f"Unknown callback mode: {mode!r}")

    def _unpack_result(
        self, result: t.Any, event_type: t.Any
    ) -> tuple[t.Any, t.Any, list[t.Any]]:
        state = self._gsm_state
        data = self._gsm_data

        match result:
            case ("next_state", new_state, new_data):
                return new_state, new_data, []
            case ("next_state", new_state, new_data, actions):
                return new_state, new_data, list(actions)
            case ("keep_state", new_data):
                return state, new_data, []
            case ("keep_state", new_data, actions):
                return state, new_data, list(actions)
            case "keep_state_and_data":
                return state, data, []
            case ("keep_state_and_data", actions):
                return state, data, list(actions)
            case ("stop", reason):
                return state, data, [("__stop__", reason)]
            case ("stop_and_reply", reason, reply_actions):
                return state, data, list(reply_actions) + [("__stop__", reason)]
            case _:
                logger.warning(
                    "%s unexpected callback result %r for event %r",
                    self,
                    result,
                    event_type,
                )
                return state, data, []

    async def _process_gsm_event(self, event_type: t.Any, event_content: t.Any) -> None:
        is_enter = event_type == "enter"

        result = await self._dispatch_gsm_callback(event_type, event_content)
        old_state = self._gsm_state
        new_state, new_data, actions = self._unpack_result(result, event_type)

        if is_enter and new_state != old_state:
            logger.warning(
                "%s state_enter callback must not change state (got %r -> %r); "
                "ignoring state change",
                self,
                old_state,
                new_state,
            )
            new_state = old_state

        state_changed = new_state != old_state
        if state_changed and self._state_timeout_scope is not None:
            self._state_timeout_scope.cancel()
            self._state_timeout_scope = None

        await self._apply_actions(actions, event_type, event_content)

        self._gsm_state = new_state
        self._gsm_data = new_data

        if state_changed:
            to_prepend: list[tuple[t.Any, t.Any]] = []
            if self._gsm_state_enter:
                to_prepend.append(("enter", old_state))
            to_prepend.extend(self._postponed)
            self._postponed = []

            for item in reversed(to_prepend):
                self._next_events.appendleft(item)

    async def _apply_actions(
        self,
        actions: list[t.Any],
        current_event_type: t.Any,
        current_event_content: t.Any,
    ) -> None:
        for action in actions:
            match action:
                case ("__stop__", reason):
                    raise Shutdown(reason)
                case Reply(to=call_msg, reply=reply_val):
                    call_msg.set_result(reply_val)
                case Postpone():
                    if current_event_type is not None:
                        self._postponed.append(
                            (current_event_type, current_event_content)
                        )
                case NextEvent(event_type=ev_type, event_content=ev_content):
                    self._next_events.append((ev_type, ev_content))
                case StateTimeout(time=t_val, content=content):
                    if self._state_timeout_scope is not None:
                        self._state_timeout_scope.cancel()
                        self._state_timeout_scope = None
                    if t_val is not None:
                        self._state_timeout_scope = await self._schedule_timer(
                            "state_timeout",
                            t_val,
                            content,
                        )
                case Timeout(time=t_val, content=content, name=name):
                    if name is not None:
                        existing = self._named_timers.pop(name, None)
                        if existing is not None:
                            existing.cancel()
                        if t_val is not None:
                            self._named_timers[name] = await self._schedule_timer(
                                ("timeout", name),
                                t_val,
                                content,
                            )
                    elif t_val is not None:
                        await self._schedule_timer("timeout", t_val, content)
                case Hibernate():
                    pass
                case _:
                    logger.warning("%s unknown action %r", self, action)

    async def _schedule_timer(
        self, event_type: t.Any, time_secs: float, content: t.Any
    ) -> CancelScope:
        if self._timer_tg is None:
            raise RuntimeError("timer task group is not available")

        scope = CancelScope()
        timer_name = None
        if (
            isinstance(event_type, tuple)
            and len(event_type) == 2
            and event_type[0] == "timeout"
        ):
            timer_name = event_type[1]

        async def _run() -> None:
            try:
                with scope:
                    await anyio.sleep(time_secs)
                    if self._inbox is not None:
                        try:
                            self._inbox.send_nowait(_TimerMsg(event_type, content))  # type: ignore
                        except Exception:
                            pass
            finally:
                if self._state_timeout_scope is scope:
                    self._state_timeout_scope = None
                if (
                    timer_name is not None
                    and self._named_timers.get(timer_name) is scope
                ):
                    self._named_timers.pop(timer_name, None)

        self._timer_tg.start_soon(_run)
        return scope

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
        request: t.Any,
        sender: "Process | None" = None,
        timeout: float = settings.call_timeout,
        *,
        metadata: dict[str, t.Any] | None = None,
    ) -> t.Any:
        from .runtime import _current_process

        sender = sender or _current_process.get() or self.supervisor
        if telemetry.is_enabled():
            with telemetry.get_tracer().start_as_current_span(
                "fastactor.gen_state_machine.call",
                attributes=self._telemetry_attrs(timeout=timeout),
            ) as span:
                try:
                    callmsg: Call = Call(sender, request, metadata=metadata)
                    self.send_nowait(callmsg)
                    return await callmsg.result(timeout)
                except BaseException as error:
                    telemetry.record_exception(span, error)
                    raise

        callmsg = Call(sender, request, metadata=metadata)
        self.send_nowait(callmsg)
        return await callmsg.result(timeout)

    def cast(
        self,
        request: t.Any,
        sender: "Process | None" = None,
        *,
        metadata: dict[str, t.Any] | None = None,
    ) -> None:
        from .runtime import _current_process

        sender = sender or _current_process.get() or self.supervisor
        self.send_nowait(Cast(sender, request, metadata=metadata))
