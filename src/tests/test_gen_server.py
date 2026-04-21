import pytest
from anyio import Event
from anyio.lowlevel import checkpoint
from support import CounterServer, EchoServer

from fastactor.otp import Call, Failed, GenServer, Info, Runtime

pytestmark = pytest.mark.anyio


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

    assert runtime.where_is("echo") is server

    with pytest.raises(Failed, match="already_started: echo"):
        await EchoServer.start(name="echo")

    await server.stop("normal")


async def test_handle_call_returns_continue_runs_handle_continue_before_next_info():
    """G: a GenServer whose handle_call returns (reply, Continue(term)). W: a call is followed by an info. T: handle_continue runs between them."""
    from fastactor.otp import Continue, GenServer

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

    from fastactor.otp import GenServer, Info

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
    from support import MonitorServer

    from fastactor.otp import GenServer, Stop

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
