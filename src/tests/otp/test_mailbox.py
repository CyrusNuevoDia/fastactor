"""OTP conformance suite — §3 Mailbox semantics.

See `SPEC.md` §3 for the source claims.
"""

from typing import Any

import pytest
from anyio import Event, create_task_group, fail_after, sleep

from fastactor.otp import Continue, GenServer, Info, Process

pytestmark = pytest.mark.anyio


class SelfSender(Process):
    async def init(self, payload: Any = "ping") -> Continue:
        self.received: list[Any] = []
        self._delivered = Event()
        self._payload = payload
        return Continue("send-self")

    async def handle_continue(self, term: Any) -> None:
        await self.send(self._payload)

    async def _handle_message(self, message: Any) -> None:
        self.received.append(message)
        self._delivered.set()

    async def await_delivery(self, timeout: float = 2) -> list[Any]:
        with fail_after(timeout):
            await self._delivered.wait()
        return list(self.received)


class InboxRecorder(GenServer):
    async def init(self) -> None:
        self.received: list[Any] = []

    async def handle_info(self, message: Info) -> None:
        self.received.append(message.message)


async def test_3_1_single_sender_ordering_is_fifo() -> None:
    """SPEC §3.1: Messages from a single sender arrive in the order they were sent.

    Source: https://www.erlang.org/doc/system/ref_man_processes.html
    """
    recorder = await InboxRecorder.start()

    for i in range(1, 6):
        recorder.info(i)

    with fail_after(2):
        while len(recorder.received) < 5:
            await sleep(0.01)

    assert recorder.received == [1, 2, 3, 4, 5]

    await recorder.stop("normal")


async def test_3_1_per_sender_ordering_preserved_across_concurrent_senders() -> None:
    """SPEC §3.1: Per-sender FIFO preserved; cross-sender order is unspecified.

    Source: https://www.erlang.org/doc/system/ref_man_processes.html
    """
    recorder = await InboxRecorder.start()

    async def sender(tag: str) -> None:
        for i in range(5):
            recorder.info((tag, i))

    async with create_task_group() as tg:
        tg.start_soon(sender, "a")
        tg.start_soon(sender, "b")

    with fail_after(2):
        while len(recorder.received) < 10:
            await sleep(0.01)

    a_items = [v for (tag, v) in recorder.received if tag == "a"]
    b_items = [v for (tag, v) in recorder.received if tag == "b"]
    assert a_items == [0, 1, 2, 3, 4]
    assert b_items == [0, 1, 2, 3, 4]

    await recorder.stop("normal")


async def test_3_4_self_send_delivers_to_own_mailbox() -> None:
    """SPEC §3.4: A process can send to itself; the message is enqueued and received later.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#send/2
    """
    proc = await SelfSender.start(payload="ping")

    received = await proc.await_delivery()

    assert received == ["ping"]

    await proc.stop("normal")
