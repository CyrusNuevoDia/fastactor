"""Worker pool with per-worker failure isolation, restart intensity, and backpressure.

Why this is uniquely actor-hard: a pool processing a stream of jobs where some
cause unrecoverable crashes. `DynamicSupervisor` restarts crashed workers so
the pool self-heals; `max_restarts`/`max_seconds` escalates when failures are
catastrophic; and every `GenServer.call` serializes through a bounded mailbox
— that's backpressure for free. The equivalent in plain asyncio is a queue +
semaphore + exception-handling wrapper that's written every time from scratch.
"""

import time
from dataclasses import dataclass
from typing import cast

import anyio
import pytest

from fastactor.otp import Call, DynamicSupervisor, GenServer, Supervisor

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class Job:
    value: int
    poison: bool = False


class Worker(GenServer):
    async def init(
        self,
        *,
        label: str,
        latency: float = 0.0,
        completed: dict[str, int] | None = None,
    ) -> None:
        self.label = label
        self.latency = latency
        self.completed = completed

    async def handle_call(self, call: Call):
        job = call.message
        if not isinstance(job, Job):
            raise ValueError(f"unknown job: {job!r}")
        if self.latency > 0:
            await anyio.sleep(self.latency)
        if job.poison:
            raise RuntimeError(f"poison job value={job.value}")
        if self.completed is not None:
            self.completed[self.label] = self.completed.get(self.label, 0) + 1
        return job.value * 2


async def _spawn_pool(
    *,
    size: int,
    latency: float = 0.0,
    max_restarts: int = 10,
    max_seconds: float = 5.0,
    completed: dict[str, int] | None = None,
) -> DynamicSupervisor:
    dsup = await DynamicSupervisor.start(
        max_restarts=max_restarts,
        max_seconds=max_seconds,
    )
    for i in range(size):
        await dsup.start_child(
            Supervisor.child_spec(
                f"w-{i}",
                Worker,
                kwargs={"label": f"w-{i}", "latency": latency, "completed": completed},
                restart="permanent",
            )
        )
    return dsup


async def _dispatch(dsup: DynamicSupervisor, job: Job, retries: int = 2):
    """Hash-to-worker, retry on crash. Uses job.value as the dispatch key so concurrent jobs spread."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        children = list(dsup.children.values())
        if not children:
            await anyio.sleep(0.01)
            last_err = RuntimeError("no live workers")
            continue
        worker = cast(Worker, children[(job.value + attempt) % len(children)].process)
        try:
            return await worker.call(job, timeout=5)
        except Exception as e:
            last_err = e
            if attempt < retries:
                await anyio.sleep(0.01)
                continue
    assert last_err is not None
    raise last_err


async def test_poison_pill_crashes_are_isolated(runtime):
    """G: 3-worker pool, mix of good and poison jobs. W: submitted concurrently. T: all good complete, pool self-heals."""
    completed: dict[str, int] = {}
    dsup = await _spawn_pool(size=3, latency=0.01, completed=completed)

    good = [Job(value=i) for i in range(12)]
    poison = [Job(value=99, poison=True), Job(value=100, poison=True)]

    good_replies: list[int] = []
    poison_errors: list[Exception] = []

    async def run_job(j: Job) -> None:
        try:
            r = await _dispatch(dsup, j)
            good_replies.append(r)
        except Exception as e:
            poison_errors.append(e)

    async with anyio.create_task_group() as tg:
        for j in [*good, *poison]:
            tg.start_soon(run_job, j)

    assert sorted(good_replies) == [i * 2 for i in range(12)]
    assert len(poison_errors) == 2

    with anyio.fail_after(1):
        while len(dsup.children) < 3:
            await anyio.sleep(0.01)
    assert len(dsup.children) == 3
    assert sum(completed.values()) == 12

    await dsup.stop("normal")


async def test_restart_intensity_escalates(runtime):
    """G: tight max_restarts, many poison jobs. W: crashes exceed threshold. T: dsup terminates."""
    dsup = await _spawn_pool(size=2, latency=0.001, max_restarts=2, max_seconds=2)

    async def submit_poison():
        for _ in range(10):
            try:
                await _dispatch(dsup, Job(value=0, poison=True), retries=0)
            except Exception:
                pass
            await anyio.sleep(0.01)

    async with anyio.create_task_group() as tg:
        tg.start_soon(submit_poison)

    with anyio.fail_after(3):
        await dsup.stopped()
    assert dsup.has_stopped()


async def test_backpressure_serializes_producer(runtime):
    """G: 2 slow workers, many fast-fired jobs. W: concurrent submits. T: wall-clock ≈ N × L / K."""
    per_job_latency = 0.03
    k = 2
    n = 8
    dsup = await _spawn_pool(size=k, latency=per_job_latency)

    start = time.perf_counter()
    async with anyio.create_task_group() as tg:
        for i in range(n):
            tg.start_soon(_dispatch, dsup, Job(value=i))
    elapsed = time.perf_counter() - start

    ideal = n * per_job_latency / k
    assert ideal <= elapsed <= ideal * 2.5, f"elapsed={elapsed:.3f}s ideal={ideal:.3f}s"

    await dsup.stop("normal")


async def test_scale_up_increases_parallelism(runtime):
    """G: same job burst routed through pools of size 1 vs 5. W: both run. T: 5-worker pool is ~5× faster."""
    per_job_latency = 0.02
    n = 10

    async def run_with_size(k: int) -> float:
        dsup = await _spawn_pool(size=k, latency=per_job_latency)
        start = time.perf_counter()
        async with anyio.create_task_group() as tg:
            for i in range(n):
                tg.start_soon(_dispatch, dsup, Job(value=i))
        elapsed = time.perf_counter() - start
        await dsup.stop("normal")
        return elapsed

    t_one = await run_with_size(1)
    t_five = await run_with_size(5)

    assert t_five < t_one * 0.5, f"t_one={t_one:.3f}s t_five={t_five:.3f}s"
