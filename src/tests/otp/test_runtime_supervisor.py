import pytest
from anyio import fail_after

from fastactor.otp import CrashRecord, GenServer, Process, Runtime


pytestmark = pytest.mark.anyio


class BoomProcess(Process):
    async def handle_info(self, message):
        raise RuntimeError(f"boom:{message.message}")


async def _crash(runtime: Runtime, tag: str) -> BoomProcess:
    proc = await BoomProcess.start()
    proc.info(tag)
    with fail_after(2):
        await proc.stopped()
    return proc


async def test_runtime_supervisor_records_crashes(runtime: Runtime):
    assert runtime.supervisor is not None

    assert runtime.total_crashes() == 0

    proc = await _crash(runtime, "first")

    # The runtime's root supervisor is linked to the BoomProcess via start_link.
    # When it crashes, the runtime records the emitted crash event.
    with fail_after(2):
        while runtime.total_crashes() < 1:
            await _tick()

    assert runtime.total_crashes() >= 1
    recent = runtime.recent_crashes()
    assert any(record.process_id == proc.id for record in recent)
    counts = runtime.crash_counts()
    assert counts.get("RuntimeError", 0) >= 1


async def test_crash_counts_aggregate_by_class(runtime: Runtime):
    assert runtime.supervisor is not None

    await _crash(runtime, "a")
    await _crash(runtime, "b")
    await _crash(runtime, "c")

    with fail_after(2):
        while runtime.total_crashes() < 3:
            await _tick()

    assert runtime.crash_counts()["RuntimeError"] >= 3


async def test_recent_crashes_respects_n(runtime: Runtime):
    assert runtime.supervisor is not None

    for i in range(5):
        await _crash(runtime, f"msg-{i}")

    with fail_after(2):
        while runtime.total_crashes() < 5:
            await _tick()

    assert len(runtime.recent_crashes(n=2)) == 2
    assert len(runtime.recent_crashes(n=100)) >= 5
    assert runtime.recent_crashes(n=0) == []


async def test_runtime_child_crashed_event_fires(runtime: Runtime):
    seen: list[CrashRecord] = []
    runtime.emitter.on(
        "runtime:child_crashed",
        lambda process_id, reason, record: seen.append(record),
    )

    proc = await _crash(runtime, "event")

    with fail_after(2):
        while not any(r.process_id == proc.id for r in seen):
            await _tick()

    rec = next(r for r in seen if r.process_id == proc.id)
    assert rec.reason_class == "RuntimeError"
    assert "event" in rec.reason_repr


async def test_normal_shutdowns_are_not_recorded(runtime: Runtime):
    assert runtime.supervisor is not None
    baseline = runtime.total_crashes()

    class Boring(GenServer):
        async def handle_call(self, call):
            return call.message

    proc = await Boring.start()
    await proc.stop("normal")

    # Give the supervisor a chance to observe (it shouldn't record anything).
    with fail_after(1):
        for _ in range(10):
            await _tick()

    assert runtime.total_crashes() == baseline


async def _tick() -> None:
    from anyio import sleep

    await sleep(0.01)
