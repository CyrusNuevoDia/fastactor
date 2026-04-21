from dataclasses import dataclass, field
import typing as t

from anyio import CancelScope, Event, fail_after
from anyio.lowlevel import checkpoint

from ..utils import id_generator
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
            self._stopped.set()

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

    async def yield_(self, timeout: float | None = None) -> R | Exception | None:
        """Poll for the task's result with a timeout, without raising on crash.

        Returns the result on success, the exception object on crash, or None on timeout.
        """
        from anyio import move_on_after

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
            return await Process.start.__func__(
                cls,
                *args,
                trap_exits=trap_exits,
                supervisor=None,
                name=name,
                **kwargs,
            )

        async def starter(*child_args, **child_kwargs) -> t.Self:
            return await Process.start.__func__(
                cls,
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

    async def async_(self, fn, *args, **kwargs) -> Task:
        """Spawn an async function as a supervised Task. Mirrors Elixir's Task.Supervisor.async/2.

        The trailing underscore in the name is because `async` is a reserved Python keyword.
        """
        started = Event()

        async def runner():
            await started.wait()
            return await fn(*args, **kwargs)

        spec: ChildSpec[Task] = ChildSpec(
            id="",
            start=(Task, (runner,), {}),
            restart="temporary",
        )
        task = t.cast(Task, await self.start_child(spec))
        started.set()
        return task

    async def run(self, fn, *args, **kwargs) -> Task:
        """Backward-compatible alias for `async_`."""
        return await self.async_(fn, *args, **kwargs)
