from abc import ABC
from dataclasses import dataclass, field
import typing as t

from anyio import Event, fail_after

if t.TYPE_CHECKING:
    from .process import Process


@dataclass
class Message(ABC):
    sender: "Process | None"


@dataclass
class Info(Message):
    message: t.Any


@dataclass
class Stop(Message):
    reason: t.Any


@dataclass
class Exit(Message):
    reason: t.Any


@dataclass
class Down(Message):
    reason: t.Any


class Ignore:
    """Used by `Process.init` to skip starting the loop if desired."""


@dataclass
class Continue:
    term: t.Any


@dataclass
class Call[Req = t.Any, Rep = t.Any](Message):
    message: Req
    _result: Rep | Exception | None = field(default=None, init=False)
    _ready: Event = field(default_factory=Event, init=False, repr=False)

    def set_result(self, value: Rep | Exception) -> None:
        self._result = value
        self._ready.set()

    async def result(self, timeout: float = 5) -> Rep:
        with fail_after(timeout):
            await self._ready.wait()
        if isinstance(self._result, Exception):
            raise self._result
        return t.cast(Rep, self._result)


@dataclass
class Cast[Req = t.Any](Message):
    message: Req
