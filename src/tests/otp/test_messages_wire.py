"""Wire-safety tests — every envelope type must round-trip via pickle + msgspec.json."""

import pickle

import msgspec
import pytest

from fastactor.otp._messages import (
    Call,
    CallReply,
    Cancel,
    Cast,
    Continue,
    CrashReason,
    Demand,
    Down,
    Events,
    Exit,
    Ignore,
    Info,
    Pid,
    Stop,
    Subscribe,
    SubscribeAck,
)

pytestmark = pytest.mark.anyio


def _roundtrip(msg, typ):
    """Round-trip via pickle AND msgspec.json. Asserts no exception."""
    pickled = pickle.loads(pickle.dumps(msg))
    raw = msgspec.json.encode(msg)
    decoded = msgspec.json.decode(raw, type=typ)
    return pickled, decoded


def test_pid_roundtrip():
    msg = Pid(id="abc123")
    pickled, decoded = _roundtrip(msg, Pid)
    assert pickled.id == "abc123"
    assert decoded.id == "abc123"
    assert decoded.node == ""


def test_info_roundtrip():
    msg = Info(pid=None, message="hi", metadata={"k": "v"})
    pickled, decoded = _roundtrip(msg, Info)
    assert pickled.message == "hi"
    assert decoded.message == "hi"
    assert decoded.metadata == {"k": "v"}


def test_stop_roundtrip():
    msg = Stop(pid=None, reason="shutdown")
    pickled, decoded = _roundtrip(msg, Stop)
    assert pickled.reason == "shutdown"
    assert decoded.reason == "shutdown"


def test_exit_roundtrip():
    msg = Exit(pid=Pid(id="proc-1"), reason="boom")
    pickled, decoded = _roundtrip(msg, Exit)
    assert pickled.pid == Pid(id="proc-1")
    assert decoded.reason == "boom"


def test_down_roundtrip():
    msg = Down(pid=Pid(id="target-1"), reason="noproc", ref="ref-1")
    pickled, decoded = _roundtrip(msg, Down)
    assert decoded.pid == Pid(id="target-1")
    assert pickled.ref == "ref-1"
    assert decoded.ref == "ref-1"
    assert decoded.reason == "noproc"


def test_call_roundtrip():
    # Generic PEP-695 Struct subclasses can't be used as msgspec.json decode targets
    # (msgspec can't resolve the TypeVar forward refs). Test pickle + encode only.
    msg = Call(pid=Pid(id="caller-1"), message={"op": "get"}, ref="r-1")
    pickled = pickle.loads(pickle.dumps(msg))
    assert pickled.ref == "r-1"
    assert pickled.pid == Pid(id="caller-1")
    raw = msgspec.json.encode(msg)
    assert b'"ref":"r-1"' in raw
    assert b'"type":"Call"' in raw


def test_call_reply_roundtrip():
    msg = CallReply(pid=None, ref="r-1", result=42)
    pickled, decoded = _roundtrip(msg, CallReply)
    assert pickled.result == 42
    assert decoded.ref == "r-1"
    assert decoded.result == 42


def test_cast_roundtrip():
    # Generic PEP-695 Struct subclasses can't be used as msgspec.json decode targets.
    msg = Cast(pid=Pid(id="caller-1"), message=["a", "b"])
    pickled = pickle.loads(pickle.dumps(msg))
    assert pickled.message == ["a", "b"]
    raw = msgspec.json.encode(msg)
    assert b'"message":["a","b"]' in raw
    assert b'"type":"Cast"' in raw


def test_subscribe_roundtrip():
    msg = Subscribe(
        pid=Pid(id="consumer-1"),
        subscription_id="sub-1",
        opts={"max_demand": 10},
    )
    pickled, decoded = _roundtrip(msg, Subscribe)
    assert pickled.subscription_id == "sub-1"
    assert decoded.opts == {"max_demand": 10}


def test_subscribe_ack_roundtrip():
    msg = SubscribeAck(
        pid=Pid(id="producer-1"),
        subscription_id="sub-1",
        opts={"min_demand": 2},
    )
    pickled, decoded = _roundtrip(msg, SubscribeAck)
    assert pickled.subscription_id == "sub-1"
    assert decoded.opts == {"min_demand": 2}


def test_cancel_roundtrip():
    msg = Cancel(pid=Pid(id="consumer-1"), subscription_id="sub-1", reason="bye")
    pickled, decoded = _roundtrip(msg, Cancel)
    assert pickled.reason == "bye"
    assert decoded.subscription_id == "sub-1"
    assert decoded.reason == "bye"


def test_demand_roundtrip():
    msg = Demand(pid=Pid(id="consumer-1"), subscription_id="sub-1", count=5)
    pickled, decoded = _roundtrip(msg, Demand)
    assert pickled.count == 5
    assert decoded.count == 5


def test_events_roundtrip():
    msg = Events(
        pid=Pid(id="producer-1"),
        subscription_id="sub-1",
        events=[1, 2, 3],
    )
    pickled, decoded = _roundtrip(msg, Events)
    assert pickled.events == [1, 2, 3]
    assert decoded.events == [1, 2, 3]


def test_continue_roundtrip():
    msg = Continue("next")
    pickled, decoded = _roundtrip(msg, Continue)
    assert pickled.term == "next"
    assert decoded.term == "next"


def test_ignore_roundtrip():
    msg = Ignore()
    pickled, decoded = _roundtrip(msg, Ignore)
    assert isinstance(pickled, Ignore)
    assert isinstance(decoded, Ignore)


def test_crash_reason_roundtrip():
    msg = CrashReason(
        class_name="ValueError",
        repr="ValueError('boom')",
        traceback_str="ValueError: boom",
    )
    pickled, decoded = _roundtrip(msg, CrashReason)
    assert pickled.class_name == "ValueError"
    assert decoded.class_name == "ValueError"
    assert decoded.traceback_str == "ValueError: boom"


def test_crash_reason_from_exception():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        crash_reason = CrashReason.from_exception(exc)

    assert crash_reason.class_name == "ValueError"
    assert "boom" in crash_reason.repr
    assert "ValueError" in crash_reason.traceback_str

    pickled, decoded = _roundtrip(crash_reason, CrashReason)
    assert pickled.class_name == "ValueError"
    assert decoded.class_name == "ValueError"
