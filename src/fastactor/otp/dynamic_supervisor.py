"""DynamicSupervisor — `one_for_one`-only supervisor with runtime-added children.

Supports `max_children` cap and `extra_arguments` prepended to every child's start
tuple. Child ids may be `None` (auto-generates `"dyn:<ksuid>"`). Default restart is
`"temporary"`, not `"permanent"`. See `src/fastactor/otp/README.md#dynamicsupervisor`.
"""

import typing as t
from dataclasses import replace

from anyio import create_task_group

from fastactor.settings import settings
from fastactor.utils import id_generator

from ._exceptions import Failed
from .process import Process
from .supervisor import (
    ChildSpec,
    RestartStrategy,
    RestartType,
    ShutdownType,
    Supervisor,
    _build_child_spec,
    _normalize_child_spec,
)

_dynamic_child_id = id_generator("dyn")


class DynamicSupervisor(Supervisor):
    # Supervisor uses @dataclass(eq=False) so equality/hashing must be re-established explicitly.
    __eq__ = Process.__eq__
    __hash__ = Process.__hash__

    max_children: int | float = float("inf")
    extra_arguments: tuple = ()

    @classmethod
    def _configure_before_spawn(cls, process: t.Any, kwargs):
        if "max_children" in kwargs:
            process.max_children = kwargs.pop("max_children")
        if "extra_arguments" in kwargs:
            process.extra_arguments = kwargs.pop("extra_arguments")

    async def init(
        self,
        strategy: RestartStrategy = "one_for_one",
        max_restarts: int = settings.supervisor_max_restarts,
        max_seconds: float = settings.supervisor_max_seconds,
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
        if not await self._run_init_safe(*args, **kwargs):
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
        shutdown: ShutdownType | None = None,
        type: t.Literal["worker", "supervisor"] = "worker",
    ) -> ChildSpec[P]:
        if shutdown is None:
            shutdown = "infinity" if type == "supervisor" else settings.call_timeout
        return _build_child_spec(
            child_id or "", func_or_class, args, kwargs, restart, shutdown, type
        )
