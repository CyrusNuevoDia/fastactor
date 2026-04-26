from threading import Lock
from time import monotonic
from typing import Any

from anyio import Event, fail_after
from anyio.lowlevel import checkpoint

from fastactor.otp import Call, Cast, Continue, Down, Exit, GenServer, Process, Shutdown


class EchoServer(GenServer):
    async def init(self) -> None:
        self.log: list[Any] = []

    async def handle_call(self, call: Call) -> Any:
        return call.message

    async def handle_cast(self, cast: Cast) -> None:
        self.log.append(cast.message)


class CounterServer(GenServer):
    async def init(self, count: int = 0) -> None:
        self.count = count

    async def handle_call(self, call: Call) -> int | None:
        if call.message == "get":
            return self.count
        if call.message == "sync":
            return self.count
        raise ValueError(call.message)

    async def handle_cast(self, cast: Cast) -> None:
        match cast.message:
            case ("add", amount):
                self.count += amount
            case ("sub", amount):
                self.count -= amount
            case ("mul", amount):
                self.count *= amount
            case ("reset", amount):
                self.count = amount
            case _:
                raise ValueError(cast.message)


class BoomServer(GenServer):
    async def init(self, on_init: bool = False) -> None:
        if on_init:
            raise RuntimeError("Boom during init")

    async def handle_call(self, call: Call) -> str:
        if call.message == "boom":
            raise RuntimeError("Boom!")
        return "ok"


class SlowStopServer(GenServer):
    async def terminate(self, reason: str | Shutdown | Exception) -> None:
        deadline = monotonic() + 0.05
        while monotonic() < deadline:
            await checkpoint()
        await super().terminate(reason)


class MonitorServer(GenServer):
    async def init(self) -> None:
        self.down_msgs: list[Down] = []
        self._events = Event()

    async def handle_down(self, message: Down) -> None:
        self.down_msgs.append(message)
        self._events.set()

    async def await_events(self, n: int, timeout: float = 2) -> list[Down]:
        with fail_after(timeout):
            while len(self.down_msgs) < n:
                await self._events.wait()
                self._events = Event()
        return list(self.down_msgs)


class LinkServer(GenServer):
    async def init(self) -> None:
        self.exits_received: list[Exit] = []
        self._events = Event()

    async def handle_exit(self, message: Exit) -> None:
        self.exits_received.append(message)
        self._events.set()

    async def await_events(self, n: int, timeout: float = 2) -> list[Exit]:
        with fail_after(timeout):
            while len(self.exits_received) < n:
                await self._events.wait()
                self._events = Event()
        return list(self.exits_received)


class ContinueServer(GenServer):
    async def init(self, payload: Any = "boot") -> Continue:
        self.order: list[Any] = ["init"]
        return Continue(payload)

    async def handle_continue(self, term: Any) -> None:
        self.order.append(("handle_continue", term))

    async def handle_call(self, call: Call) -> list[Any]:
        self.order.append(("handle_call", call.message))
        return list(self.order)


class OrderLog:
    _lock = Lock()
    _events: list[str] = []

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._events.clear()

    @classmethod
    def record(cls, label: str) -> None:
        with cls._lock:
            cls._events.append(label)

    @classmethod
    def snapshot(cls) -> list[str]:
        with cls._lock:
            return list(cls._events)


async def await_child_restart(
    sup: Any,
    cid: str,
    old_proc: Process,
    timeout: float = 2,
) -> Process:
    with fail_after(timeout):
        while True:
            running_child = sup.children.get(cid)
            if running_child is not None and running_child.process is not old_proc:
                return running_child.process
            await checkpoint()
