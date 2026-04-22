"""Long-running compute with restart-resume via a separately supervised state actor.

Why this is uniquely actor-hard: a research loop that has accumulated findings
must NOT lose them when the compute half crashes. Putting the findings in a
sibling `Agent` under the same `one_for_one` supervisor means state survives
while compute restarts — and compute's `init` reads state to find its resume
point. In plain asyncio you'd reach for disk persistence or globals; here the
failure boundary is declared, not coded.
"""

# `anyio.sleep` is used in the compute loop to simulate work; deterministic
# waits elsewhere use `fail_after` + `poll_value`.
import anyio
import pytest
from support import await_child_restart

from e2e.support import poll_value
from fastactor.otp import Agent, GenServer, Supervisor, Task

pytestmark = pytest.mark.anyio


class ResearchCompute(GenServer):
    """Compute half of a split state/compute pair. Drives work in a linked Task; task crash → GenServer crash → supervisor restart."""

    async def init(
        self,
        *,
        state: Agent,
        total_steps: int,
        crash_flag: dict,
        done: anyio.Event,
    ) -> None:
        self.state = state
        self.total = total_steps
        self.crash_flag = crash_flag
        self.done = done
        existing = await state.get(lambda s: len(s))
        task = await Task.start(self._run, existing)
        task.link(self)

    async def _run(self, start: int) -> None:
        for step in range(start, self.total):
            crash_steps = self.crash_flag.get("steps", [])
            if step in crash_steps:
                crash_steps.remove(step)
                raise RuntimeError(f"simulated crash at step {step}")
            finding = f"finding-{step}"
            await self.state.update(lambda s, f=finding: [*s, f])
            await anyio.sleep(0.002)
        self.done.set()


async def _start_pair(
    sup: Supervisor,
    *,
    total_steps: int,
    crash_flag: dict,
    done: anyio.Event,
    max_restarts: int = 3,
) -> tuple:
    sup.max_restarts = max_restarts
    state = await sup.start_child(
        sup.child_spec("state", Agent, args=(lambda: [],), restart="permanent")
    )
    compute = await sup.start_child(
        sup.child_spec(
            "compute",
            ResearchCompute,
            kwargs={
                "state": state,
                "total_steps": total_steps,
                "crash_flag": crash_flag,
                "done": done,
            },
            restart="transient",
        )
    )
    return state, compute


async def test_compute_crash_resumes_without_duplicates(runtime, make_supervisor):
    """G: compute crashes once mid-run. W: supervisor restarts it. T: findings are complete, no dupes."""
    sup = await make_supervisor(max_restarts=3, max_seconds=10)
    done = anyio.Event()
    crash_flag = {"steps": [5]}
    state, compute = await _start_pair(
        sup, total_steps=10, crash_flag=crash_flag, done=done
    )

    restarted = await await_child_restart(sup, "compute", compute, timeout=3)
    assert restarted is not compute

    with anyio.fail_after(3):
        await done.wait()

    findings = await state.get(lambda s: list(s))
    assert findings == [f"finding-{i}" for i in range(10)]


async def test_state_survives_while_compute_restarts(runtime, make_supervisor):
    """G: compute crashes. W: supervisor restarts compute. T: state process identity unchanged."""
    sup = await make_supervisor(max_restarts=3, max_seconds=10)
    done = anyio.Event()
    state, compute = await _start_pair(
        sup, total_steps=10, crash_flag={"steps": [4]}, done=done
    )
    state_id = state.id

    await await_child_restart(sup, "compute", compute, timeout=3)
    with anyio.fail_after(3):
        await done.wait()

    assert sup.children["state"].process.id == state_id
    assert not state.has_stopped()


async def test_multiple_crashes_within_intensity_still_complete(
    runtime, make_supervisor
):
    """G: compute crashes at two different steps. W: both restarts succeed. T: all findings recorded."""
    sup = await make_supervisor(max_restarts=3, max_seconds=10)
    done = anyio.Event()
    state, compute = await _start_pair(
        sup, total_steps=12, crash_flag={"steps": [3, 8]}, done=done
    )

    await await_child_restart(sup, "compute", compute, timeout=3)

    with anyio.fail_after(5):
        await done.wait()

    findings = await state.get(lambda s: list(s))
    assert findings == [f"finding-{i}" for i in range(12)]


async def test_compute_exceeding_intensity_escalates_and_stops_state(runtime):
    """G: max_restarts=1, compute crashes twice. W: supervisor fails. T: both children are torn down."""
    sup = await Supervisor.start(max_restarts=1, max_seconds=10)
    done = anyio.Event()
    state, compute = await _start_pair(
        sup, total_steps=12, crash_flag={"steps": [3, 7]}, done=done, max_restarts=1
    )

    with anyio.fail_after(5):
        await sup.stopped()

    assert sup.has_stopped()
    await poll_value(lambda: state.has_stopped(), lambda v: v, timeout=2)
    assert state.has_stopped()
