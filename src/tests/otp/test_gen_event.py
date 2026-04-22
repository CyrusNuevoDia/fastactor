"""OTP conformance suite — §7 gen_event.

See `SPEC.md` §7 for the source claims.
"""

from typing import Any

import pytest

from fastactor.otp import (
    EventHandler,
    GenEvent,
    HandlerNotInstalled,
    RemoveHandler,
    SwapHandler,
)

pytestmark = pytest.mark.anyio


class Recorder(EventHandler):
    """Records every event it sees into `self.events`, tagged with `self.tag`."""

    async def init(self, tag: str = "r") -> None:
        self.tag = tag
        self.events: list[Any] = []
        self.init_args: tuple[Any, ...] = ()

    async def handle_event(self, event: Any) -> None:
        self.events.append(event)
        return None

    async def handle_call(self, request: Any) -> Any:
        return ("reply", self.tag, request)

    async def terminate(self, arg: Any) -> Any:
        self.terminated_with = arg
        return ("terminated", self.tag, arg)


class Exploder(EventHandler):
    """Raises from `handle_event` to exercise the crash-removal path."""

    async def init(self) -> None:
        self.terminated_with: Any = None

    async def handle_event(self, event: Any) -> None:
        raise RuntimeError(f"boom on {event!r}")

    async def terminate(self, arg: Any) -> Any:
        self.terminated_with = arg
        return arg


class RemoveOnEvent(EventHandler):
    async def init(self) -> None:
        self.terminated_with: Any = None

    async def handle_event(self, event: Any) -> RemoveHandler:
        return RemoveHandler()

    async def terminate(self, arg: Any) -> Any:
        self.terminated_with = arg
        return arg


class SwapSource(EventHandler):
    async def init(self) -> None:
        self.terminated_with: Any = None

    async def handle_event(self, event: Any) -> SwapHandler:
        return SwapHandler(args1="swap-arg1", handler2=SwapTarget(), args2="swap-arg2")

    async def terminate(self, arg: Any) -> Any:
        self.terminated_with = arg
        return "from-source-terminate"


class SwapTarget(EventHandler):
    async def init(self, init_pair: Any) -> None:
        self.init_pair = init_pair
        self.events: list[Any] = []

    async def handle_event(self, event: Any) -> None:
        self.events.append(event)


async def test_7_1_event_manager_dispatches_to_every_handler() -> None:
    """SPEC §7.1: An event is dispatched to every installed handler.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html
    """
    manager = await GenEvent.start()
    h1, h2, h3 = Recorder(), Recorder(), Recorder()
    await manager.add_handler(h1, tag="one")
    await manager.add_handler(h2, tag="two")
    await manager.add_handler(h3, tag="three")

    await manager.sync_notify("ping")

    assert h1.events == ["ping"]
    assert h2.events == ["ping"]
    assert h3.events == ["ping"]

    handlers = await manager.which_handlers()
    assert handlers == [h1, h2, h3]  # install order preserved

    await manager.stop("normal")


async def test_7_2_add_handler_calls_init() -> None:
    """SPEC §7.2: add_handler/3 invokes Handler:init/1.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html#add_handler/3
    """
    manager = await GenEvent.start()
    h = Recorder()
    await manager.add_handler(h, tag="inited")

    assert h.tag == "inited"
    assert (await manager.which_handlers()) == [h]

    await manager.stop("normal")


async def test_7_2_delete_handler_calls_terminate() -> None:
    """SPEC §7.2: delete_handler/3 invokes Handler:terminate/2.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html#delete_handler/3
    """
    manager = await GenEvent.start()
    h = Recorder()
    await manager.add_handler(h, tag="t")

    result = await manager.delete_handler(h, arg="bye")

    assert h.terminated_with == "bye"
    assert result == ("terminated", "t", "bye")
    assert (await manager.which_handlers()) == []

    with pytest.raises(HandlerNotInstalled):
        await manager.delete_handler(h, arg="again")

    await manager.stop("normal")


async def test_7_2_swap_handler_calls_terminate_then_init() -> None:
    """SPEC §7.2: swap_handler calls Old:terminate then New:init({NewArgs, TerminateResult}).

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html#swap_handler/3
    """
    manager = await GenEvent.start()
    source = SwapSource()
    target = SwapTarget()
    await manager.add_handler(source)

    await manager.swap_handler((source, "old-args"), (target, "new-args"))

    # Old handler saw its terminate arg
    assert source.terminated_with == "old-args"
    # New handler's init received ({args2}, <result of old's terminate>)
    assert target.init_pair == ("new-args", "from-source-terminate")

    handlers = await manager.which_handlers()
    assert handlers == [target]

    # Swapped-in handler actually receives subsequent events
    await manager.sync_notify("after-swap")
    assert target.events == ["after-swap"]

    await manager.stop("normal")


async def test_7_3_crashed_handler_is_removed_manager_survives() -> None:
    """SPEC §7.3: A crashing handler is removed; the gen_event manager stays alive.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html
    """
    manager = await GenEvent.start()
    exploder = Exploder()
    survivor = Recorder()
    await manager.add_handler(exploder)
    await manager.add_handler(survivor, tag="s")

    # First event: exploder crashes, survivor handles
    await manager.sync_notify("event-1")

    handlers = await manager.which_handlers()
    assert handlers == [survivor]
    assert isinstance(exploder.terminated_with, tuple)
    assert exploder.terminated_with[0] == "error"
    assert isinstance(exploder.terminated_with[1], RuntimeError)
    assert survivor.events == ["event-1"]

    # Second event: manager is still alive and survivor keeps working
    await manager.sync_notify("event-2")
    assert survivor.events == ["event-1", "event-2"]
    assert not manager.has_stopped()

    await manager.stop("normal")


async def test_7_handler_returning_remove_handler_is_dropped() -> None:
    """A handler returning `RemoveHandler()` from `handle_event` is removed."""
    manager = await GenEvent.start()
    leaver = RemoveOnEvent()
    stayer = Recorder()
    await manager.add_handler(leaver)
    await manager.add_handler(stayer, tag="keep")

    await manager.sync_notify("go")

    handlers = await manager.which_handlers()
    assert handlers == [stayer]
    assert leaver.terminated_with == "remove_handler"
    assert stayer.events == ["go"]

    await manager.stop("normal")


async def test_7_call_handler_routes_to_specific_handler() -> None:
    """gen_event:call/3 delivers a request to one specific installed handler."""
    manager = await GenEvent.start()
    h1 = Recorder()
    h2 = Recorder()
    await manager.add_handler(h1, tag="a")
    await manager.add_handler(h2, tag="b")

    reply = await manager.call_handler(h2, "query")
    assert reply == ("reply", "b", "query")

    with pytest.raises(HandlerNotInstalled):
        await manager.call_handler(RemoveOnEvent(), "query")

    await manager.stop("normal")


async def test_7_notify_is_async_and_dispatches_in_install_order() -> None:
    """`notify` is fire-and-forget, but events still reach handlers in install order."""
    manager = await GenEvent.start()
    h1 = Recorder()
    h2 = Recorder()
    await manager.add_handler(h1, tag="1")
    await manager.add_handler(h2, tag="2")

    manager.notify("async-event")
    # Use sync_notify as a barrier — it runs after the cast drains.
    await manager.sync_notify("barrier")

    assert h1.events == ["async-event", "barrier"]
    assert h2.events == ["async-event", "barrier"]

    await manager.stop("normal")
