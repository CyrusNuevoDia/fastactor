"""
Lesson 08: `DynamicSupervisor` -- one agent per user session.

The `Supervisor` from Lesson 07 is great when you know your children up
front -- "start 3 workers and a logger and a planner." But a real AI-agent
deployment mostly doesn't work that way. Users connect. A fresh per-user
agent gets spawned. Later, the user disconnects, or the agent crashes, and
you need to reap it. Repeat forever.

`DynamicSupervisor` is the shape for that: same restart mechanics, `one_for_one`
only, children added/removed via `start_child(...)` at runtime. Two features
matter most for per-user scenarios:

  - `max_children` : cap the population (backpressure -- when you hit the cap,
                     `start_child` raises `Failed`, and the caller can decide
                     what to do).
  - `extra_arguments` : tuple prepended to every child's start args. Useful
                        when every child needs the same shared thing (e.g. a
                        pool handle, a config object, a parent agent id).

The default restart for a DynamicSupervisor child is `temporary` -- a dead
child stays dead unless you explicitly ask for `permanent` or `transient`.
This matches the "one session per child" mental model.

The "normal Python" way: a `dict[user_id, asyncio.Task]` + manual cleanup.
Works for toy scale; leaks for real: if the task crashes, the dict entry stays,
and you don't notice until you try to talk to a ghost.

Prior lessons: 07 (Supervisor).
New concepts: `DynamicSupervisor`, `start_child`, `max_children`,
              `extra_arguments`, default `temporary` restart.

Read the relevant source:
  - src/fastactor/otp/dynamic_supervisor.py
"""

import pytest
from support import CounterServer

from fastactor.otp import DynamicSupervisor, Failed

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_starts_children_on_demand():
    """G: an empty DynamicSupervisor. W: we start 3 children at different times. T: all 3 are live.

    Passing `None` as the child id lets the DynamicSupervisor auto-generate a
    unique id per child. That's exactly what you want when children represent
    sessions or requests -- you rarely have a meaningful name in advance.
    """
    sup = await DynamicSupervisor.start()

    alice = await sup.start_child(
        DynamicSupervisor.child_spec(None, CounterServer, kwargs={"count": 10})
    )
    bob = await sup.start_child(
        DynamicSupervisor.child_spec(None, CounterServer, kwargs={"count": 20})
    )
    carol = await sup.start_child(
        DynamicSupervisor.child_spec(None, CounterServer, kwargs={"count": 30})
    )

    assert await alice.call("get") == 10
    assert await bob.call("get") == 20
    assert await carol.call("get") == 30

    await sup.stop("normal")


async def test_default_restart_is_temporary_so_dead_children_stay_dead():
    """G: a DynamicSupervisor with a default-restart child. W: it exits normally. T: no restart, no entry."""
    sup = await DynamicSupervisor.start()
    session = await sup.start_child(
        DynamicSupervisor.child_spec("session-1", CounterServer)
    )

    await session.stop("normal")

    # Session ended; supervisor reaped the entry. No ghost reference left.
    assert "session-1" not in sup.children

    await sup.stop("normal")


async def test_max_children_provides_backpressure():
    """G: max_children=2. W: we try to spawn a 3rd. T: start_child raises Failed -- caller must cope.

    This is the useful boundary for AI-agent deployments: caller gets a clean
    `Failed` exception when the cap is hit. The alternative (unbounded spawn)
    is an OOM waiting to happen.
    """
    sup = await DynamicSupervisor.start(max_children=2)

    await sup.start_child(
        DynamicSupervisor.child_spec(None, CounterServer, kwargs={"count": 1})
    )
    await sup.start_child(
        DynamicSupervisor.child_spec(None, CounterServer, kwargs={"count": 2})
    )

    with pytest.raises((Failed, ValueError), match="max"):
        await sup.start_child(
            DynamicSupervisor.child_spec(None, CounterServer, kwargs={"count": 3})
        )

    await sup.stop("normal")


async def test_extra_arguments_are_prepended_to_every_childs_init():
    """G: extra_arguments=(10,). W: we start a CounterServer with no args. T: init sees count=10.

    `CounterServer.init(count=0)` -- the extra argument 10 is prepended to the
    positional args, so init runs as `CounterServer.init(10)`. This is how you
    wire shared context into a pool of per-user agents without threading it
    through every call site.
    """
    sup = await DynamicSupervisor.start(extra_arguments=(10,))

    child = await sup.start_child(DynamicSupervisor.child_spec("c1", CounterServer))
    assert await child.call("get") == 10

    await sup.stop("normal")


# Next: per-request parallelism. A user query often fans out into N tool calls
# that all run simultaneously. Lesson 09 covers `Task` and `TaskSupervisor` --
# the "one-shot async job" primitive, with the same supervision guarantees.
