"""Tests for GenStateMachine — §5 spec conformance."""

from typing import Any, Callable

import pytest
from anyio import fail_after, sleep
from anyio.lowlevel import checkpoint

from fastactor.otp import (
    Call,
    GenStateMachine,
    Hibernate,
    NextEvent,
    Postpone,
    Reply,
    StateTimeout,
    Timeout,
)

pytestmark = pytest.mark.anyio


def _call_message(event_type: object) -> Call | None:
    if (
        isinstance(event_type, tuple)
        and len(event_type) == 2
        and event_type[0] == "call"
    ):
        call_msg = event_type[1]
        assert isinstance(call_msg, Call)
        return call_msg
    return None


async def _stop_machine(machine: GenStateMachine) -> None:
    await machine.stop("normal")
    await machine.stopped()


async def _wait_for[T](
    machine: GenStateMachine,
    request: str,
    predicate: Callable[[T], bool],
) -> T:
    with fail_after(2):
        while True:
            value = await machine.call(request)
            if predicate(value):
                return value
            await sleep(0.01)


class Switch(GenStateMachine):
    """handle_event_function mode: flip toggles on/off, count tracks flips."""

    callback_mode = "handle_event_function"

    async def init(self):
        return ("off", 0)

    async def handle_event(self, event_type, event_content, state, data):
        if event_type == "cast" and event_content == "flip":
            new_state = "on" if state == "off" else "off"
            new_data = data + (1 if state == "off" else 0)
            return ("next_state", new_state, new_data)
        if (call_msg := _call_message(event_type)) is not None:
            if event_content == "count":
                return ("keep_state_and_data", [Reply(call_msg, data)])
            if event_content == "state":
                return ("keep_state_and_data", [Reply(call_msg, state)])
        return "keep_state_and_data"


class TrafficLight(GenStateMachine):
    """state_functions mode: red -> green -> yellow -> red."""

    callback_mode = "state_functions"

    async def init(self):
        return ("red", [])

    async def red(self, event_type, event_content, data):
        if event_type == "cast" and event_content == "next":
            return ("next_state", "green", data + ["red->green"])
        if (
            call_msg := _call_message(event_type)
        ) is not None and event_content == "history":
            return ("keep_state_and_data", [Reply(call_msg, list(data))])
        return "keep_state_and_data"

    async def green(self, event_type, event_content, data):
        if event_type == "cast" and event_content == "next":
            return ("next_state", "yellow", data + ["green->yellow"])
        if (
            call_msg := _call_message(event_type)
        ) is not None and event_content == "history":
            return ("keep_state_and_data", [Reply(call_msg, list(data))])
        return "keep_state_and_data"

    async def yellow(self, event_type, event_content, data):
        if event_type == "cast" and event_content == "next":
            return ("next_state", "red", data + ["yellow->red"])
        if (
            call_msg := _call_message(event_type)
        ) is not None and event_content == "history":
            return ("keep_state_and_data", [Reply(call_msg, list(data))])
        return "keep_state_and_data"


async def test_5_1_callback_mode_state_functions() -> None:
    """SPEC §5.1: callback_mode = state_functions routes events to per-state methods."""
    machine = await TrafficLight.start()

    try:
        machine.cast("next")
        machine.cast("next")

        assert await machine.call("history") == ["red->green", "green->yellow"]
    finally:
        await _stop_machine(machine)


async def test_5_1_callback_mode_handle_event_function() -> None:
    """SPEC §5.1: callback_mode = handle_event_function routes events to handle_event/4."""
    machine = await Switch.start()

    try:
        machine.cast("flip")
        count = await machine.call("count")
        machine.cast("flip")
        state = await machine.call("state")

        assert (count, state) == (1, "off")
    finally:
        await _stop_machine(machine)


async def test_5_1_callback_mode_state_enter() -> None:
    """SPEC §5.1: state_enter invokes the new state's enter callback before mailbox events."""

    class EnterTracker(GenStateMachine):
        callback_mode = ("state_functions", "state_enter")

        async def init(self):
            return ("idle", [])

        async def idle(self, event_type, event_content, data):
            if event_type == "cast" and event_content == "start":
                return ("next_state", "ready", data)
            if (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "entered":
                return ("keep_state_and_data", [Reply(call_msg, list(data))])
            return "keep_state_and_data"

        async def ready(self, event_type, event_content, data):
            if event_type == "enter":
                return ("keep_state", data + [event_content])
            if (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "entered":
                return ("keep_state_and_data", [Reply(call_msg, list(data))])
            return "keep_state_and_data"

    machine = await EnterTracker.start()

    try:
        machine.cast("start")

        assert await machine.call("entered") == ["idle"]
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_call() -> None:
    """SPEC §5.2: call events arrive as ('call', From) with request content."""
    machine = await Switch.start()

    try:
        assert await machine.call("state") == "off"
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_cast() -> None:
    """SPEC §5.2: cast events arrive with event_type='cast'."""
    machine = await Switch.start()

    try:
        machine.cast("flip")

        assert await machine.call("state") == "on"
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_info() -> None:
    """SPEC §5.2: raw info messages arrive with event_type='info'."""

    class InfoRecorder(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "info":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await InfoRecorder.start()

    try:
        machine.info("hello")

        assert await machine.call("events") == [("info", "hello")]
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_state_timeout() -> None:
    """SPEC §5.2: state_timeout actions deliver event_type='state_timeout'."""

    class StateTimeoutRecorder(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("idle", None, [StateTimeout(0.05, "tick")])

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "state_timeout":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await StateTimeoutRecorder.start()

    try:
        events = await _wait_for(machine, "events", bool)

        assert events == [("state_timeout", "tick")]
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_generic_timeout() -> None:
    """SPEC §5.2: named Timeout actions deliver event_type=('timeout', Name)."""

    class NamedTimeoutRecorder(GenStateMachine):
        async def init(self):
            self.events: list[tuple[Any, Any]] = []
            return ("idle", None, [Timeout(0.05, "payload", name="my_timer")])

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == ("timeout", "my_timer"):
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await NamedTimeoutRecorder.start()

    try:
        events = await _wait_for(machine, "events", bool)

        assert events == [(("timeout", "my_timer"), "payload")]
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_anonymous_timeout() -> None:
    """SPEC §5.2: anonymous Timeout actions deliver event_type='timeout'."""

    class AnonymousTimeoutRecorder(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("idle", None, [Timeout(0.05, "anon")])

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "timeout":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await AnonymousTimeoutRecorder.start()

    try:
        events = await _wait_for(machine, "events", bool)

        assert events == [("timeout", "anon")]
    finally:
        await _stop_machine(machine)


async def test_5_2_event_type_internal() -> None:
    """SPEC §5.2: next_event injections arrive with event_type='internal'."""

    class InternalRecorder(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("idle", None, [NextEvent("internal", "hello")])

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "internal":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await InternalRecorder.start()

    try:
        await checkpoint()

        assert await machine.call("events") == [("internal", "hello")]
    finally:
        await _stop_machine(machine)


async def test_5_3_action_reply() -> None:
    """SPEC §5.3: Reply actions resolve waiting calls."""

    class ReplyMachine(GenStateMachine):
        async def init(self):
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "ask":
                return ("keep_state_and_data", [Reply(call_msg, "answer")])
            return "keep_state_and_data"

    machine = await ReplyMachine.start()

    try:
        assert await machine.call("ask") == "answer"
    finally:
        await _stop_machine(machine)


async def test_5_3_action_next_event() -> None:
    """SPEC §5.3: NextEvent actions run before later mailbox messages."""

    class NextEventMachine(GenStateMachine):
        async def init(self):
            self.order: list[str] = []
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "cast" and event_content == "trigger":
                self.order.append("trigger")
                return ("keep_state_and_data", [NextEvent("internal", "injected")])
            if event_type == "cast" and event_content == "second":
                self.order.append("second")
            elif event_type == "internal":
                self.order.append(str(event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "order":
                return ("keep_state_and_data", [Reply(call_msg, list(self.order))])
            return "keep_state_and_data"

    machine = await NextEventMachine.start()

    try:
        machine.cast("trigger")
        machine.cast("second")

        assert await machine.call("order") == ["trigger", "injected", "second"]
    finally:
        await _stop_machine(machine)


async def test_5_3_action_postpone() -> None:
    """SPEC §5.3: Postpone replays the current event after the next state change."""

    class PostponeMachine(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, str]] = []
            return ("locked", None)

        async def handle_event(self, event_type, event_content, state, data):
            if (
                state == "locked"
                and event_type == "cast"
                and event_content == "proceed"
            ):
                self.events.append(("locked", "proceed"))
                return ("keep_state_and_data", [Postpone()])
            if state == "locked" and event_type == "cast" and event_content == "open":
                self.events.append(("locked", "open"))
                return ("next_state", "open", data)
            if state == "open" and event_type == "cast" and event_content == "proceed":
                self.events.append(("open", "proceed"))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await PostponeMachine.start()

    try:
        machine.cast("proceed")
        machine.cast("open")

        assert await machine.call("events") == [
            ("locked", "proceed"),
            ("locked", "open"),
            ("open", "proceed"),
        ]
    finally:
        await _stop_machine(machine)


async def test_5_3_action_state_timeout() -> None:
    """SPEC §5.3: StateTimeout actions schedule a state_timeout event."""

    class ArmStateTimeout(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "cast" and event_content == "arm":
                return ("keep_state_and_data", [StateTimeout(0.05, "tick")])
            if event_type == "state_timeout":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await ArmStateTimeout.start()

    try:
        machine.cast("arm")
        events = await _wait_for(machine, "events", bool)

        assert events == [("state_timeout", "tick")]
    finally:
        await _stop_machine(machine)


async def test_5_3_action_generic_timeout_named() -> None:
    """SPEC §5.3: named Timeout actions schedule ('timeout', Name) events."""

    class ArmNamedTimeout(GenStateMachine):
        async def init(self):
            self.events: list[tuple[Any, Any]] = []
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "cast" and event_content == "arm":
                return ("keep_state_and_data", [Timeout(0.05, "tock", name="t1")])
            if event_type == ("timeout", "t1"):
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await ArmNamedTimeout.start()

    try:
        machine.cast("arm")
        events = await _wait_for(machine, "events", bool)

        assert events == [(("timeout", "t1"), "tock")]
    finally:
        await _stop_machine(machine)


async def test_5_3_action_generic_timeout_anonymous() -> None:
    """SPEC §5.3: anonymous Timeout actions schedule timeout events."""

    class ArmAnonymousTimeout(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "cast" and event_content == "arm":
                return ("keep_state_and_data", [Timeout(0.05, "anon")])
            if event_type == "timeout":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await ArmAnonymousTimeout.start()

    try:
        machine.cast("arm")
        events = await _wait_for(machine, "events", bool)

        assert events == [("timeout", "anon")]
    finally:
        await _stop_machine(machine)


async def test_5_3_action_hibernate() -> None:
    """SPEC §5.3: Hibernate is accepted and leaves the machine running."""

    class HibernateMachine(GenStateMachine):
        async def init(self):
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "cast" and event_content == "rest":
                return ("keep_state_and_data", [Hibernate()])
            if (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "status":
                return ("keep_state_and_data", [Reply(call_msg, "awake")])
            return "keep_state_and_data"

    machine = await HibernateMachine.start()

    try:
        machine.cast("rest")
        await checkpoint()

        assert (not machine.has_stopped(), await machine.call("status")) == (
            True,
            "awake",
        )
    finally:
        await _stop_machine(machine)


async def test_5_4_postponed_events_replay_on_state_change() -> None:
    """SPEC §5.4: postponed events replay in the new state after a state change."""

    class ReplayMachine(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, str]] = []
            return ("waiting", None)

        async def handle_event(self, event_type, event_content, state, data):
            if state == "waiting" and event_type == "cast" and event_content == "work":
                self.events.append(("waiting", "work"))
                return ("keep_state_and_data", [Postpone()])
            if state == "waiting" and event_type == "cast" and event_content == "ready":
                self.events.append(("waiting", "ready"))
                return ("next_state", "active", data)
            if state == "active" and event_type == "cast" and event_content == "work":
                self.events.append(("active", "work"))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await ReplayMachine.start()

    try:
        machine.cast("work")
        machine.cast("ready")

        assert await machine.call("events") == [
            ("waiting", "work"),
            ("waiting", "ready"),
            ("active", "work"),
        ]
    finally:
        await _stop_machine(machine)


async def test_5_5_state_timeout_cancelled_on_state_change() -> None:
    """SPEC §5.5: state_timeout is cancelled automatically when the state changes."""

    class CancelledStateTimeout(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any]] = []
            return ("a", None, [StateTimeout(0.1, "should_not_fire")])

        async def handle_event(self, event_type, event_content, state, data):
            if state == "a" and event_type == "cast" and event_content == "go":
                return ("next_state", "b", data)
            if event_type == "state_timeout":
                self.events.append((event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await CancelledStateTimeout.start()

    try:
        machine.cast("go")
        await checkpoint()
        with fail_after(2):
            await sleep(0.15)

        assert (machine.state, await machine.call("events")) == ("b", [])
    finally:
        await _stop_machine(machine)


async def test_5_5_named_generic_timeout_persists_across_state_change() -> None:
    """SPEC §5.5: named generic timeouts survive state changes."""

    class PersistentNamedTimeout(GenStateMachine):
        async def init(self):
            self.events: list[tuple[str, Any, Any]] = []
            return ("a", None, [Timeout(0.1, "persist", name="keeper")])

        async def handle_event(self, event_type, event_content, state, data):
            if state == "a" and event_type == "cast" and event_content == "go":
                return ("next_state", "b", data)
            if event_type == ("timeout", "keeper"):
                self.events.append((state, event_type, event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "events":
                return ("keep_state_and_data", [Reply(call_msg, list(self.events))])
            return "keep_state_and_data"

    machine = await PersistentNamedTimeout.start()

    try:
        machine.cast("go")
        await checkpoint()
        events = await _wait_for(machine, "events", bool)

        assert events == [("b", ("timeout", "keeper"), "persist")]
    finally:
        await _stop_machine(machine)


async def test_5_6_next_event_processed_before_mailbox() -> None:
    """SPEC §5.6: next_event actions run before queued mailbox messages in action order."""

    class OrderedNextEvents(GenStateMachine):
        async def init(self):
            self.order: list[str] = []
            return ("idle", None)

        async def handle_event(self, event_type, event_content, state, data):
            if event_type == "cast" and event_content == "trigger":
                self.order.append("trigger")
                return (
                    "keep_state_and_data",
                    [NextEvent("internal", "a"), NextEvent("internal", "b")],
                )
            if event_type == "cast" and event_content == "second":
                self.order.append("second")
            elif event_type == "internal":
                self.order.append(str(event_content))
            elif (
                call_msg := _call_message(event_type)
            ) is not None and event_content == "order":
                return ("keep_state_and_data", [Reply(call_msg, list(self.order))])
            return "keep_state_and_data"

    machine = await OrderedNextEvents.start()

    try:
        machine.cast("trigger")
        machine.cast("second")

        assert await machine.call("order") == ["trigger", "a", "b", "second"]
    finally:
        await _stop_machine(machine)
