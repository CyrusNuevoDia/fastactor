import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from .process import Process
from .runtime import Runtime, _RegistryEntry

logger = logging.getLogger(__name__)


class AlreadyRegistered(Exception):
    def __init__(self, pid: Process):
        self.pid = pid
        super().__init__(f"already registered: {pid}")


class Registry:
    @classmethod
    async def new(cls, name: str, mode: Literal["unique", "duplicate"]) -> None:
        if mode not in ("unique", "duplicate"):
            raise ValueError(f"Invalid registry mode: {mode}")

        runtime = Runtime.current()
        async with runtime._registry_lock:
            if name in runtime.registries:
                raise ValueError(f"Registry already exists: {name}")

            runtime.registries[name] = _RegistryEntry(mode=mode)

    @classmethod
    async def register(cls, name: str, key: Any, proc: Process) -> None:
        runtime = Runtime.current()
        async with runtime._registry_lock:
            entry = runtime.registries.get(name)
            if entry is None:
                raise ValueError(f"Unknown registry: {name}")

            if entry.mode == "unique":
                existing_proc_id = entry.unique.get(key)
                if existing_proc_id is not None:
                    existing_proc = runtime.processes.get(existing_proc_id)
                    if existing_proc is not None:
                        raise AlreadyRegistered(existing_proc)
                    entry.unique.pop(key, None)

                entry.unique[key] = proc.id
            else:
                entry.duplicate[key].add(proc.id)

            runtime._proc_keys[proc.id].add((name, key))

    @classmethod
    async def unregister(cls, name: str, key: Any, proc: Process) -> None:
        runtime = Runtime.current()
        async with runtime._registry_lock:
            entry = runtime.registries.get(name)
            if entry is None:
                return

            if entry.mode == "unique":
                if entry.unique.get(key) == proc.id:
                    entry.unique.pop(key, None)
            else:
                proc_ids = entry.duplicate.get(key)
                if proc_ids is not None:
                    proc_ids.discard(proc.id)
                    if not proc_ids:
                        entry.duplicate.pop(key, None)

            proc_keys = runtime._proc_keys.get(proc.id)
            if proc_keys is None:
                return

            proc_keys.discard((name, key))
            if not proc_keys:
                runtime._proc_keys.pop(proc.id, None)

    @classmethod
    async def lookup(cls, name: str, key: Any) -> list[Process]:
        runtime = Runtime.current()
        async with runtime._registry_lock:
            entry = runtime.registries.get(name)
            if entry is None:
                return []

            if entry.mode == "unique":
                proc_id = entry.unique.get(key)
                if proc_id is None:
                    return []

                proc = runtime.processes.get(proc_id)
                return [proc] if proc is not None else []

            proc_ids = list(entry.duplicate.get(key, set()))
            return [
                proc
                for proc_id in proc_ids
                if (proc := runtime.processes.get(proc_id)) is not None
            ]

    @classmethod
    async def dispatch(
        cls,
        name: str,
        key: Any,
        fn: Callable[[Process], Awaitable[None]],
    ) -> None:
        runtime = Runtime.current()
        async with runtime._registry_lock:
            entry = runtime.registries.get(name)
            if entry is None:
                return

            if entry.mode == "unique":
                proc_id = entry.unique.get(key)
                proc_ids = [proc_id] if proc_id is not None else []
            else:
                proc_ids = list(entry.duplicate.get(key, set()))

        for proc_id in proc_ids:
            proc = runtime.processes.get(proc_id)
            if proc is None:
                continue

            try:
                await fn(proc)
            except Exception:
                logger.exception(
                    "Registry dispatch callback failed for %s[%r] and %s",
                    name,
                    key,
                    proc,
                )

    @classmethod
    async def keys(cls, name: str, proc: Process) -> list[Any]:
        runtime = Runtime.current()
        async with runtime._registry_lock:
            if name not in runtime.registries:
                return []

            return [
                key
                for registry_name, key in runtime._proc_keys.get(proc.id, set())
                if registry_name == name
            ]


__all__ = ["AlreadyRegistered", "Registry"]
