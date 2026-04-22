"""
Lesson 10: Find agents by name with `Registry`.

So far every test has held onto Process references directly. Real systems
can't work that way: agents come and go, and "route this to the summarizer"
needs to resolve at the moment of the call, not at spawn time.

The "normal Python" answer is a global `dict[name, task]`. Two failure modes:
stale entries when a task dies and nobody cleans up, and no good way to
fan-out ("send this to all the workers listening on this topic").

`Registry` fixes both:
  - Entries auto-disappear when the registered process terminates.
  - Two modes: `unique` (one process per key) and `duplicate` (many per key,
    enabling pub/sub fan-out via `dispatch`).
  - `Registry.register` / `Registry.lookup` / `Registry.dispatch` are the
    direct API; `Process.start(via=(registry, key))` is the shorthand that
    does "spawn and register" in one step -- important for supervised
    children, because the registration happens as part of init, so a
    supervisor-restarted child re-registers automatically.

`Runtime.whereis(name_or_via)` is the universal lookup: accepts either a
global string name or a `(registry, key)` tuple.

Prior lessons: 08 (DynamicSupervisor).
New concepts: `Registry`, `Registry.new`, `register`, `lookup`, `dispatch`,
              `unique` vs `duplicate` mode, `via=` shorthand, `whereis`.

Read the relevant source:
  - src/fastactor/otp/registry.py
  - src/fastactor/otp/runtime.py  (whereis, name-vs-via)
"""

import pytest
from helpers import CounterServer

from fastactor.otp import Registry, whereis

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_unique_registry_maps_key_to_one_process():
    """G: a unique registry. W: we register a CounterServer under "summarizer". T: lookup returns exactly that process."""
    await Registry.new("agents", "unique")

    summarizer = await CounterServer.start()
    await Registry.register("agents", "summarizer", summarizer)

    found = await Registry.lookup("agents", "summarizer")
    assert found == [summarizer]

    await summarizer.stop("normal")


async def test_registry_auto_cleans_up_when_process_dies():
    """G: a registered process. W: the process stops. T: lookup returns [] -- no stale entries."""
    await Registry.new("agents", "unique")

    proc = await CounterServer.start()
    await Registry.register("agents", "ephemeral", proc)

    await proc.stop("normal")
    await proc.stopped()

    assert await Registry.lookup("agents", "ephemeral") == []


async def test_via_shorthand_registers_at_spawn_time():
    """G: a unique registry. W: CounterServer.start(via=(reg, key)). T: lookup and whereis both find it.

    `via=` is the pattern you'll use most often under a supervisor, because
    the registration happens during init -- so when the supervisor restarts
    a dead child, the replacement re-registers under the same key
    automatically, without you having to do anything.
    """
    await Registry.new("agents", "unique")

    worker = await CounterServer.start(via=("agents", "worker-1"))

    assert await Registry.lookup("agents", "worker-1") == [worker]
    assert await whereis(("agents", "worker-1")) is worker

    await worker.stop("normal")


async def test_duplicate_registry_enables_fanout_dispatch():
    """G: a duplicate registry with 3 subscribers. W: Registry.dispatch. T: the callback runs on every subscriber.

    `duplicate` mode is the pub/sub shape: many processes register under the
    same key (a "topic"), and `dispatch` fans a callback out to all of them.
    Exceptions in one callback are logged and swallowed; peers still run.
    """
    await Registry.new("topics", "duplicate")

    a = await CounterServer.start(count=0)
    b = await CounterServer.start(count=0)
    c = await CounterServer.start(count=0)
    await Registry.register("topics", "updates", a)
    await Registry.register("topics", "updates", b)
    await Registry.register("topics", "updates", c)

    async def bump(proc):
        proc.cast(("add", 10))

    await Registry.dispatch("topics", "updates", bump)
    # call-after-cast: drain each mailbox before we read.
    await a.call("sync")
    await b.call("sync")
    await c.call("sync")

    assert await a.call("get") == 10
    assert await b.call("get") == 10
    assert await c.call("get") == 10

    for p in (a, b, c):
        await p.stop("normal")


# Last one: let's put it all together. Lesson 11 wires a router, a registry,
# and a dynamic supervisor into a tiny multi-agent system -- the shape you'll
# reach for whenever you have "users connect, each needs their own agent,
# and the router must find the right one."
