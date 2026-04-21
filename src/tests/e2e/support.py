import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from anyio import fail_after, sleep

from fastactor.otp import Call, GenServer


@dataclass(frozen=True)
class Ask:
    prompt: str


@dataclass(frozen=True)
class AgentReply:
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class FakeAgent(GenServer):
    """Simulated LLM-style agent: sleeps to mimic latency, can be configured to crash."""

    async def init(
        self,
        *,
        label: str,
        handler: Callable[[str], str] | None = None,
        latency: float = 0.0,
        fail_after: int | None = None,
        freed: set[str] | None = None,
    ) -> None:
        self.label = label
        self.handler = handler or (lambda p: f"echo({p})")
        self.latency = latency
        self.fail_after = fail_after
        self.freed = freed
        self._calls = 0

    async def handle_call(self, call: Call) -> Any:
        if not isinstance(call.message, Ask):
            raise ValueError(f"{self.label} got unknown call: {call.message!r}")
        if self.latency > 0:
            await sleep(self.latency)
        self._calls += 1
        if self.fail_after is not None and self._calls > self.fail_after:
            raise RuntimeError(f"{self.label} failed after {self._calls} calls")
        return AgentReply(
            text=self.handler(call.message.prompt),
            meta={"agent": self.label, "calls": self._calls},
        )

    async def terminate(self, reason: Any) -> None:
        if self.freed is not None:
            self.freed.add(self.label)
        await super().terminate(reason)


async def await_value[T](
    fn: Callable[[], T | Awaitable[T]],
    predicate: Callable[[T], bool],
    timeout: float = 2.0,
    interval: float = 0.01,
) -> T:
    """Poll fn() until predicate(value) is true; TimeoutError after `timeout`."""

    async def _call() -> T:
        outcome = fn()
        if inspect.isawaitable(outcome):
            return cast(T, await outcome)
        return cast(T, outcome)

    with fail_after(timeout):
        while True:
            value = await _call()
            if predicate(value):
                return value
            await sleep(interval)
