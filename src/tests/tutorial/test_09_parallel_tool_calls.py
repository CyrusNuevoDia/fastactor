"""
Lesson 09: Parallel tool calls with `Task` and `TaskSupervisor`.

An AI agent that calls one tool per step is slow. Agents that parallelize
-- fanning out to a search, a calculator, and a code interpreter at once --
are much faster. The question this lesson answers: what's the shape of
"spawn N concurrent jobs, collect results, survive partial failure"?

The "normal Python" answer is `asyncio.gather(*calls)`. It works until one
sibling raises: by default that cancels the other siblings. You can opt in to
`return_exceptions=True`, but now you have to sort wins from losses by hand.

`Task` is the actor-model equivalent: a one-shot coroutine wrapped as a
Process. It's awaitable -- `await task` returns the result or raises. Two
flavors:

  - `Task.start(fn)`       -- unlinked. Caller survives a Task crash.
  - `Task.start_link(fn)`  -- linked. Task crash cascades to caller (or to
                              `handle_exit` if caller trap_exits).

`TaskSupervisor` is a `DynamicSupervisor` that hands you pooled Tasks via
`sup.run(fn, *args)`. Every task is supervised, so orphan tracking is
automatic and a parent crash tears them down structurally.

Prior lessons: 05 (isolation), 08 (DynamicSupervisor).
New concepts: `Task.start`, `Task.start_link`, awaiting a Task,
              `TaskSupervisor.run`, Task.poll for non-raising polling.

Read the relevant source:
  - src/fastactor/otp/task.py
"""

import asyncio

import pytest

from fastactor.otp import Task, TaskSupervisor

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# Helper "tools" -- simulated async jobs with different behaviors.
# ─────────────────────────────────────────────────────────────────────────────


async def _tool_ok(name: str) -> str:
    await asyncio.sleep(0)
    return f"{name}-ok"


async def _tool_boom() -> str:
    await asyncio.sleep(0)
    raise RuntimeError("tool exploded")


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_normal_way_gather_cancels_on_one_failure():
    """G: 3 tool calls via asyncio.gather. W: one raises. T: gather raises; the siblings are cancelled."""
    with pytest.raises(RuntimeError, match="tool exploded"):
        await asyncio.gather(_tool_ok("a"), _tool_boom(), _tool_ok("b"))


async def test_the_actor_way_task_is_awaitable_and_returns_result():
    """G: Task.start wrapping an async function. W: we await it. T: we get the return value.

    A Task is just a Process whose body is "run this coroutine once and then
    stop." `await task` resolves to the function's return value -- or re-raises
    the exception if it crashed.
    """
    task = await Task.start(_tool_ok, "search")

    result = await task
    assert result == "search-ok"


async def test_tasks_run_concurrently_and_isolate_failures():
    """G: 3 Tasks, one crashes. W: we await the survivors first, then the crasher. T: survivors see their results.

    Tasks are Processes -- their lifetimes and failures are independent by
    default. This is the contrast with `asyncio.gather`: no implicit
    cancellation cascade.
    """
    a = await Task.start(_tool_ok, "a")
    boom = await Task.start(_tool_boom)
    b = await Task.start(_tool_ok, "b")

    assert await a == "a-ok"
    assert await b == "b-ok"

    with pytest.raises(RuntimeError, match="tool exploded"):
        await boom


async def test_task_supervisor_pools_supervised_tasks():
    """G: a TaskSupervisor. W: we spawn 5 jobs via run. T: all 5 resolve; partial failures don't affect peers.

    `TaskSupervisor.run(fn, *args)` is the everyday shape: a pool of Tasks
    you fan out to, all supervised, all isolated. Use this for parallel tool
    invocation in agents.
    """
    pool = await TaskSupervisor.start()

    tasks = [await pool.run(_tool_ok, f"t{i}") for i in range(5)]
    results = [await t for t in tasks]

    assert results == ["t0-ok", "t1-ok", "t2-ok", "t3-ok", "t4-ok"]

    # Adding a crasher alongside the winners doesn't poison the pool.
    crasher = await pool.run(_tool_boom)
    winner = await pool.run(_tool_ok, "survivor")

    with pytest.raises(RuntimeError, match="tool exploded"):
        await crasher
    assert await winner == "survivor-ok"

    await pool.stop("normal")


async def test_task_poll_without_raising_on_crash():
    """G: a Task that crashes. W: we poll it with a timeout. T: we get the Exception instance back, not a raise.

    `task.poll(timeout)` is the non-raising cousin of `await task`. Useful
    when you want to inspect the outcome without unwinding the current frame.
    Returns: the result on success, the exception object on crash, or None
    on timeout.
    """
    task = await Task.start(_tool_boom)
    outcome = await task.poll(timeout=1)

    assert isinstance(outcome, RuntimeError)
    assert "tool exploded" in str(outcome)


# Next: how do two agents find each other by name? We've been holding Process
# references by hand; that doesn't scale to a system where "the summarizer
# agent" is a moving target. Lesson 10 covers `Registry`.
