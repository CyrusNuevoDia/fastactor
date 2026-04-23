"""Mailbox message types and wire-safe envelope helpers."""

from __future__ import annotations

import typing as t

import msgspec

from fastactor.utils import id_generator

if t.TYPE_CHECKING:
    from .process import Process


_call_ref = id_generator("call")


class Pid(msgspec.Struct, frozen=True):
    """Process identity — local when ``node == ''``, remote otherwise."""

    id: str
    node: str = ""

    @classmethod
    def local(cls, id: str) -> "Pid":
        return cls(id=id)


def _pid_of(proc: "Process | None") -> "Pid | None":
    return Pid.local(proc.id) if proc is not None else None


class Message(msgspec.Struct, kw_only=True, tag="base", dict=True):
    sender_id: "Pid | None" = None
    metadata: dict[str, t.Any] | None = None

    @property
    def sender(self) -> "Process | None":
        # Prefer the strong ref stamped at send time — survives sender death
        # because Python keeps the Process alive while this message holds it.
        ref = self.__dict__.get("_sender_ref")
        if ref is not None:
            return ref
        sid = self.sender_id
        if sid is None or sid.node:
            return None
        from .runtime import Runtime

        try:
            return Runtime.current().processes.get(sid.id)
        except RuntimeError:
            return None


class Info(Message, tag=True, kw_only=True):
    message: t.Any


class Stop(Message, tag=True, kw_only=True):
    reason: t.Any


class Exit(Message, tag=True, kw_only=True):
    reason: t.Any


class Down(Message, tag=True, kw_only=True):
    reason: t.Any
    ref: str | None = None


class Ignore(msgspec.Struct, tag=True):
    """Used by ``Process.init`` to skip starting the loop if desired."""


class Continue(msgspec.Struct, tag=True):
    term: t.Any


class Call[Req = t.Any, Res = t.Any](Message, tag=True, kw_only=True):
    message: Req
    ref: str

    def set_result(self, value: Res | Exception) -> None:
        caller = self.sender
        if caller is not None:
            caller._deliver_reply(self.ref, value)


class CallReply(Message, tag=True, kw_only=True):
    ref: str
    result: t.Any


class Cast[Req = t.Any](Message, tag=True, kw_only=True):
    message: Req


class Subscribe(Message, tag=True, kw_only=True):
    """Sent by a consumer to a producer to initiate a subscription."""

    subscription_id: str
    opts: dict[str, t.Any] = msgspec.field(default_factory=dict)


class SubscribeAck(Message, tag=True, kw_only=True):
    """Sent by a producer back to the consumer acknowledging the subscription."""

    subscription_id: str
    opts: dict[str, t.Any] = msgspec.field(default_factory=dict)


class Cancel(Message, tag=True, kw_only=True):
    """Sent by either side to cancel a subscription."""

    subscription_id: str
    reason: t.Any = "normal"


class Demand(Message, tag=True, kw_only=True):
    """Sent by a consumer to a producer requesting more events."""

    subscription_id: str
    count: int


class Events(Message, tag=True, kw_only=True):
    """Sent by a producer to a consumer delivering events."""

    subscription_id: str
    events: list[t.Any] = msgspec.field(default_factory=list)


class CrashReason(msgspec.Struct, frozen=True):
    """Wire-safe crash reason. Produced from an exception on the emitting side."""

    class_name: str
    repr: str
    traceback_str: str

    @classmethod
    def from_exception(cls, exc: BaseException) -> "CrashReason":
        try:
            import tblib  # ty: ignore[unresolved-import]

            pickled_tb = tblib.Traceback(exc.__traceback__).as_dict()
            tb_str = repr(pickled_tb)
        except ImportError:
            import traceback

            tb_str = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        return cls(
            class_name=type(exc).__name__,
            repr=repr(exc),
            traceback_str=tb_str,
        )
