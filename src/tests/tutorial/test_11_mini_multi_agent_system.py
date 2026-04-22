"""
Lesson 11: Putting it all together -- a mini multi-agent system.

This lesson composes every primitive you've met so far into a small,
realistic shape: a router that receives queries tagged with a user id, a
dynamic pool of per-user worker agents, and a registry mapping user ids to
worker processes. It's a miniature of the production pattern you'd use for
"one conversational agent per connected user."

What you should watch for:

  1. Workers start with `via=("agents", user_id)` -- registration happens in
     init. When the supervisor restarts a dead worker, the replacement
     re-registers under the same key automatically. This is the invariant
     that makes "crash -> supervisor heals -> router still works" a
     zero-code property.

  2. The Router uses `whereis((registry, key))` to find the right worker at
     call time. If none exists, it starts one. The router never holds a
     stale reference -- the registry is the source of truth.

  3. Partial failure is survivable: crash a worker, the supervisor restarts
     it (permanent restart), the registry rebinds the new process under the
     same key, and the next query routes correctly.

Prior lessons: all of 00-10.
New concepts: none -- this is the integration.

Read the relevant source:
  - everything in src/fastactor/otp/
"""

from typing import Any, cast

import pytest
from helpers import await_child_restart

from fastactor.otp import (
    Call,
    DynamicSupervisor,
    GenServer,
    Registry,
    whereis,
)

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# The Worker: one per user session. Echoes queries with a user-scoped prefix.
# ─────────────────────────────────────────────────────────────────────────────


class Worker(GenServer):
    async def init(self, user_id: str) -> None:
        self.user_id = user_id

    async def handle_call(self, call: Call) -> str:
        match call.message:
            case "boom":
                raise RuntimeError("worker crashed")
            case ("query", query):
                return f"[{self.user_id}] {query}"
        raise ValueError(call.message)


# ─────────────────────────────────────────────────────────────────────────────
# The Router: finds-or-spawns a Worker for the given user id, forwards the
# query, and returns the worker's reply. It holds a reference to the pool,
# but lookups go through the registry -- so a restarted worker is found
# transparently.
# ─────────────────────────────────────────────────────────────────────────────


class Router(GenServer):
    async def init(self, pool: DynamicSupervisor) -> None:
        self.pool = pool

    async def handle_call(self, call: Call) -> Any:
        match call.message:
            case ("route", user_id, query):
                proc = await whereis(("agents", user_id))
                if proc is None:
                    await self.pool.start_child(
                        DynamicSupervisor.child_spec(
                            f"worker-{user_id}",
                            Worker,
                            kwargs={
                                "user_id": user_id,
                                "via": ("agents", user_id),
                            },
                            restart="permanent",
                        )
                    )
                    proc = await whereis(("agents", user_id))
                assert proc is not None
                worker = cast(Worker, proc)
                return await worker.call(("query", query))
        raise ValueError(call.message)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_router_spawns_a_worker_on_first_contact():
    """G: a router + empty pool + registry. W: route("alice", "hello"). T: a worker is spawned and replies."""
    await Registry.new("agents", "unique")
    pool = await DynamicSupervisor.start()
    router = await Router.start(pool)

    reply = await router.call(("route", "alice", "hello"))
    assert reply == "[alice] hello"

    # The worker is registered and retrievable by key.
    worker = await whereis(("agents", "alice"))
    assert worker is not None

    await router.stop("normal")
    await pool.stop("normal")


async def test_router_reuses_the_same_worker_across_calls():
    """G: a router + pool + registry. W: two calls for "bob". T: both are served by the same Worker instance."""
    await Registry.new("agents", "unique")
    pool = await DynamicSupervisor.start()
    router = await Router.start(pool)

    await router.call(("route", "bob", "first"))
    worker = await whereis(("agents", "bob"))
    await router.call(("route", "bob", "second"))
    still = await whereis(("agents", "bob"))
    assert worker is still

    await router.stop("normal")
    await pool.stop("normal")


async def test_crashed_worker_is_replaced_and_routing_still_works():
    """G: a routed-to worker. W: we crash it. T: supervisor restarts it, via= re-registers, router routes again.

    This is the payoff: you wrote no crash-handling code in the Router. The
    supervisor's `permanent` restart policy + the worker's `via=` spawn
    argument combine to heal the system without any coordination between
    them. The Router just asks `whereis` at every call and always sees the
    currently-alive worker.
    """
    await Registry.new("agents", "unique")
    pool = await DynamicSupervisor.start()
    router = await Router.start(pool)

    # First call spawns the worker.
    assert await router.call(("route", "carol", "hi")) == "[carol] hi"
    found = await whereis(("agents", "carol"))
    assert found is not None
    worker = cast(Worker, found)

    # Crash the worker directly.
    with pytest.raises(RuntimeError, match="worker crashed"):
        await worker.call("boom")
    await worker.stopped()

    # Supervisor restarts it. The replacement runs init with the same kwargs,
    # so via= re-registers it under "carol".
    new_worker = await await_child_restart(pool, "worker-carol", worker)
    assert new_worker is not worker

    # Registry points at the new process; the router finds it transparently.
    assert await whereis(("agents", "carol")) is new_worker
    assert await router.call(("route", "carol", "again")) == "[carol] again"

    await router.stop("normal")
    await pool.stop("normal")


async def test_multiple_users_get_independent_workers():
    """G: a router, pool, registry. W: route for 3 users. T: each gets a distinct Worker identity."""
    await Registry.new("agents", "unique")
    pool = await DynamicSupervisor.start()
    router = await Router.start(pool)

    await router.call(("route", "u1", "a"))
    await router.call(("route", "u2", "b"))
    await router.call(("route", "u3", "c"))

    w1 = await whereis(("agents", "u1"))
    w2 = await whereis(("agents", "u2"))
    w3 = await whereis(("agents", "u3"))

    assert w1 is not None and w2 is not None and w3 is not None
    assert len({w1, w2, w3}) == 3

    await router.stop("normal")
    await pool.stop("normal")


# You've reached the end of the guided tour. You've now used every primitive
# in fastactor's core public surface: Process, GenServer, Agent, Task and
# TaskSupervisor, Supervisor, DynamicSupervisor, Registry, Runtime, links,
# monitors, trap_exits, and handle_continue.
#
# What's not covered here:
#   - GenStage / GenStateMachine / GenEvent -- advanced streaming / FSM /
#     pub-sub primitives. See src/tests/otp/test_gen_stage.py,
#     test_gen_state_machine.py, and test_gen_event.py.
#   - Runtime entry points (fastactor.run(main), SIGINT/SIGTERM traps) --
#     see src/tests/otp/test_runtime_entrypoints.py.
#
# Suggested further reading:
#   - src/fastactor/otp/README.md for the per-primitive deep dive
#   - llms.txt for the Elixir <-> Python mental model table
#   - Joe Armstrong's thesis "Making Reliable Distributed Systems in the
#     Presence of Software Errors" (2003) for the original ideas.
#
# Go build something that doesn't fall over.
