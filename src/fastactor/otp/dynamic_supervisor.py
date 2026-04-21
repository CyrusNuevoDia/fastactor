from dataclasses import replace
from inspect import iscoroutinefunction
import typing as t

from anyio import create_task_group

from ..utils import id_generator
from ._exceptions import Crashed, Failed, Shutdown
from .process import Process
from .supervisor import (
    ChildSpec,
    RestartStrategy,
    RestartType,
    ShutdownType,
    Supervisor,
    _normalize_child_spec,
)

_dynamic_child_id = id_generator("dyn")


class DynamicSupervisor(Supervisor):
    __eq__ = Process.__eq__
    __hash__ = Process.__hash__

    max_children: int | float = float("inf")
    extra_arguments: tuple = ()

    @classmethod
    async def start(
        cls,
        *args,
        max_children: int | float = float("inf"),
        extra_arguments: tuple = (),
        trap_exits: bool = True,
        supervisor: "Supervisor | None" = None,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        **kwargs,
    ) -> t.Self:
        from .runtime import Runtime

        runtime = Runtime.current()
        supervisor = supervisor or runtime.supervisor

        process = cls(supervisor=supervisor, trap_exits=trap_exits)
        process.max_children = max_children
        process.extra_arguments = extra_arguments
        return await runtime.spawn(process, *args, name=name, via=via, **kwargs)

    @classmethod
    async def start_link(
        cls,
        *args,
        max_children: int | float = float("inf"),
        extra_arguments: tuple = (),
        trap_exits: bool = True,
        supervisor: "Supervisor | None" = None,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        **kwargs,
    ) -> t.Self:
        from .runtime import Runtime

        runtime = Runtime.current()
        supervisor = supervisor or runtime.supervisor

        process = cls(supervisor=supervisor, trap_exits=trap_exits)
        process.max_children = max_children
        process.extra_arguments = extra_arguments
        if supervisor is not None:
            process.link(supervisor)
        return await runtime.spawn(process, *args, name=name, via=via, **kwargs)

    async def init(
        self,
        strategy: RestartStrategy = "one_for_one",
        max_restarts: int = 3,
        max_seconds: float = 5.0,
        children: list | None = None,
    ):
        if strategy != "one_for_one":
            raise ValueError(
                f"DynamicSupervisor only supports one_for_one, got {strategy}"
            )

        await super().init(
            strategy=strategy,
            max_restarts=max_restarts,
            max_seconds=max_seconds,
            children=children,
        )

    async def loop(self, *args, **kwargs):
        try:
            await self._init(*args, **kwargs)
        except (Crashed, Shutdown) as error:
            self._crash_exc = error
            self._started.set()
            self._stopped.set()
            return
        async with create_task_group() as task_group:
            self._task_group = task_group
            await super()._loop()

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
        if len(self.children) >= self.max_children:
            raise Failed(f"max_children={self.max_children} reached")

        normalized_spec = spec
        if not normalized_spec.id:
            normalized_spec = replace(normalized_spec, id=_dynamic_child_id())

        if self.extra_arguments:
            func, args, kwargs = normalized_spec.start
            normalized_spec = replace(
                normalized_spec,
                start=(func, (*self.extra_arguments, *args), dict(kwargs)),
            )

        return await super().start_child(normalized_spec)

    @staticmethod
    def child_spec[P: Process](
        child_id: str | None,
        func_or_class: type[P] | t.Callable[..., t.Awaitable[P]],
        args: tuple | None = None,
        kwargs: dict | None = None,
        restart: RestartType = "temporary",
        shutdown: ShutdownType = 5,
        type: t.Literal["worker", "supervisor"] = "worker",
        modules: list[t.Any] | None = None,
        significant: bool = False,
    ) -> ChildSpec[P]:
        modules = modules or []
        args = args or tuple()
        kwargs = kwargs or {}

        if not hasattr(func_or_class, "start_link") and not iscoroutinefunction(
            func_or_class
        ):
            raise ValueError("Child must be a coroutine or have a start_link method")

        return ChildSpec(
            id=child_id or "",
            start=(func_or_class, args, kwargs),
            restart=restart,
            shutdown=shutdown,
            type=type,
            modules=modules,
            significant=significant,
        )
