import random

import anyio
import pytest
from anyio import Event, fail_after

from fastactor.otp import GenServer, Info, Process, TimerRef

pytestmark = pytest.mark.anyio


async def wait_for(condition, *, timeout: float = 1.0) -> None:
    with fail_after(timeout):
        while not condition():
            await anyio.sleep(0.005)


class TimerEcho(GenServer):
    async def init(
        self,
        *,
        expected: int | None = None,
        block_s: float = 0,
    ) -> None:
        self.expected = expected
        self.block_s = block_s
        self.infos: list[Info] = []
        self.messages: list[object] = []
        self.times: list[float] = []
        self.depths: list[int] = []
        self.received = Event()
        self.expected_received = Event()

    async def handle_info(self, message: Info) -> None:
        self.infos.append(message)
        self.messages.append(message.message)
        self.times.append(anyio.current_time())
        if self._inbox is not None:
            self.depths.append(self._inbox.statistics().current_buffer_used)
        if self.block_s:
            await anyio.sleep(self.block_s)
        self.received.set()
        if self.expected is not None and len(self.messages) >= self.expected:
            self.expected_received.set()


class InitTimer(Process):
    async def init(self) -> None:
        self.send_after(60_000, "ping")

    async def handle_info(self, message: Info) -> None:
        raise AssertionError(f"unexpected timer delivery: {message.message!r}")


async def test_send_after_delivers_info_to_self() -> None:
    proc = await TimerEcho.start()

    ref = proc.send_after(50, "ping")

    assert isinstance(ref, TimerRef)
    await wait_for(lambda: proc.messages == ["ping"])
    assert proc.infos[0].message == "ping"

    await proc.stop("normal")


async def test_cancel_timer_prevents_send_after_delivery() -> None:
    proc = await TimerEcho.start()

    ref = proc.send_after(1000, "ping")
    remaining = proc.cancel_timer(ref)

    assert remaining is not None
    await anyio.sleep(1.1)
    assert proc.messages == []

    await proc.stop("normal")


async def test_cancel_timer_after_fire_returns_none() -> None:
    proc = await TimerEcho.start()

    ref = proc.send_after(10, "ping")
    await wait_for(lambda: proc.messages == ["ping"])

    assert proc.cancel_timer(ref) is None

    await proc.stop("normal")


async def test_timers_are_cancelled_when_process_stops() -> None:
    proc = await InitTimer.start()

    assert proc._proc_timers

    await proc.stop("normal")

    assert proc._proc_timer_tg is None
    assert proc._proc_timers == {}


async def test_start_interval_delivers_periodically() -> None:
    proc = await TimerEcho.start(expected=4)

    ref = proc.start_interval(20, "tick")
    await wait_for(lambda: len(proc.messages) >= 4)
    proc.cancel_timer(ref)

    tick_times = proc.times[:4]
    intervals = [right - left for left, right in zip(tick_times, tick_times[1:])]
    assert len(intervals) >= 3
    assert all(0.005 <= interval <= 0.075 for interval in intervals)

    await proc.stop("normal")


async def test_start_interval_does_not_pile_up_when_handler_is_slow() -> None:
    proc = await TimerEcho.start(expected=3, block_s=0.08)

    ref = proc.start_interval(20, "tick")
    await wait_for(lambda: len(proc.messages) >= 3)
    proc.cancel_timer(ref)
    count_after_cancel = len(proc.messages)

    intervals = [right - left for left, right in zip(proc.times, proc.times[1:])]
    assert len(intervals) >= 2
    assert all(interval >= 0.075 for interval in intervals[:2])
    assert max(proc.depths, default=0) <= 1
    await anyio.sleep(0.13)
    assert len(proc.messages) == count_after_cancel

    await proc.stop("normal")


async def test_send_after_can_target_another_process() -> None:
    sender = await TimerEcho.start()
    receiver = await TimerEcho.start()

    sender.send_after(20, "to-b", target=receiver)

    await wait_for(lambda: receiver.messages == ["to-b"])
    assert sender.messages == []
    assert receiver.infos[0].sender == sender

    await receiver.stop("normal")
    await sender.stop("normal")


async def test_send_after_to_stopped_target_is_silent() -> None:
    sender = await TimerEcho.start()
    receiver = await TimerEcho.start()

    sender.send_after(50, "to-b", target=receiver)
    await receiver.stop("normal")
    await anyio.sleep(0.1)

    assert not sender.has_stopped()

    await sender.stop("normal")


async def test_many_timers_can_cancel_exact_subset() -> None:
    proc = await TimerEcho.start(expected=500)
    rng = random.Random(42)
    refs = [proc.send_after(250, idx) for idx in range(1000)]
    cancelled = set(rng.sample(range(1000), 500))

    for idx in cancelled:
        assert proc.cancel_timer(refs[idx]) is not None

    await wait_for(lambda: len(proc.messages) >= 500, timeout=3)
    await anyio.sleep(0.05)

    expected = set(range(1000)) - cancelled
    assert set(proc.messages) == expected
    assert len(proc.messages) == len(expected)

    await proc.stop("normal")


async def test_reschedule_replaces_prior_named_timer() -> None:
    proc = await TimerEcho.start()

    proc.reschedule("tick", 50, "a")
    proc.reschedule("tick", 50, "b")

    await anyio.sleep(0.1)
    assert proc.messages == ["b"]
    assert [info.message for info in proc.infos] == ["b"]

    await proc.stop("normal")


async def test_reschedule_name_can_be_reused_after_timer_fires() -> None:
    proc = await TimerEcho.start()

    proc.reschedule("tick", 50, "a")
    await wait_for(lambda: proc.messages == ["a"])

    proc.reschedule("tick", 50, "b")
    await wait_for(lambda: proc.messages == ["a", "b"])

    await proc.stop("normal")


async def test_named_timers_are_cancelled_when_process_stops() -> None:
    class InitNamedTimer(Process):
        async def init(self) -> None:
            self.reschedule("tick", 60_000, "ping")

        async def handle_info(self, message: Info) -> None:
            raise AssertionError(f"unexpected timer delivery: {message.message!r}")

    proc = await InitNamedTimer.start()

    assert proc._proc_timers
    assert proc._proc_named_timers

    await proc.stop("normal")

    assert proc._proc_timer_tg is None
    assert proc._proc_timers == {}
    assert proc._proc_named_timers == {}
