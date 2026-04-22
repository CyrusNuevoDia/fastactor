"""Mailbox message types — `Info`, `Stop`, `Exit`, `Down`, `Call`, `Cast`, `Continue`, `Ignore`, `Subscribe`, `SubscribeAck`, `Cancel`, `Demand`, `Events`.

All messages carry `sender: Process | None` and optional `metadata`. `Call` also
carries an Event + result slot for the reply round-trip. `Continue` and `Ignore`
are control-flow sentinels, not mailbox-delivered.
See `src/fastactor/otp/README.md#messages-reference`.
"""

import typing as t
from abc import ABC
from dataclasses import dataclass, field

from anyio import Event, fail_after

from fastactor.settings import settings

if t.TYPE_CHECKING:
    from .process import Process


@dataclass
class Message(ABC):
    sender: "Process | None"
    metadata: dict[str, t.Any] | None = field(default=None, kw_only=True)


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
    ref: str | None = None


class Ignore:
    """Used by `Process.init` to skip starting the loop if desired."""


@dataclass
class Continue:
    term: t.Any


@dataclass
class Call[Req = t.Any, Res = t.Any](Message):
    message: Req
    _result: Res | Exception | None = field(default=None, init=False)
    _ready: Event = field(default_factory=Event, init=False, repr=False)

    def set_result(self, value: Res | Exception) -> None:
        self._result = value
        self._ready.set()

    async def result(self, timeout: float = settings.call_timeout) -> Res:
        with fail_after(timeout):
            await self._ready.wait()
        if isinstance(self._result, Exception):
            raise self._result
        return t.cast(Res, self._result)


@dataclass
class Cast[Req = t.Any](Message):
    message: Req


# --- GenStage message types ---


@dataclass
class Subscribe(Message):
    """Sent by a consumer to a producer to initiate a subscription."""

    subscription_id: str
    opts: dict[str, t.Any] = field(default_factory=dict)


@dataclass
class SubscribeAck(Message):
    """Sent by a producer back to the consumer acknowledging the subscription."""

    subscription_id: str
    opts: dict[str, t.Any] = field(default_factory=dict)


@dataclass
class Cancel(Message):
    """Sent by either side to cancel a subscription."""

    subscription_id: str
    reason: t.Any = "normal"


@dataclass
class Demand(Message):
    """Sent by a consumer to a producer requesting more events."""

    subscription_id: str
    count: int


@dataclass
class Events(Message):
    """Sent by a producer to a consumer delivering events."""

    subscription_id: str
    events: list[t.Any] = field(default_factory=list)
