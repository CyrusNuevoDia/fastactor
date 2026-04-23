"""Task + TaskSupervisor — one-shot awaitable coroutines, linked or unlinked.

`Task.start(fn)` = Elixir's `Task.async_nolink` (caller survives a crash).
`Task.start_link(fn)` = Elixir's `Task.async` (crash cascades via link).
`TaskSupervisor.run(fn, ...)` spawns pooled tasks. See
`src/fastactor/otp/README.md#task--tasksupervisor`.
"""

import typing as t
from dataclasses import dataclass, field

from anyio import CancelScope, Event, fail_after, move_on_after
from anyio.lowlevel import checkpoint

from fastactor.utils import id_generator

from .dynamic_supervisor import DynamicSupervisor
from .process import Process
from .supervisor import ChildSpec

_task_supervisor_id = id_generator("task-supervisor")


@dataclass(repr=False, eq=False)
class Task[R](Process):
    _fn: t.Callable[..., t.Awaitable[R]] | None = field(default=None, init=False)
    _args: tuple[t.Any, ...] = field(default_factory=tuple, init=False)
    _kwargs: dict[str, t.Any] | None = field(default=None, init=False)
    _result: R | Exception | None = field(default=None, init=False)
    _done: Event = field(default_factory=Event, init=False)
    _start_ack: Event = field(default_factory=Event, init=False)
    _run_scope: CancelScope | None = field(default=None, init=False)

    async def init(self, fn, *args, **kwargs):
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    async def _loop(self):
        reason: t.Any = "normal"
        self._started.set()
        await self._start_ack.wait()

        try:
            assert self._fn is not None
            with CancelScope() as scope:
                self._run_scope = scope
                self._result = await self._fn(*self._args, **(self._kwargs or {}))
            if scope.cancelled_caught:
                reason = "shutdown"
        except Exception as error:
            self._result = error
            self._crash_exc = error
            reason = error
        finally:
            self._run_scope = None
            self._done.set()
            await self.terminate(reason)
            try:
                await self._system_teardown(reason)
            finally:
                self._stopped.set()
                self._started.set()

    def __await__(self):
        return self._resolve().__await__()

    async def started(self) -> t.Self:
        await super().started()
        self._start_ack.set()
        return self

    async def _resolve(self) -> R:
        await self._done.wait()
        if isinstance(self._result, Exception):
            raise self._result
        return t.cast(R, self._result)

    async def poll(self, timeout: float | None = None) -> R | Exception | None:
        """Poll for the task's result with a timeout, without raising on crash.

        Returns the result on success, the exception object on crash, or None on timeout.
        """
        with move_on_after(timeout):
            await self._done.wait()

        if not self._done.is_set():
            return None
        return self._result

    async def shutdown(self, timeout: float = 5) -> None:
        """Stop the task with reason 'shutdown'. If it doesn't stop in time, kill it."""
        if self.has_stopped():
            return
        try:
            await self.stop("shutdown", timeout=timeout)
        except TimeoutError:
            await self.kill()

    async def stop(
        self,
        reason: t.Any = "normal",
        timeout: float | None = 60,
        sender: Process | None = None,
    ):
        if self.has_stopped():
            return

        with fail_after(timeout):
            while self._run_scope is None and not self.has_stopped():
                await checkpoint()

            if self.has_stopped() or self._run_scope is None:
                return

            self._run_scope.cancel()
            await self.stopped()

    async def kill(self):
        if self.has_stopped():
            return

        while self._run_scope is None and not self.has_stopped():
            await checkpoint()

        if self.has_stopped() or self._run_scope is None:
            return

        self._run_scope.cancel()
        await self.stopped()

    @classmethod
    async def start[T](  # type: ignore[override]
        cls,
        fn: t.Callable[..., t.Awaitable[T]],
        *args,
        **kwargs,
    ) -> "Task[T]":
        return t.cast("Task[T]", await cls._spawn(fn, *args, link=False, **kwargs))

    @classmethod
    async def start_link[T](  # type: ignore[override]
        cls,
        fn: t.Callable[..., t.Awaitable[T]],
        *args,
        **kwargs,
    ) -> "Task[T]":
        return t.cast("Task[T]", await cls._spawn(fn, *args, link=True, **kwargs))


@dataclass(repr=False, eq=False)
class TaskSupervisor(DynamicSupervisor):
    @classmethod
    async def start(
        cls,
        *args,
        trap_exits: bool = True,
        supervisor=None,
        name: str | None = None,
        **kwargs,
    ) -> t.Self:
        from .runtime import Runtime

        runtime = Runtime.current()
        supervisor = supervisor or runtime.supervisor

        if supervisor is None:
            return await super().start(
                *args, trap_exits=trap_exits, supervisor=None, name=name, **kwargs
            )

        async def starter(*child_args, **child_kwargs) -> t.Self:
            return await super(TaskSupervisor, cls).start(
                *child_args,
                trap_exits=trap_exits,
                supervisor=supervisor,
                name=name,
                **child_kwargs,
            )

        spec: ChildSpec[t.Self] = ChildSpec(
            id=name or _task_supervisor_id(),
            start=(starter, args, dict(kwargs)),
            restart="temporary",
            type="supervisor",
        )
        return t.cast(t.Self, await supervisor.start_child(spec))

    @classmethod
    async def start_link(
        cls,
        *args,
        trap_exits: bool = True,
        supervisor=None,
        name: str | None = None,
        **kwargs,
    ) -> t.Self:
        return await cls.start(
            *args,
            trap_exits=trap_exits,
            supervisor=supervisor,
            name=name,
            **kwargs,
        )

    async def run[R](
        self,
        fn: t.Callable[..., t.Awaitable[R]],
        *args,
        **kwargs,
    ) -> Task[R]:
        """Spawn an async function as a supervised Task. Mirrors Elixir's Task.Supervisor.async/2."""
        started = Event()

        async def runner() -> R:
            await started.wait()
            return await fn(*args, **kwargs)

        spec: ChildSpec[Task[R]] = ChildSpec(
            id="",
            start=(Task, (runner,), {}),
            restart="temporary",
        )
        task = t.cast("Task[R]", await self.start_child(spec))
        started.set()
        return task
