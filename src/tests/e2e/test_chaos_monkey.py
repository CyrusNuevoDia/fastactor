"""Multi-stage pipeline under chaos: random actors get killed; correctness preserved.

Why this is uniquely actor-hard: the Erlang value-prop compressed into a
pytest. A protected state-actor and a fleet of supervised, restartable
processors cooperate via idempotent claim-at-completion. Kill random
ephemeral actors throughout — the supervisor restarts them; the set-based
`done` tally deduplicates retries; the invariant `done == initial_tasks`
holds at the end. Try writing this with plain asyncio.
"""

import random
from dataclasses import dataclass

import anyio
import pytest

from fastactor.otp import Agent, Cast, Continue, DynamicSupervisor, GenServer, Process

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class Tick:
    pass


class Ingestor(GenServer):
    """Idempotently merges `initial` tasks into the queue. Safe under restart."""

    async def init(self, *, queue: Agent, initial: set[int]) -> None:
        def add(s, tasks=initial):
            return {
                "pending": s["pending"] | (tasks - s["done"] - s["pending"]),
                "done": s["done"],
            }

        await queue.update(add)


class Processor(GenServer):
    """Self-cast Tick loop: read a pending id, simulate work, atomically move to done."""

    async def init(self, *, queue: Agent, delay: float = 0.005) -> Continue:
        self.queue = queue
        self.delay = delay
        return Continue("boot")

    async def handle_continue(self, term) -> None:
        self.cast(Tick())

    async def handle_cast(self, cast: Cast) -> None:
        if not isinstance(cast.message, Tick):
            return
        pending = await self.queue.get(lambda s: sorted(s["pending"]))
        if pending:
            task_id = pending[0]
            await anyio.sleep(self.delay)

            def complete(s, tid=task_id):
                return {
                    "pending": s["pending"] - {tid},
                    "done": s["done"] | {tid},
                }

            await self.queue.update(complete)
        else:
            await anyio.sleep(self.delay)
        self.cast(Tick())


async def _build_pipeline(
    outer, *, n_tasks: int, n_processors: int, delay: float = 0.005
):
    queue = await outer.start_child(
        outer.child_spec(
            "queue",
            Agent,
            args=(lambda: {"pending": set(), "done": set()},),
            restart="permanent",
        )
    )
    inner = await outer.start_child(
        outer.child_spec(
            "inner",
            DynamicSupervisor,
            kwargs={"max_restarts": 500, "max_seconds": 60},
            restart="permanent",
        )
    )
    initial = set(range(n_tasks))
    ingestor = await inner.start_child(
        inner.child_spec(
            "ingestor",
            Ingestor,
            kwargs={"queue": queue, "initial": initial},
            restart="permanent",
        )
    )
    processors = []
    for i in range(n_processors):
        p = await inner.start_child(
            inner.child_spec(
                f"proc-{i}",
                Processor,
                kwargs={"queue": queue, "delay": delay},
                restart="permanent",
            )
        )
        processors.append(p)
    return queue, inner, ingestor, processors, initial


async def _wait_for_done(queue: Agent, expected: set[int], timeout: float) -> set[int]:
    with anyio.fail_after(timeout):
        while True:
            done = await queue.get(lambda s: set(s["done"]))
            if done >= expected:
                return done
            await anyio.sleep(0.02)


async def _chaos_loop(
    targets: list[Process],
    interval: float,
    duration: float,
    counter: list[int],
) -> None:
    """Run in parallel with the pipeline; kill a random live target every `interval`s for `duration`s."""
    deadline = anyio.current_time() + duration
    while anyio.current_time() < deadline:
        await anyio.sleep(interval)
        live = [t for t in targets if not t.has_stopped()]
        if not live:
            continue
        victim = random.choice(live)
        counter[0] += 1
        try:
            await victim.kill()
        except Exception:
            pass


async def test_chaos_monkey_tasks_under_fire(runtime, make_supervisor):
    """G: 30 tasks, 3 processors, chaos kills every 40ms. W: chaos + work overlap. T: done == initial and at least one processor got replaced by supervisor restart."""
    outer = await make_supervisor(max_restarts=500, max_seconds=60)
    queue, inner, _ingestor, processors, initial = await _build_pipeline(
        outer, n_tasks=30, n_processors=3, delay=0.01
    )
    initial_proc_ids = {p.id for p in processors}

    done = None
    kill_counter = [0]
    async with anyio.create_task_group() as tg:
        tg.start_soon(_chaos_loop, processors, 0.04, 0.8, kill_counter)
        done = await _wait_for_done(queue, initial, timeout=5)
        tg.cancel_scope.cancel()

    assert done == initial
    assert kill_counter[0] >= 2, f"chaos under-exercised: attempts={kill_counter[0]}"
    final_proc_ids = {
        inner.children[f"proc-{i}"].process.id
        for i in range(3)
        if f"proc-{i}" in inner.children
    }
    assert final_proc_ids - initial_proc_ids, (
        f"no processor was restarted by supervisor: "
        f"initial={initial_proc_ids} final={final_proc_ids}"
    )


async def test_queue_is_protected_from_chaos(runtime, make_supervisor):
    """G: chaos only targets ephemerals. W: run to completion. T: queue process identity unchanged."""
    outer = await make_supervisor(max_restarts=500, max_seconds=60)
    queue, inner, ingestor, processors, initial = await _build_pipeline(
        outer, n_tasks=20, n_processors=2, delay=0.005
    )
    queue_pid = queue.id

    kill_counter = [0]
    async with anyio.create_task_group() as tg:
        tg.start_soon(_chaos_loop, [ingestor, *processors], 0.05, 0.6, kill_counter)
        await _wait_for_done(queue, initial, timeout=5)
        tg.cancel_scope.cancel()

    assert outer.children["queue"].process.id == queue_pid
    assert not queue.has_stopped()


async def test_inner_supervisor_survives_chaos(runtime, make_supervisor):
    """G: chaos intensity within tolerance. W: inner supervisor supervises restarts. T: still alive at end."""
    outer = await make_supervisor(max_restarts=500, max_seconds=60)
    queue, inner, ingestor, processors, initial = await _build_pipeline(
        outer, n_tasks=20, n_processors=3, delay=0.005
    )

    kill_counter = [0]
    async with anyio.create_task_group() as tg:
        tg.start_soon(_chaos_loop, [ingestor, *processors], 0.08, 0.6, kill_counter)
        await _wait_for_done(queue, initial, timeout=5)
        tg.cancel_scope.cancel()

    assert not inner.has_stopped()
