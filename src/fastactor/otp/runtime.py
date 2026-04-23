"""Runtime — owns the anyio task group, root `Supervisor`, and registries.

Singleton: one Runtime per process. Entry points: `async with Runtime(): ...`,
`fastactor.run(main)`, or `await Runtime.start() / await rt.stop()`. Also hosts
the global-name map, `Registry` storage, and `whereis()` lookup. `Runtime`
keeps a bounded crash log (`CrashRecord`, `recent_crashes`, `crash_counts`).
See `src/fastactor/otp/README.md` (§Named registration, §Supervisor root).
"""

import logging
import signal
import typing as t
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from time import monotonic

from anyio import CancelScope, Lock, create_task_group, open_signal_receiver
from anyio.abc import TaskGroup
from pyee import EventEmitter

from fastactor.settings import settings
from fastactor.utils import emit_awaited

from ._exceptions import Failed, is_normal_shutdown_reason
from .process import Process
from .supervisor import Supervisor

logger = logging.getLogger(__name__)

current_process: ContextVar[Process | None] = ContextVar(
    "current_process",
    default=None,
)


@dataclass
class _RegistryEntry:
    mode: t.Literal["unique", "duplicate"]
    unique: dict[t.Any, str] = field(default_factory=dict)
    duplicate: defaultdict[t.Any, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )


@dataclass(frozen=True)
class CrashRecord:
    process_id: str
    reason_class: str
    reason_repr: str
    at: float


_CRASH_LOG_MAX = 1000


@dataclass(repr=False)
class Runtime:
    _current: t.ClassVar["Runtime | None"] = None
    _lock: t.ClassVar[Lock] = Lock()

    supervisor: Supervisor | None = None
    _task_group: TaskGroup | None = None
    trap_signals: bool = True
    telemetry: bool = False
    _telemetry_uninstrument: t.Callable[[], None] | None = field(
        default=None, init=False
    )

    registry: dict[str, str] = field(default_factory=dict, init=False)
    _reverse_registry: dict[str, str] = field(default_factory=dict, init=False)
    processes: dict[str, Process] = field(default_factory=dict, init=False)
    registries: dict[str, _RegistryEntry] = field(default_factory=dict, init=False)
    _registry_lock: Lock = field(default_factory=Lock, init=False)
    _proc_keys: defaultdict[str, set[tuple[str, t.Any]]] = field(
        default_factory=lambda: defaultdict(set),
        init=False,
    )
    _crash_log: deque[CrashRecord] = field(
        default_factory=lambda: deque(maxlen=_CRASH_LOG_MAX), init=False
    )
    _crash_counts: defaultdict[str, int] = field(
        default_factory=lambda: defaultdict(int), init=False
    )

    emitter: EventEmitter = field(default_factory=EventEmitter, init=False)

    @classmethod
    def current(cls) -> "Runtime":
        if cls._current is None:
            raise RuntimeError("No Runtime is currently active.")
        return cls._current

    async def __aenter__(self):
        async with self._lock:
            if Runtime._current is not None:
                raise RuntimeError("Runtime already started")

            self._task_group = await create_task_group().__aenter__()
            self.supervisor = Supervisor(trap_exits=True)
            Runtime._current = self

            try:
                await self.spawn(self.supervisor)
            except Exception as error:
                try:
                    await self._task_group.__aexit__(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                finally:
                    Runtime._current = None
                    self.supervisor = None
                    self._task_group = None
                raise

            if self.trap_signals:
                self._task_group.start_soon(self._trap_signals)

            if self.telemetry or settings.telemetry_enabled:
                from fastactor import telemetry

                self._telemetry_uninstrument = telemetry.instrument(self)

            self.emitter.on("crashed", self._record_crash)
            await emit_awaited(self.emitter, "runtime:started", self)
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        task_group = self._task_group
        supervisor = self.supervisor

        with CancelScope(shield=True):
            await self._lock.acquire()

        try:
            with CancelScope(shield=True):
                if supervisor is not None:
                    if supervisor.has_started() and not supervisor.has_stopped():
                        try:
                            await supervisor.stop()
                        except Exception:
                            logger.exception(
                                "Runtime: supervisor failed to stop cleanly"
                            )
                if task_group is not None:
                    task_group.cancel_scope.cancel()

            try:
                if task_group is not None:
                    try:
                        await task_group.__aexit__(exc_type, exc_val, exc_tb)
                    except BaseExceptionGroup as eg:
                        if len(eg.exceptions) == 1:
                            raise eg.exceptions[0] from None
                        raise
            finally:
                if self._telemetry_uninstrument is not None:
                    try:
                        self._telemetry_uninstrument()
                    except Exception:
                        logger.exception("Runtime: telemetry uninstrument failed")
                    self._telemetry_uninstrument = None
                Runtime._current = None
                self.supervisor = None
                self._task_group = None
                await emit_awaited(self.emitter, "runtime:stopped", self)
        finally:
            self._lock.release()

    @classmethod
    async def start(cls, *, trap_signals: bool = True) -> "Runtime":
        rt = cls(trap_signals=trap_signals)
        await rt.__aenter__()
        return rt

    async def stop(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: t.Any = None,
    ) -> None:
        await self.__aexit__(exc_type, exc_val, exc_tb)

    async def _trap_signals(self) -> None:
        try:
            with open_signal_receiver(signal.SIGINT, signal.SIGTERM) as sigs:
                async for sig in sigs:
                    logger.info("Runtime: received signal %s, shutting down", sig)
                    if self._task_group is not None:
                        self._task_group.cancel_scope.cancel()
                    break
        except NotImplementedError:
            logger.warning("Runtime: signal traps not supported on this platform")

    async def _record_crash(
        self,
        proc: Process,
        *,
        exc: Exception | None,
        reason: t.Any,
    ) -> None:
        if proc is self.supervisor:
            return  # don't self-record on our own shutdown
        if is_normal_shutdown_reason(reason):
            return

        reason_class = type(reason).__name__
        record = CrashRecord(
            process_id=proc.id,
            reason_class=reason_class,
            reason_repr=repr(reason),
            at=monotonic(),
        )
        self._crash_log.append(record)
        self._crash_counts[reason_class] += 1

        await emit_awaited(
            self.emitter,
            "runtime:child_crashed",
            process_id=proc.id,
            reason=reason,
            record=record,
        )

    def recent_crashes(self, n: int = 50) -> list[CrashRecord]:
        if n <= 0:
            return []
        return list(self._crash_log)[-n:]

    def crash_counts(self) -> dict[str, int]:
        return dict(self._crash_counts)

    def total_crashes(self) -> int:
        return sum(self._crash_counts.values())

    async def spawn[P: Process](
        self,
        process: P,
        *args,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        **kwargs,
    ) -> P:
        from fastactor import telemetry

        if not telemetry.is_enabled():
            return await self._spawn_impl(process, *args, name=name, via=via, **kwargs)

        attrs: dict[str, t.Any] = {
            telemetry.ATTR_PROCESS_ID: process.id,
            telemetry.ATTR_PROCESS_CLASS: type(process).__name__,
        }
        parent = current_process.get()
        if parent is not None:
            attrs[telemetry.ATTR_PARENT_ID] = parent.id

        with telemetry.get_tracer().start_as_current_span(
            "fastactor.runtime.spawn",
            attributes=attrs,
        ) as span:
            try:
                return await self._spawn_impl(
                    process, *args, name=name, via=via, **kwargs
                )
            except BaseException as err:
                telemetry.record_exception(span, err)
                raise

    async def _spawn_impl[P: Process](
        self,
        process: P,
        *args,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        **kwargs,
    ) -> P:
        if self._task_group is None:
            raise RuntimeError("Runtime is not running")
        if name is not None and via is not None:
            raise ValueError("Cannot specify both name= and via= when spawning")
        if name is not None and name in self.registry:
            existing_proc = self.processes.get(self.registry[name])
            raise Failed(("already_started", existing_proc))

        self._task_group.start_soon(partial(process.loop, **kwargs), *args)
        await process.started()

        if process.has_stopped():
            if process._ignored:
                return "ignore"  # type: ignore[return-value]  # ty: ignore[invalid-return-type]
            exc = process._crash_exc
            if exc is not None:
                raise exc.__cause__ if exc.__cause__ is not None else exc
            raise RuntimeError("Process crashed before it could start")

        self.register(process)
        if name is not None:
            self.register_name(name, process)
        if via is not None:
            from .registry import Registry

            registry_name, key = via
            await Registry.register(registry_name, key, process)
        await emit_awaited(self.emitter, "process:spawned", process)
        return process

    def register(self, proc: Process):
        self.processes[proc.id] = proc

    async def unregister(self, proc: Process):
        removed = self.processes.pop(proc.id, None)

        if name := self._reverse_registry.get(proc.id):
            self.unregister_name(name)

        for registry_name, key in self._proc_keys.pop(proc.id, set()):
            entry = self.registries.get(registry_name)
            if entry is None:
                continue

            if entry.mode == "unique":
                if entry.unique.get(key) == proc.id:
                    entry.unique.pop(key, None)
                continue

            proc_ids = entry.duplicate.get(key)
            if not proc_ids:
                continue

            proc_ids.discard(proc.id)
            if not proc_ids:
                entry.duplicate.pop(key, None)

        if removed is not None:
            await emit_awaited(self.emitter, "process:unregistered", proc)

    def register_name(self, name: str, proc: Process):
        if existing_name := self._reverse_registry.get(proc.id):
            if existing_name != name:
                self.registry.pop(existing_name, None)

        if existing_proc_id := self.registry.get(name):
            if existing_proc_id != proc.id:
                self._reverse_registry.pop(existing_proc_id, None)

        self.registry[name] = proc.id
        self._reverse_registry[proc.id] = name

    def unregister_name(self, name: str):
        proc_id = self.registry.pop(name, None)
        if proc_id is None:
            return
        self._reverse_registry.pop(proc_id, None)

    async def whereis(
        self,
        name_or_via: "str | tuple[str, t.Any]",
    ) -> Process | None:
        if isinstance(name_or_via, tuple):
            from .registry import Registry

            registry_name, key = name_or_via
            procs = await Registry.lookup(registry_name, key)
            return procs[0] if procs else None
        if proc_id := self.registry.get(name_or_via):
            return self.processes.get(proc_id)
        return None


async def whereis(name_or_via: "str | tuple[str, t.Any]") -> Process | None:
    """Package-level convenience: looks up a process by string name or by (registry, key)."""
    return await Runtime.current().whereis(name_or_via)
