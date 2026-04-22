"""Race N speculator agents with first-success semantics.

Why this is uniquely actor-hard: speculator agents are long-lived stateful
processes (in reality: holding LLM subprocesses, loaded models, or partial
results). When the first one wins, the others must be torn down with their
`terminate()` hooks running — `asyncio.wait(FIRST_COMPLETED)` cancels Tasks
but leaves stateful resources dangling. And if one *crashes* instead of
losing, links propagate that failure to an observer that can abort the whole
race — no bookkeeping per call-site.
"""

import anyio
import pytest

from .helpers import AgentReply, Ask, FakeAgent
from fastactor.otp import DynamicSupervisor, Exit, GenServer

pytestmark = pytest.mark.anyio


class RaceObserver(GenServer):
    """A trap-exits GenServer: when a linked speculator dies abnormally, it sets abort."""

    async def init(self) -> None:
        self.abort = anyio.Event()
        self.exits: list[Exit] = []

    async def handle_exit(self, message: Exit) -> None:
        self.exits.append(message)
        self.abort.set()


async def _spawn_speculator(
    dsup: DynamicSupervisor,
    *,
    label: str,
    latency: float,
    fail_after: int | None,
    freed: set[str],
) -> FakeAgent:
    return await dsup.start_child(
        dsup.child_spec(
            label,
            FakeAgent,
            kwargs={
                "label": label,
                "handler": lambda p, n=label: f"{n}:{p}",
                "latency": latency,
                "fail_after": fail_after,
                "freed": freed,
            },
        )
    )


async def _drive_race(
    speculators: list[FakeAgent],
    prompt: str,
    abort: anyio.Event,
    per_call_timeout: float = 2.0,
) -> AgentReply | None:
    """Concurrent fan-out; first reply wins; `abort` cancels the whole race."""
    result: AgentReply | None = None

    async with anyio.create_task_group() as tg:

        async def run(spec: FakeAgent) -> None:
            nonlocal result
            try:
                reply = await spec.call(Ask(prompt), timeout=per_call_timeout)
            except BaseException:
                return
            if result is None and not abort.is_set():
                result = reply
                tg.cancel_scope.cancel()

        async def watch() -> None:
            await abort.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(watch)
        for s in speculators:
            tg.start_soon(run, s)

    return result


async def test_first_success_wins_and_all_speculators_terminate(runtime):
    """G: 3 speculators of varying speed. W: fastest returns. T: all terminate() hooks ran."""
    freed: set[str] = set()
    observer = await RaceObserver.start(trap_exits=True)
    dsup = await DynamicSupervisor.start()

    fast = await _spawn_speculator(
        dsup, label="fast", latency=0.02, fail_after=None, freed=freed
    )
    medium = await _spawn_speculator(
        dsup, label="medium", latency=0.15, fail_after=None, freed=freed
    )
    slow = await _spawn_speculator(
        dsup, label="slow", latency=0.3, fail_after=None, freed=freed
    )

    for s in (fast, medium, slow):
        observer.link(s)

    reply = await _drive_race([fast, medium, slow], "problem", observer.abort)

    assert reply is not None
    assert reply.meta["agent"] == "fast"
    assert not observer.abort.is_set()

    await dsup.stop("normal")

    assert freed == {"fast", "medium", "slow"}


async def test_crash_in_one_speculator_aborts_race_via_link(runtime):
    """G: a buggy speculator raises fastest. W: its Exit reaches the observer. T: race aborts, everyone cleaned up."""
    freed: set[str] = set()
    observer = await RaceObserver.start(trap_exits=True)
    dsup = await DynamicSupervisor.start()

    buggy = await _spawn_speculator(
        dsup, label="buggy", latency=0.01, fail_after=0, freed=freed
    )
    slow = await _spawn_speculator(
        dsup, label="slow", latency=0.5, fail_after=None, freed=freed
    )

    for s in (buggy, slow):
        observer.link(s)

    reply = await _drive_race(
        [buggy, slow], "problem", observer.abort, per_call_timeout=1.0
    )

    assert reply is None  # aborted before any winner reported

    with anyio.fail_after(1):
        await observer.abort.wait()
    assert len(observer.exits) == 1
    assert observer.exits[0].sender is buggy

    await dsup.stop("normal")
    assert freed == {"buggy", "slow"}


async def test_race_times_out_cleanly(runtime):
    """G: every speculator too slow. W: outer move_on_after fires. T: no winner, all terminate() hooks still ran."""
    freed: set[str] = set()
    dsup = await DynamicSupervisor.start()

    speculators: list[FakeAgent] = []
    for i in range(3):
        speculators.append(
            await _spawn_speculator(
                dsup,
                label=f"slow-{i}",
                latency=1.0,
                fail_after=None,
                freed=freed,
            )
        )

    abort = anyio.Event()
    result: AgentReply | None = None
    with anyio.move_on_after(0.05):
        result = await _drive_race(speculators, "x", abort, per_call_timeout=1.0)

    assert result is None

    await dsup.stop("normal")
    assert freed == {"slow-0", "slow-1", "slow-2"}
