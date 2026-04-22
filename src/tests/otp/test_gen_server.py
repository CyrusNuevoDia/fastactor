from typing import Any

import pytest
from anyio import Event, create_task_group, fail_after, sleep
from anyio.lowlevel import checkpoint
from .helpers import (
    CounterServer,
    EchoServer,
    MonitorServer,
)

from fastactor.otp import (
    Call,
    Cast,
    Continue,
    Failed,
    GenServer,
    Ignore,
    Info,
    Runtime,
    Shutdown,
    Stop,
)

pytestmark = pytest.mark.anyio


class StopInInitServer(GenServer):
    async def init(self, reason: Any = "boot_failed") -> Stop:
        return Stop(None, reason)


class DeferredReplyServer(GenServer):
    async def init(self) -> None:
        self.pending: list[Call] = []

    async def handle_call(self, call: Call) -> None:
        self.pending.append(call)
        return None


class InfoServer(GenServer):
    async def init(self) -> None:
        self.infos: list[object] = []
        self.flushed = Event()

    async def handle_info(self, message: Info) -> None:
        self.infos.append(message.message)
        if message.message == "flush":
            self.flushed.set()


class DelayedReplyServer(GenServer):
    async def handle_call(self, call: Call) -> str:
        await checkpoint()
        return str(call.message)


async def test_call_returns_echo_payload():
    """G: an EchoServer. W: we call it. T: the reply is the payload."""
    server = await EchoServer.start()

    assert await server.call("ping") == "ping"

    await server.stop("normal")


async def test_casts_are_processed_before_sync_call():
    """G: an EchoServer with queued casts. W: we make a sync call. T: earlier casts have drained."""
    server = await EchoServer.start()

    server.cast("first")
    server.cast("second")
    assert await server.call("sync") == "sync"

    assert server.log == ["first", "second"]

    await server.stop("normal")


async def test_info_messages_route_to_handle_info():
    """G: an InfoServer. W: infos are sent. T: handle_info records them in FIFO order."""
    server = await InfoServer.start()

    server.info("hello")
    server.info("flush")
    await server.flushed.wait()

    assert server.infos == ["hello", "flush"]

    await server.stop("normal")


async def test_counter_get_returns_init_state():
    """G: a CounterServer with initial state. W: we call get. T: it returns the count."""
    server = await CounterServer.start(count=7)

    assert await server.call("get") == 7

    await server.stop("normal")


async def test_counter_cast_mutations_are_visible_after_sync():
    """G: a CounterServer. W: casts mutate state before a sync call. T: get sees the updated count."""
    server = await CounterServer.start(count=10)

    server.cast(("add", 5))
    server.cast(("sub", 3))
    server.cast(("mul", 2))
    await server.call("sync")

    assert await server.call("get") == 24

    await server.stop("normal")


async def test_bad_call_raises_and_crashes_server():
    """G: a CounterServer. W: we call an unsupported request. T: the call raises and the server crashes."""
    server = await CounterServer.start()

    with pytest.raises(ValueError, match="bad"):
        await server.call("bad")

    await server.stopped()
    assert isinstance(server._crash_exc, ValueError)


async def test_call_timeout_can_expire_before_reply():
    """G: a delayed reply server. W: the call timeout is zero. T: TimeoutError wins before the reply arrives."""
    server = await DelayedReplyServer.start()

    with pytest.raises(TimeoutError):
        await server.call("slow", timeout=0)

    assert await server.call("ping") == "ping"

    await server.stop("normal")


async def test_named_start_registers_lookup_and_rejects_duplicates(runtime: Runtime):
    """G: a named server is running. W: we look it up and try a duplicate name. T: lookup works and duplicate start fails."""
    server = await EchoServer.start(name="echo")

    assert await runtime.whereis("echo") is server

    with pytest.raises(Failed) as exc_info:
        await EchoServer.start(name="echo")
    reason = exc_info.value.reason
    assert isinstance(reason, tuple) and reason[0] == "already_started"

    await server.stop("normal")


async def test_handle_call_returns_continue_runs_handle_continue_before_next_info():
    """G: a GenServer whose handle_call returns (reply, Continue(term)). W: a call is followed by an info. T: handle_continue runs between them."""

    class Observer(GenServer):
        async def init(self):
            self.order = []

        async def handle_call(self, call):
            self.order.append(("call", call.message))
            if call.message == "ping":
                return ("reply", Continue("after-call"))
            return list(self.order)

        async def handle_continue(self, term):
            self.order.append(("continue", term))

        async def handle_info(self, message):
            self.order.append(("info", message.message))

    srv = await Observer.start()

    assert await srv.call("ping") == "reply"
    srv.info("later")
    order = await srv.call("snapshot")

    assert order[:3] == [
        ("call", "ping"),
        ("continue", "after-call"),
        ("info", "later"),
    ]

    await srv.stop("normal")


async def test_handle_call_none_is_deferred_reply():
    """G: a GenServer whose handle_call stashes the Call and returns None. W: a later info resolves it. T: the awaiting call gets the deferred value."""
    import anyio

    class Deferred(GenServer):
        async def init(self):
            self.pending = None

        async def handle_call(self, call):
            self.pending = call
            return None

        async def handle_info(self, message):
            if isinstance(message, Info) and message.message == "release":
                if self.pending is not None:
                    self.pending.set_result(42)
                    self.pending = None

    srv = await Deferred.start()

    async with anyio.create_task_group() as tg:

        async def caller():
            result = await srv.call("deferred", timeout=2)
            assert result == 42

        tg.start_soon(caller)
        await anyio.sleep(0.05)
        srv.info("release")

    await srv.stop("normal")


async def test_handle_call_tuple_stop_with_reply():
    """G: a GenServer whose handle_call returns (reply, Stop(reason)). W: the call completes. T: the caller gets the reply and the process stops with that reason."""

    class Stoppy(GenServer):
        async def handle_call(self, call):
            return ("farewell", Stop(None, "custom_reason"))

    monitor = await MonitorServer.start()
    srv = await Stoppy.start()
    monitor.monitor(srv)

    reply = await srv.call("bye")
    assert reply == "farewell"

    await srv.stopped()
    downs = await monitor.await_events(1, timeout=2)

    assert len(downs) == 1
    assert downs[0].reason == "custom_reason"

    await monitor.stop("normal")


# ---------------------------------------------------------------------------
# §4 gen_server conformance
# ---------------------------------------------------------------------------


class OkInitServer(GenServer):
    async def init(self) -> None:
        self.state = "ready"


class OkContinueInitServer(GenServer):
    async def init(self) -> Continue:
        self.order: list[Any] = ["init"]
        return Continue("post-init")

    async def handle_continue(self, term: Any) -> None:
        self.order.append(("continue", term))

    async def handle_call(self, call: Call) -> list[Any]:
        return list(self.order)


class IgnoreInitServer(GenServer):
    async def init(self) -> Ignore:
        return Ignore()


async def test_4_1_init_ok_state() -> None:
    """SPEC §4.1: init returns {ok, State} — process runs and holds state.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:init/1
    """
    srv = await OkInitServer.start()

    assert srv.state == "ready"
    assert not srv.has_stopped()

    await srv.stop("normal")


async def test_4_1_init_ok_state_timeout() -> None:
    """SPEC §4.1: init returns {ok, State, Timeout} — handle_info(timeout, _) fires after T ms idle.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:init/1
    """

    class TimeoutInit(GenServer):
        async def init(self) -> tuple[None, int]:
            self.timed_out = False
            return (None, 100)

        async def handle_info(self, message: Info) -> None:
            if message.message == "timeout":
                self.timed_out = True

    srv = await TimeoutInit.start()
    await sleep(0.2)
    assert srv.timed_out
    await srv.stop("normal")


async def test_4_1_init_ok_state_continue() -> None:
    """SPEC §4.1: init returns {ok, State, {continue, Term}} — handle_continue runs before mailbox.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:init/1
    """
    srv = await OkContinueInitServer.start()

    order = await srv.call("snapshot")
    assert order[:2] == ["init", ("continue", "post-init")]

    await srv.stop("normal")


async def test_4_1_init_stop_exits_with_reason() -> None:
    """SPEC §4.1: init returns {stop, Reason} — process exits; caller receives the reason.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:init/1
    """
    with pytest.raises(Shutdown) as exc_info:
        await StopInInitServer.start(reason="bootstrap_failed")

    assert exc_info.value.reason == "bootstrap_failed"


async def test_4_1_init_ignore_signals_caller_without_process() -> None:
    """SPEC §4.1: init returns `ignore` — start_link caller receives `ignore`, no process remains.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:init/1
    """
    result = await IgnoreInitServer.start()  # Erlang: returns the atom 'ignore'

    assert result == "ignore"  # port raises, never returns this


class ReplyServer(GenServer):
    async def handle_call(self, call: Call) -> Any:
        return ("pong", call.message)


async def test_4_2_handle_call_reply() -> None:
    """SPEC §4.2: handle_call returns {reply, Reply, NewState}.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3
    """
    srv = await ReplyServer.start()

    reply = await srv.call("hi")
    assert reply == ("pong", "hi")

    await srv.stop("normal")


async def test_4_2_handle_call_reply_with_continue() -> None:
    """SPEC §4.2: handle_call returns {reply, Reply, NewState, {continue, C}} — continue runs next.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3
    """

    class Observer(GenServer):
        async def init(self) -> None:
            self.order: list[Any] = []

        async def handle_call(self, call: Call) -> tuple[str, Continue]:
            self.order.append(("call", call.message))
            if call.message == "first":
                return ("ok", Continue("c1"))
            return ("snapshot", Continue("noop"))  # sentinel path

        async def handle_continue(self, term: Any) -> None:
            self.order.append(("continue", term))

    srv = await Observer.start()

    assert await srv.call("first") == "ok"
    await srv.stop("normal")

    assert srv.order[:2] == [("call", "first"), ("continue", "c1")]


async def test_4_2_handle_call_reply_with_timeout() -> None:
    """SPEC §4.2: handle_call returns {reply, Reply, State, Timeout} — idle timeout.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3
    """

    class TimeoutReply(GenServer):
        async def init(self) -> None:
            self.timed_out = False

        async def handle_call(self, call: Call) -> tuple[str, int]:  # type: ignore[override]
            return ("ok", 100)

        async def handle_info(self, message: Info) -> None:
            if message.message == "timeout":
                self.timed_out = True

    srv = await TimeoutReply.start()
    assert await srv.call("x") == "ok"
    await sleep(0.2)
    assert srv.timed_out
    await srv.stop("normal")


async def test_4_2_handle_call_noreply_unblocks_on_explicit_set_result() -> None:
    """SPEC §4.2: {noreply, NewState} — caller blocked until an explicit reply is issued.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3
    """
    srv = await DeferredReplyServer.start()

    async def release_after() -> None:
        with fail_after(1):
            while not srv.pending:
                await checkpoint()
        srv.pending[0].set_result(42)

    reply = None
    async with create_task_group() as tg:
        tg.start_soon(release_after)
        reply = await srv.call("wait", timeout=2)

    assert reply == 42
    await srv.stop("normal")


async def test_4_2_handle_call_stop_with_reply() -> None:
    """SPEC §4.2: {stop, Reason, Reply, NewState} — reply is sent, then the server terminates.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3
    """

    class StoppyReply(GenServer):
        async def handle_call(self, call: Call) -> tuple[str, Stop]:
            return ("farewell", Stop(None, "bye"))

    srv = await StoppyReply.start()

    assert await srv.call("exit") == "farewell"
    await srv.stopped()

    assert srv.has_stopped()


async def test_4_2_handle_call_stop_without_reply() -> None:
    """SPEC §4.2: {stop, Reason, NewState} — terminate without replying; caller sees DOWN/timeout.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3
    """

    class StoppyNoReply(GenServer):
        async def handle_call(self, call: Call) -> Stop:  # type: ignore[override]
            # Erlang: {stop, Reason, NewState}. Port has no such shape.
            return Stop(None, "bye")  # type: ignore[return-value]

    srv = await StoppyNoReply.start()

    with pytest.raises(Shutdown) as exc:
        await srv.call("exit", timeout=1)
    assert exc.value.reason == "bye"


async def test_4_3_call_returns_reply_from_live_server() -> None:
    """SPEC §4.3: call on a live server returns the reply.

    Source: https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen.erl do_call
    """
    srv = await EchoServer.start()

    assert await srv.call("ping") == "ping"

    await srv.stop("normal")


async def test_4_3_call_crashed_handler_raises_same_reason() -> None:
    """SPEC §4.3: When handle_call raises, caller raises the same reason (not timeout).

    Source: https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen.erl do_call
    """

    class CrashingCall(GenServer):
        async def handle_call(self, call: Call) -> None:
            raise ValueError("bad request")

    srv = await CrashingCall.start()

    with pytest.raises(ValueError, match="bad request"):
        await srv.call("x", timeout=1)

    await srv.stopped()


async def test_4_3_call_dead_server_raises_noproc() -> None:
    """SPEC §4.3: call on a dead/non-existent server raises `noproc` immediately.

    Source: https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen.erl do_call
    """
    srv = await EchoServer.start()
    await srv.stop("normal")
    await srv.stopped()

    with pytest.raises(Exception) as exc:
        await srv.call("ping", timeout=1)
    assert "noproc" in str(exc.value)  # Erlang-exact; port raises a different error.


async def test_4_3_call_timeout_does_not_kill_callee() -> None:
    """SPEC §4.3: call timeout only affects the caller's wait; the callee keeps running.

    Source: https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen.erl do_call;
    see also .claude/SPEC.md §10.3.
    """

    class SlowReply(GenServer):
        async def handle_call(self, call: Call) -> str:
            await sleep(0.15)
            return "ok"

    srv = await SlowReply.start()

    with pytest.raises(TimeoutError):
        await srv.call("slow", timeout=0.05)

    assert not srv.has_stopped()
    # Server is still responsive after caller timed out.
    assert await srv.call("slow", timeout=1) == "ok"

    await srv.stop("normal")


async def test_4_4_cast_to_live_server_returns_immediately() -> None:
    """SPEC §4.4: cast returns ok immediately; handle_cast is invoked asynchronously.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#cast/2
    """

    class Recorder(GenServer):
        async def init(self) -> None:
            self.log: list[Any] = []

        async def handle_cast(self, cast: Cast) -> None:
            self.log.append(cast.message)

        async def handle_call(self, call: Call) -> list[Any]:
            return list(self.log)

    srv = await Recorder.start()

    result = srv.cast("m1")
    assert result is None

    drained = await srv.call("drain")
    assert drained == ["m1"]

    await srv.stop("normal")


async def test_4_4_cast_to_dead_pid_returns_ok_silently() -> None:
    """SPEC §4.4: cast to a dead pid returns ok with no exception.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#cast/2
    """
    srv = await EchoServer.start()
    await srv.stop("normal")
    await srv.stopped()

    # Erlang: always returns ok; port raises.
    assert srv.cast("ghost") is None


async def test_4_5_handle_info_receives_raw_message() -> None:
    """SPEC §4.5: Any non-call/cast/system message is delivered to handle_info/2.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_info/2
    """

    class InfoCatcher(GenServer):
        async def init(self) -> None:
            self.seen: list[Any] = []

        async def handle_info(self, message: Info) -> None:
            self.seen.append(message.message)

        async def handle_call(self, call: Call) -> list[Any]:
            return list(self.seen)

    srv = await InfoCatcher.start()
    srv.info(("arbitrary", 42))

    with fail_after(2):
        while not srv.seen:
            await checkpoint()

    assert srv.seen == [("arbitrary", 42)]

    await srv.stop("normal")


async def test_4_6_handle_continue_runs_before_next_mailbox_message() -> None:
    """SPEC §4.6: handle_continue runs before any further mailbox messages and can chain.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_continue/2
    """

    class Chain(GenServer):
        async def init(self) -> Continue:
            self.order: list[Any] = ["init"]
            return Continue("step1")

        async def handle_continue(self, term: Any) -> Continue | None:
            self.order.append(("continue", term))
            if term == "step1":
                return Continue("step2")
            return None

        async def handle_info(self, message: Info) -> None:
            self.order.append(("info", message.message))

        async def handle_call(self, call: Call) -> list[Any]:
            return list(self.order)

    srv = await Chain.start()
    srv.info("mail")

    order = await srv.call("snapshot")

    assert order[:3] == ["init", ("continue", "step1"), ("continue", "step2")]
    # Mailbox processed strictly after continues have drained.
    assert ("info", "mail") in order

    await srv.stop("normal")


async def test_4_7_timeout_message_after_idle_period() -> None:
    """SPEC §4.7: Callback returning {..., Timeout} emits handle_info(timeout, _) after idle.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html
    """

    class IdleTimeout(GenServer):
        async def init(self) -> None:
            self.timed_out = False

        async def handle_call(self, call: Call) -> tuple[str, int]:  # type: ignore[override]
            return ("ok", 100)

        async def handle_info(self, message: Info) -> None:
            if message.message == "timeout":
                self.timed_out = True

    srv = await IdleTimeout.start()
    assert await srv.call("go") == "ok"
    await sleep(0.2)
    assert srv.timed_out
    await srv.stop("normal")


async def test_4_8_terminate_called_on_shutdown_when_trapping() -> None:
    """SPEC §4.8: Trapping + timeout-shutdown — terminate(shutdown, _) is observed.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:terminate/2
    """

    class Cleanup(GenServer):
        async def init(self) -> None:
            self.reason: Any = None

        async def terminate(self, reason: Any) -> None:
            self.reason = reason
            await super().terminate(reason)

    srv = await Cleanup.start(trap_exits=True)

    await srv.stop("shutdown")
    await srv.stopped()

    assert srv.reason == "shutdown"


async def test_4_8_no_terminate_when_not_trapping_exits() -> None:
    """SPEC §4.8: Without trap_exits, the process dies without terminate running.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:terminate/2
    """

    class Cleanup(GenServer):
        async def init(self) -> None:
            self.terminated = False

        async def terminate(self, reason: Any) -> None:
            self.terminated = True
            await super().terminate(reason)

    srv = await Cleanup.start(trap_exits=False)
    await srv.stop("shutdown")
    await srv.stopped()

    assert not srv.terminated


async def test_4_9_reply_allows_decoupled_reply_from_later_event() -> None:
    """SPEC §4.9: gen_server.reply/2 unblocks a caller left hanging by {noreply, _}.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#reply/2
    """
    from fastactor.otp import gen_server  # type: ignore[attr-defined]

    srv = await DeferredReplyServer.start()

    async def release() -> None:
        with fail_after(1):
            while not srv.pending:
                await checkpoint()
        from_ = srv.pending[0]
        gen_server.reply(from_, 42)  # type: ignore[attr-defined]

    reply = None
    async with create_task_group() as tg:
        tg.start_soon(release)
        reply = await srv.call("wait", timeout=2)

    assert reply == 42
    await srv.stop("normal")
