"""
Lesson 07: Supervisors heal.

You now have: actors, messages, crashes-are-isolated, and the raw signals to
know when something died (links, monitors). The obvious next question: who
restarts a crashed worker? Writing that by hand looks like

    while True:
        try:
            await run_worker()
        except Exception:
            log.warning("worker died, restarting")
            await sleep(1)

...which works until you want: "only restart if it wasn't a graceful stop",
or "restart the whole sibling group when one dies", or "if we restart more
than 3 times in 5 seconds, give up and bubble the failure up." Those rules
compound quickly. OTP's Supervisor has already solved this and its policies
are declarative.

A Supervisor holds `ChildSpec`s: one per child, describing how to start it,
when to restart it, and how to shut it down. Restart policies:

  - `permanent` : always restart (default for long-running workers)
  - `transient` : restart only if the crash was abnormal (not "normal"/"shutdown")
  - `temporary` : never restart (one-shot jobs)

Strategies (when ONE child dies, what happens to its siblings?):

  - `one_for_one`  : restart only the dead one (default)
  - `one_for_all`  : restart every child
  - `rest_for_one` : restart the dead one + every spec AFTER it in the list

Intensity: if `max_restarts` restarts happen within `max_seconds`, the
Supervisor itself crashes -- fail-stop, which is the RIGHT behavior. Crash
loops should escalate, not hide.

Prior lessons: 05 (crash isolation), 06 (monitors).
New concepts: `Supervisor`, `ChildSpec` (via `sup.child_spec(...)`),
              restart policies, strategies, `max_restarts`/`max_seconds`,
              `await_child_restart` for deterministic restart checks.

Read the relevant source:
  - src/fastactor/otp/supervisor.py
  - src/tests/otp/helpers.py  (await_child_restart helper)
"""

from typing import cast

from anyio import fail_after
from anyio.lowlevel import checkpoint
import pytest
from ..otp.helpers import BoomServer, CounterServer, await_child_restart

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_permanent_child_is_restarted_after_a_crash(make_supervisor):
    """G: a one_for_one supervisor with a BoomServer child. W: child crashes. T: supervisor restarts it.

    The restarted process has a NEW identity -- same spec id, different Process
    instance. `await_child_restart` proves that by identity-comparing against
    the old reference.
    """
    sup = await make_supervisor(strategy="one_for_one")
    old = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    with pytest.raises(RuntimeError, match="Boom!"):
        await old.call("boom")
    await old.stopped()

    new = cast(BoomServer, await await_child_restart(sup, "worker", old))

    assert new is not old
    # The fresh worker is healthy -- no residual damage from the crash.
    assert await new.call("hello") == "ok"


async def test_temporary_child_is_not_restarted(make_supervisor):
    """G: a temporary child. W: it crashes. T: no restart happens; spec still present but process is gone."""
    sup = await make_supervisor()
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="temporary")
    )

    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await child.stopped()

    # Supervisor stays up, but this child is not replaced. which_children
    # still lists the spec; the process slot is ":undefined".
    rows = sup.which_children()
    assert rows == [
        {
            "id": "worker",
            "process": ":undefined",
            "restart": "temporary",
            "shutdown": 5,
            "type": "worker",
            "significant": False,
        }
    ]


async def test_one_for_all_strategy_restarts_every_sibling(make_supervisor):
    """G: a one_for_all supervisor with two children. W: one crashes. T: both are restarted.

    This is the "if one goes, they all go" strategy -- useful when children
    share lifecycle state (e.g. a DB connection pool and the workers using it).
    """
    sup = await make_supervisor(strategy="one_for_all")
    crashy = await sup.start_child(
        sup.child_spec("crashy", BoomServer, restart="permanent")
    )
    peer = await sup.start_child(
        sup.child_spec("peer", CounterServer, restart="permanent")
    )

    with pytest.raises(RuntimeError, match="Boom!"):
        await crashy.call("boom")
    await crashy.stopped()

    # BOTH children are replaced with new instances.
    new_crashy = await await_child_restart(sup, "crashy", crashy)
    new_peer = await await_child_restart(sup, "peer", peer)
    assert new_crashy is not crashy
    assert new_peer is not peer


async def test_intensity_exceeded_crashes_the_supervisor(make_supervisor):
    """G: max_restarts=2 over 5s. W: we crash the child 3 times in a row. T: supervisor gives up and exits.

    The right response to "this worker won't stay up" is to escalate, not to
    spin forever. The supervisor crashing is a SIGNAL to whoever supervises
    the supervisor (possibly the root runtime supervisor) that something
    deeper is wrong.
    """
    sup = await make_supervisor(max_restarts=2, max_seconds=5)
    child = await sup.start_child(
        sup.child_spec("worker", BoomServer, restart="permanent")
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="Boom!"):
            await child.call("boom")
        await child.stopped()
        child = cast(BoomServer, await await_child_restart(sup, "worker", child))

    # Third crash pushes us over the intensity limit.
    with pytest.raises(RuntimeError, match="Boom!"):
        await child.call("boom")
    await child.stopped()

    with fail_after(2):
        while not sup.has_stopped():
            await checkpoint()
    assert sup.has_stopped()


# Next: sometimes you don't know your children at startup time. Each user
# that connects needs their own agent spawned on-the-fly. Lesson 08 covers
# `DynamicSupervisor` -- same policies, but children added/removed at runtime.
