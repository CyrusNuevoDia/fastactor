"""Supervisor — OTP-style restart policies with `ChildSpec`-driven children.

Strategies: `one_for_one`, `one_for_all`, `rest_for_one`. Restart types:
`permanent` / `transient` / `temporary`. Shutdown types: int seconds, `"infinity"`,
`"brutal_kill"`. Restart intensity capped by `max_restarts` / `max_seconds`.
See `src/fastactor/otp/README.md#supervisor` for the full API and tuple-form
child-spec shortcut.
"""

import typing as t
from collections import deque
from dataclasses import dataclass, field
from inspect import iscoroutinefunction
from time import monotonic

from anyio import (
    BrokenResourceError,
    Event,
    create_memory_object_stream,
    create_task_group,
)
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from fastactor import telemetry
from fastactor.settings import settings

from ._exceptions import Failed, is_normal_shutdown_reason
from ._messages import Stop
from .process import Process

RestartType = t.Literal["permanent", "transient", "temporary"]
ShutdownType = float | t.Literal["brutal_kill", "infinity"]
RestartStrategy = t.Literal["one_for_one", "one_for_all", "rest_for_one"]


@dataclass
class ChildSpec[P: Process = Process]:
    id: str
    start: tuple[t.Callable[..., t.Any], tuple, dict]
    restart: RestartType = "permanent"
    shutdown: ShutdownType = settings.call_timeout
    type: t.Literal["worker", "supervisor"] = "worker"


@dataclass
class RunningChild:
    process: Process
    spec: ChildSpec


def _normalize_child_spec(spec: "ChildSpec | tuple") -> ChildSpec:
    """Accept either a ChildSpec or a (cls_or_coro, kwargs_dict) 2-tuple (Elixir-style).

    When given a tuple, the child id defaults to `cls.__name__`. Collisions are still
    detected by the calling supervisor's duplicate-id check.
    """
    if isinstance(spec, ChildSpec):
        return spec
    if isinstance(spec, tuple) and len(spec) == 2:
        cls_or_coro, kwargs = spec
        if not isinstance(kwargs, dict):
            raise TypeError(
                f"Tuple child spec expected (cls, kwargs_dict), got {spec!r}"
            )
        return Supervisor.child_spec(
            getattr(cls_or_coro, "__name__", "anon"),
            cls_or_coro,
            kwargs=kwargs,
        )
    raise TypeError(f"Unsupported child spec: {spec!r}")


def _build_child_spec[P: Process](
    child_id: str,
    func_or_class: type[P] | t.Callable[..., t.Awaitable[P]],
    args: tuple | None,
    kwargs: dict | None,
    restart: RestartType,
    shutdown: ShutdownType,
    type: t.Literal["worker", "supervisor"],
) -> ChildSpec[P]:
    if not hasattr(func_or_class, "start_link") and not iscoroutinefunction(
        func_or_class
    ):
        raise ValueError("Child must be a coroutine or have a start_link method")
    return ChildSpec(
        id=child_id,
        start=(func_or_class, args or tuple(), kwargs or {}),
        restart=restart,
        shutdown=shutdown,
        type=type,
    )


@dataclass(repr=False, eq=False)
class Supervisor(Process):
    trap_exits: bool = True
    strategy: RestartStrategy = "one_for_one"
    max_restarts: int = settings.supervisor_max_restarts
    max_seconds: float = settings.supervisor_max_seconds

    child_specs: dict[str, ChildSpec] = field(default_factory=dict)
    children: dict[str, RunningChild] = field(default_factory=dict, init=False)
    _task_group: TaskGroup | None = field(default=None, init=False)
    _terminating: bool = field(default=False, init=False)
    _strategy_stopped_events: dict[str, Event] = field(default_factory=dict, init=False)
    _strategy_updates_receive: MemoryObjectReceiveStream[None] | None = field(
        default=None,
        init=False,
    )
    _strategy_updates_send: MemoryObjectSendStream[None] | None = field(
        default=None,
        init=False,
    )

    async def init(
        self,
        strategy: RestartStrategy = "one_for_one",
        max_restarts: int = settings.supervisor_max_restarts,
        max_seconds: float = settings.supervisor_max_seconds,
        children: list | None = None,
    ):
        self.strategy = strategy
        self.max_restarts = max_restarts
        self.max_seconds = max_seconds
        if children:
            for raw_spec in children:
                normalized = _normalize_child_spec(raw_spec)
                if normalized.id in self.child_specs:
                    raise Failed(f"Duplicate child id in children=[]: {normalized.id}")
                self.child_specs[normalized.id] = normalized

    async def terminate(self, reason: t.Any):
        self._terminating = True
        ordered_ids = [
            child_id
            for child_id, _ in reversed(self._ordered_child_specs())
            if child_id in self.children
        ]

        for child_id in ordered_ids:
            running_child = self.children.get(child_id)
            if running_child is None:
                continue

            await self._shutdown_child(
                running_child.process,
                running_child.spec.shutdown,
                reason,
            )
        self.children.clear()
        await super().terminate(reason)

    async def _record_restart(
        self, restart_times: deque[float], spec_id: str | None = None
    ):
        now = monotonic()
        restart_times.append(now)
        while restart_times and now - restart_times[0] > self.max_seconds:
            restart_times.popleft()
        if len(restart_times) > self.max_restarts:
            await self._emit(
                "child:restart_exceeded",
                spec_id=spec_id,
                restart_count=len(restart_times),
            )
            raise Failed("Max restart intensity reached")
        await self._emit(
            "child:restarted",
            spec_id=spec_id,
            restart_count=len(restart_times),
        )

    def _should_restart(self, reason: t.Any, spec: ChildSpec) -> bool:
        match spec.restart:
            case "permanent":
                return True
            case "temporary":
                return False
            case "transient":
                return not is_normal_shutdown_reason(reason)
            case _:
                raise ValueError(f"Invalid restart {spec.restart}")

    async def _shutdown_child(
        self,
        proc: Process,
        shutdown: ShutdownType,
        reason: t.Any,
    ):
        if proc.has_stopped():
            return

        spec_id = self._spec_id_for(proc)

        if shutdown == "brutal_kill":
            await proc.kill()
            await self._emit(
                "child:terminated", child=proc, spec_id=spec_id, reason="killed"
            )
            return

        timeout = None if shutdown == "infinity" else shutdown
        try:
            await proc.stop(reason, timeout=timeout, sender=self)
        except TimeoutError:
            await proc.kill()
        except BrokenResourceError:
            await proc.stopped()
        await self._emit("child:terminated", child=proc, spec_id=spec_id, reason=reason)

    def _spec_id_for(self, proc: Process) -> str | None:
        for child_id, running in self.children.items():
            if running.process is proc:
                return child_id
        return None

    async def _start_child_process(self, spec: ChildSpec) -> Process:
        func, args, kwargs = spec.start

        if isinstance(func, type) and issubclass(func, Process):
            proc = await func.start_link(*args, supervisor=self, **kwargs)
        elif iscoroutinefunction(func):
            proc = await func(*args, **kwargs)
        else:
            assert False, f"ChildSpec {spec.id}: start[0] not callable {func}"

        await self._emit("child:started", child=proc, spec_id=spec.id)
        return proc

    async def _run_child(self, child_id: str, spec: ChildSpec, ready: Event):
        restart_times: deque[float] = deque()

        while True:
            if restart_times and telemetry.is_enabled():
                attrs: dict[str, t.Any] = {
                    telemetry.ATTR_SUPERVISOR_NAME: self.id,
                    telemetry.ATTR_CHILD_ID: child_id,
                    telemetry.ATTR_RESTART_COUNT: len(restart_times),
                    telemetry.ATTR_RESTART_STRATEGY: self.strategy,
                }
                with telemetry.get_tracer().start_as_current_span(
                    "fastactor.supervisor.restart_child",
                    attributes=attrs,
                ) as span:
                    try:
                        proc = await self._start_child_process(spec)
                    except BaseException as err:
                        telemetry.record_exception(span, err)
                        raise
            else:
                proc = await self._start_child_process(spec)
            self.children[child_id] = RunningChild(proc, spec)
            ready.set()

            await proc.stopped()
            reason = proc._crash_exc or "normal"
            self.children.pop(child_id, None)

            if self._terminating:
                break

            if not self._should_restart(reason, spec):
                break

            try:
                await self._record_restart(restart_times, spec_id=child_id)
            except Failed as error:
                self._fail_supervisor(error)
                break

    async def _supervise_once(
        self,
        child_id: str,
        spec: ChildSpec,
        ready: Event,
        stopped_event: Event,
    ):
        try:
            proc = await self._start_child_process(spec)
        except Exception:
            ready.set()
            stopped_event.set()
            raise

        self.children[child_id] = RunningChild(proc, spec)
        ready.set()

        try:
            await proc.stopped()
        finally:
            stopped_event.set()

    async def _one_for_one(self):
        ready_events: list[Event] = []
        for child_id, spec in self.child_specs.items():
            ready = Event()
            assert self._task_group is not None
            self._task_group.start_soon(self._run_child, child_id, spec, ready)
            ready_events.append(ready)

        for ready in ready_events:
            await ready.wait()

    def _ordered_child_specs(self) -> list[tuple[str, ChildSpec]]:
        return list(self.child_specs.items())

    def _ordered_running_ids(self) -> list[str]:
        return [
            child_id
            for child_id, _ in self._ordered_child_specs()
            if child_id in self._strategy_stopped_events
        ]

    async def _signal_strategy_update(self):
        if self._strategy_updates_send is None:
            return
        await self._strategy_updates_send.send(None)

    def _fail_supervisor(self, reason: t.Any):
        if self._inbox is None or self.has_stopped():
            return

        try:
            self.send_nowait(Stop(self, reason))
        except BrokenResourceError:
            pass

    async def _wait_for_first_stop(
        self,
        stopped_events: dict[str, Event],
    ) -> str | None:
        winner: str | None = None
        done = Event()

        async def wait_for_stop(child_id: str, event: Event):
            nonlocal winner
            await event.wait()
            if winner is None:
                winner = child_id
                done.set()

        async def wait_for_update():
            if self._strategy_updates_receive is None:
                return
            await self._strategy_updates_receive.receive()
            done.set()

        async with create_task_group() as task_group:
            task_group.start_soon(wait_for_update)
            for child_id, event in stopped_events.items():
                task_group.start_soon(wait_for_stop, child_id, event)
            await done.wait()
            task_group.cancel_scope.cancel()

        return winner

    async def _start_strategy_children(
        self,
        ordered_specs: list[tuple[str, ChildSpec]],
    ):
        ready_events: list[Event] = []
        for child_id, spec in ordered_specs:
            ready = Event()
            stopped = Event()
            self._strategy_stopped_events[child_id] = stopped
            assert self._task_group is not None
            self._task_group.start_soon(
                self._supervise_once,
                child_id,
                spec,
                ready,
                stopped,
            )
            ready_events.append(ready)

        for ready in ready_events:
            await ready.wait()

    async def _restart_strategy_children(
        self,
        ordered_ids: list[str],
        reason: t.Any,
        *,
        restart: bool = True,
    ):
        for child_id in reversed(ordered_ids):
            running_child = self.children.get(child_id)
            if running_child is None:
                continue

            await self._shutdown_child(
                running_child.process,
                running_child.spec.shutdown,
                reason,
            )

        for child_id in ordered_ids:
            stopped_event = self._strategy_stopped_events.get(child_id)
            if stopped_event is not None:
                await stopped_event.wait()

        for child_id in ordered_ids:
            self.children.pop(child_id, None)
            self._strategy_stopped_events.pop(child_id, None)

        if restart:
            await self._start_strategy_children(
                [
                    (child_id, spec)
                    for child_id, spec in self._ordered_child_specs()
                    if child_id in ordered_ids
                ]
            )

    async def _one_for_all(self):
        restart_times: deque[float] = deque()
        while True:
            ordered_ids = self._ordered_running_ids()
            if not ordered_ids:
                if self._strategy_updates_receive is None:
                    return
                await self._strategy_updates_receive.receive()
                continue

            child_id = await self._wait_for_first_stop(
                {
                    running_id: self._strategy_stopped_events[running_id]
                    for running_id in ordered_ids
                }
            )
            if child_id is None:
                continue

            ordered_ids = self._ordered_running_ids()
            if child_id not in ordered_ids:
                continue

            running_child = self.children.get(child_id)
            if running_child is None:
                self._strategy_stopped_events.pop(child_id, None)
                continue

            reason = running_child.process._crash_exc or "normal"
            if self._terminating:
                self.children.pop(child_id, None)
                self._strategy_stopped_events.pop(child_id, None)
                continue

            if not self._should_restart(reason, running_child.spec):
                await self._restart_strategy_children(
                    ordered_ids,
                    reason="shutdown",
                    restart=False,
                )
                continue

            try:
                await self._record_restart(restart_times, spec_id=child_id)
            except Failed as error:
                self._fail_supervisor(error)
                return

            await self._restart_strategy_children(
                ordered_ids,
                reason="one_for_all_restart",
            )

    async def _rest_for_one(self):
        restart_times: deque[float] = deque()

        while True:
            ordered_specs = self._ordered_child_specs()
            ordered_ids = [
                child_id
                for child_id, _ in ordered_specs
                if child_id in self._strategy_stopped_events
            ]
            if not ordered_ids:
                if self._strategy_updates_receive is None:
                    return
                await self._strategy_updates_receive.receive()
                continue

            child_id = await self._wait_for_first_stop(
                {
                    running_id: self._strategy_stopped_events[running_id]
                    for running_id in ordered_ids
                }
            )
            if child_id is None:
                continue

            ordered_specs = self._ordered_child_specs()
            ordered_ids = [
                running_id
                for running_id, _ in ordered_specs
                if running_id in self._strategy_stopped_events
            ]
            if child_id not in ordered_ids:
                continue

            idx = ordered_ids.index(child_id)
            spec = self.child_specs[child_id]
            running_child = self.children.get(child_id)
            reason = "normal"
            if running_child is not None:
                reason = running_child.process._crash_exc or "normal"

            if self._terminating:
                self.children.pop(child_id, None)
                self._strategy_stopped_events.pop(child_id, None)
                continue

            if not self._should_restart(reason, spec):
                self.children.pop(child_id, None)
                self._strategy_stopped_events.pop(child_id, None)
                continue

            try:
                await self._record_restart(restart_times, spec_id=child_id)
            except Failed as error:
                self._fail_supervisor(error)
                return

            for tail_id in reversed(ordered_ids[idx + 1 :]):
                tail_child = self.children.get(tail_id)
                if tail_child is None:
                    continue

                await self._shutdown_child(
                    tail_child.process,
                    tail_child.spec.shutdown,
                    "rest_for_one_restart",
                )

            await self._restart_strategy_children(
                ordered_ids[idx:],
                reason="rest_for_one_restart",
            )

    @staticmethod
    def _unwrap_exception_group(error: Exception) -> Exception:
        current: BaseException = error
        while isinstance(current, BaseExceptionGroup) and len(current.exceptions) == 1:
            current = current.exceptions[0]
        return current if isinstance(current, Exception) else error

    async def loop(self, *args, **kwargs):
        if not await self._run_init_safe(*args, **kwargs):
            return

        try:
            async with create_task_group() as task_group:
                self._task_group = task_group

                if self.strategy == "one_for_one":
                    await self._one_for_one()
                elif self.strategy == "one_for_all":
                    (
                        self._strategy_updates_send,
                        self._strategy_updates_receive,
                    ) = create_memory_object_stream(100)
                    await self._start_strategy_children(self._ordered_child_specs())
                    task_group.start_soon(self._one_for_all)
                elif self.strategy == "rest_for_one":
                    (
                        self._strategy_updates_send,
                        self._strategy_updates_receive,
                    ) = create_memory_object_stream(100)
                    await self._start_strategy_children(self._ordered_child_specs())
                    task_group.start_soon(self._rest_for_one)
                else:
                    raise Failed(f"Unsupported strategy {self.strategy}")

                try:
                    await super()._loop()
                finally:
                    task_group.cancel_scope.cancel()
        except Exception as error:
            unwrapped = self._unwrap_exception_group(error)
            if self.has_stopped():
                if self._crash_exc is None:
                    self._crash_exc = unwrapped
                return

            self._crash_exc = unwrapped
            try:
                await self.terminate(unwrapped)
            finally:
                self._stopped.set()
                self._started.set()
        finally:
            self._task_group = None

    def which_children(self) -> list[t.Any]:
        results = []
        for child_id, spec in self.child_specs.items():
            child_proc: Process | str = ":undefined"
            if child_id in self.children:
                child_proc = self.children[child_id].process
            results.append(
                {
                    "id": spec.id,
                    "process": child_proc,
                    "restart": spec.restart,
                    "shutdown": spec.shutdown,
                    "type": spec.type,
                    "significant": False,
                }
            )
        return results

    def count_children(self) -> dict[str, int]:
        """Return counts mirroring Elixir's Supervisor.count_children/1.

        Keys: "specs" (total specs registered), "active" (currently running),
        "workers" (specs with type='worker'), "supervisors" (specs with type='supervisor').
        """
        workers = 0
        supervisors = 0
        for spec in self.child_specs.values():
            if spec.type == "supervisor":
                supervisors += 1
            else:
                workers += 1
        return {
            "specs": len(self.child_specs),
            "active": len(self.children),
            "workers": workers,
            "supervisors": supervisors,
        }

    @t.overload
    async def start_child[P: Process](self, spec: ChildSpec[P]) -> P: ...
    @t.overload
    async def start_child[P: Process](self, spec: tuple[type[P], dict]) -> P: ...
    @t.overload
    async def start_child[P: Process](
        self, spec: tuple[t.Callable[..., t.Awaitable[P]], dict]
    ) -> P: ...
    async def start_child(self, spec: "ChildSpec | tuple") -> Process:
        spec = _normalize_child_spec(spec)
        child_id = spec.id
        if child_id in self.child_specs:
            raise Failed(f"Child with id={child_id} already exists.")
        if self._task_group is None:
            raise Failed("Supervisor not running.")

        self.child_specs[child_id] = spec

        ready_event = Event()
        if self.strategy == "one_for_one":
            self._task_group.start_soon(self._run_child, child_id, spec, ready_event)
        else:
            await self._start_strategy_children([(child_id, spec)])
            await self._signal_strategy_update()
            ready_event.set()

        await ready_event.wait()

        if child_id not in self.children:
            raise Failed(f"Child {child_id} failed to start.")

        return self.children[child_id].process

    async def terminate_child(self, child_id: str):
        if child_id not in self.child_specs:
            raise Failed(f"No such child: {child_id}")
        if child_id not in self.children:
            raise Failed(f"Child {child_id} is not running")

        running_child = self.children[child_id]
        if self.strategy != "one_for_one":
            self.children.pop(child_id, None)
            self._strategy_stopped_events.pop(child_id, None)
            await self._signal_strategy_update()
        await self._shutdown_child(
            running_child.process,
            running_child.spec.shutdown,
            reason="normal",
        )
        if self.strategy == "one_for_one":
            self.children.pop(child_id, None)

    def delete_child(self, child_id: str):
        if child_id not in self.child_specs:
            raise Failed(f"No such child: {child_id}")
        if child_id in self.children:
            raise Failed(f"Child {child_id} is still running, terminate first")
        del self.child_specs[child_id]

    async def restart_child(self, child_id: str):
        if child_id not in self.child_specs:
            raise Failed(f"No such child: {child_id}")
        if (
            child_id in self.children
            and not self.children[child_id].process.has_stopped()
        ):
            raise Failed(f"Child {child_id} is running, cannot restart yet")
        if self._task_group is None:
            raise Failed("Supervisor not running")

        ready = Event()
        spec = self.child_specs[child_id]
        if self.strategy == "one_for_one":
            self._task_group.start_soon(self._run_child, child_id, spec, ready)
        else:
            self.children.pop(child_id, None)
            self._strategy_stopped_events.pop(child_id, None)
            await self._start_strategy_children([(child_id, spec)])
            await self._signal_strategy_update()
            ready.set()

        await ready.wait()
        return self.children[child_id].process

    def _check_task_group(self):
        if self._task_group is None:
            raise Failed("Supervisor not running")

    @staticmethod
    def child_spec[P: Process](
        child_id: str,
        func_or_class: type[P] | t.Callable[..., t.Awaitable[P]],
        args: tuple | None = None,
        kwargs: dict | None = None,
        restart: RestartType = "permanent",
        shutdown: ShutdownType | None = None,
        type: t.Literal["worker", "supervisor"] = "worker",
    ) -> ChildSpec[P]:
        if not child_id:
            raise ValueError("Child spec must have an id")
        if shutdown is None:
            shutdown = "infinity" if type == "supervisor" else settings.call_timeout
        return _build_child_spec(
            child_id, func_or_class, args, kwargs, restart, shutdown, type
        )
