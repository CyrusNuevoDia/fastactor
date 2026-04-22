"""
Lesson 04: Two-phase work with `handle_continue`.

An AI agent often needs to reply to a caller quickly, THEN do post-work before
it looks at the next message -- "plan, reply, reflect" or "serve, reply, log".
You could chain awaits inside a single `handle_call`, but that pauses the
mailbox for both phases. `Continue` lets you split them: reply now, then run
the second phase with the mailbox still paused, before the next message.

There are two places `Continue` appears:
  1. `init(...)` returns `Continue(term)` -> `handle_continue(term)` runs
     before the first mailbox read. Useful for "set up state, then do post-init
     work that requires the full init to be done first."
  2. `handle_call(...)` returns `(reply, Continue(term))` -> the caller gets
     `reply` immediately, and `handle_continue(term)` runs next, before the
     next mailbox message is read.

Prior lessons: 02 (call/cast).
New concepts: `Continue`, `handle_continue`, init-returning-Continue,
              (reply, Continue) tuple return from handle_call.

Read the relevant source:
  - src/fastactor/otp/gen_server.py  (_do_handle_call + Continue matching)
  - src/fastactor/otp/_messages.py   (Continue dataclass)
"""

from typing import Any

import pytest
from helpers import ContinueServer

from fastactor.otp import Call, Continue, GenServer

pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────────────────
# A "plan then reflect" server. handle_call returns (reply, Continue(term)).
# The caller gets the plan back immediately. The server then runs
# handle_continue("reflect", plan) before the next incoming message -- this is
# where you'd update an internal log, kick off background work, etc.
# ─────────────────────────────────────────────────────────────────────────────


class PlanAndReflect(GenServer):
    async def init(self) -> None:
        self.plans_made: list[str] = []
        self.reflections: list[str] = []

    async def handle_call(self, call: Call) -> tuple[str, Continue] | list[str]:
        match call.message:
            case ("plan", query):
                plan = f"plan({query})"
                self.plans_made.append(plan)
                return plan, Continue(("reflect", plan))
            case "snapshot":
                return list(self.reflections)
            case _:
                raise ValueError(call.message)

    async def handle_continue(self, term: Any) -> None:
        match term:
            case ("reflect", plan):
                self.reflections.append(f"reflection-on:{plan}")


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_init_continue_runs_before_first_mailbox_read():
    """G: ContinueServer. W: init returns Continue("boot"). T: handle_continue ran before handle_call.

    ContinueServer records the ordering in self.order. If init -> Continue
    worked correctly, the list reads: "init", then ("handle_continue", "boot"),
    then ("handle_call", ...). Any mailbox message would arrive AFTER the
    continuation.
    """
    server = await ContinueServer.start(payload="boot")

    order = await server.call("first-message")

    assert order == [
        "init",
        ("handle_continue", "boot"),
        ("handle_call", "first-message"),
    ]

    await server.stop("normal")


async def test_handle_call_returning_reply_and_continue_replies_first():
    """G: a PlanAndReflect server. W: call(("plan", "x")). T: caller gets the plan, then reflection runs."""
    server = await PlanAndReflect.start()

    plan = await server.call(("plan", "user-query"))
    assert plan == "plan(user-query)"
    # At this moment `handle_continue` may not have run yet -- but it MUST
    # complete before the next mailbox message is handled. So a subsequent
    # call() acts as a synchronization point that proves the continuation ran.
    reflections = await server.call("snapshot")
    assert reflections == ["reflection-on:plan(user-query)"]

    await server.stop("normal")


async def test_continuation_runs_before_next_message():
    """G: PlanAndReflect. W: two plans in a row. T: each plan's reflection runs before the next plan begins.

    Sequence (by mailbox ordering):
      plan A  ->  reply A  ->  reflect A  ->  plan B  ->  reply B  ->  reflect B
    The key invariant: no message between "plan A" and "plan B" can interleave
    "reflect A". This is stronger than what a plain chain of awaits would give
    you -- it's enforced by the mailbox loop itself.
    """
    server = await PlanAndReflect.start()

    await server.call(("plan", "A"))
    await server.call(("plan", "B"))

    reflections = await server.call("snapshot")
    assert reflections == ["reflection-on:plan(A)", "reflection-on:plan(B)"]
    assert server.plans_made == ["plan(A)", "plan(B)"]

    await server.stop("normal")


# Next: failure. Everything we've built assumes the happy path. Lesson 05
# shows what happens when a handler raises -- which happens a lot when your
# agent is calling flaky LLM APIs or third-party tools -- and why an actor
# crashing is different from, say, an uncaught exception tearing down a task
# group.
