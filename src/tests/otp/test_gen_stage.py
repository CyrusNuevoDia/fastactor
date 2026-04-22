"""Tests for GenStage — Producer, Consumer, ProducerConsumer, and all three dispatchers."""

import asyncio
from typing import Any

import pytest
from anyio import sleep

from fastactor.otp import (
    BroadcastDispatcher,
    Consumer,
    PartitionDispatcher,
    Producer,
    ProducerConsumer,
)
from fastactor.otp._messages import Call

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class NumberProducer(Producer[int]):
    async def init(self, start: int = 0, chunk: int | None = None):  # type: ignore[override]
        self.counter = start
        self.chunk = chunk  # None = unlimited

    async def handle_demand(self, demand: int):  # type: ignore[override]
        count = min(demand, self.chunk) if self.chunk is not None else demand
        for i in range(count):
            yield self.counter + i
        self.counter += count


class ListProducer(Producer[int]):
    """Produces from a fixed list then goes silent."""

    async def init(self, items: list[int]):  # type: ignore[override]
        self._items = list(items)

    async def handle_demand(self, demand: int) -> list[int]:  # type: ignore[override]
        batch, self._items = self._items[:demand], self._items[demand:]
        return batch


class CollectorConsumer(Consumer[int]):
    async def init(self, *, max_demand: int = 10, min_demand: int = 0):  # type: ignore[override]
        self.received: list[int] = []
        self._max_demand = max_demand
        self._min_demand = min_demand

    async def handle_events(self, events: list[int]) -> None:  # type: ignore[override]
        self.received.extend(events)

    async def await_n(self, n: int, timeout: float = 3.0) -> list[int]:
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.received) < n:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Only {len(self.received)}/{n} events after {timeout}s"
                )
            await sleep(0.05)
        return self.received[:n]


class DoublerProducerConsumer(ProducerConsumer[int, int]):
    async def handle_events(self, events: list[int]):  # type: ignore[override]
        for e in events:
            yield e * 2


class EchoProducer(Producer[Any]):
    """Also answers GenServer calls — verifies GenServer compat."""

    async def init(self):  # type: ignore[override]
        self.demand_count = 0

    async def handle_demand(self, demand: int) -> list[Any]:  # type: ignore[override]
        self.demand_count += demand
        return []

    async def handle_call(self, call: Call) -> Any:
        return call.message


class PartitionedConsumer(Consumer[int]):
    """Consumer that registers on a specific partition."""

    async def init(self, partition: int, max_demand: int = 10):  # type: ignore[override]
        self.partition = partition
        self.received: list[int] = []
        self._max_demand = max_demand

    async def handle_events(self, events: list[int]) -> None:  # type: ignore[override]
        self.received.extend(events)

    async def await_n(self, n: int, timeout: float = 3.0) -> list[int]:
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.received) < n:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Partition {self.partition}: {len(self.received)}/{n}"
                )
            await sleep(0.05)
        return self.received[:n]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_producer_consumer_basic():
    """Basic producer → consumer pipeline delivers events in order."""
    producer = await NumberProducer.start(start=0)
    consumer = await CollectorConsumer.start(max_demand=5)
    await consumer.subscribe_to(producer, max_demand=5)

    received = await consumer.await_n(5)
    assert received == [0, 1, 2, 3, 4]

    await producer.stop()
    await consumer.stop()


async def test_list_producer():
    """Producer returning a plain list (not generator) works correctly."""
    producer = await ListProducer.start(items=list(range(20)))
    consumer = await CollectorConsumer.start()
    await consumer.subscribe_to(producer, max_demand=10)

    received = await consumer.await_n(10)
    assert received == list(range(10))

    await producer.stop()
    await consumer.stop()


async def test_backpressure():
    """Consumer with max_demand=5 receives exactly 5 events per demand cycle."""
    producer = await NumberProducer.start(start=0, chunk=100)
    consumer = await CollectorConsumer.start(max_demand=5)
    await consumer.subscribe_to(producer, max_demand=5)

    # First batch must be exactly 5
    received = await consumer.await_n(5)
    assert len(received) == 5
    assert received == [0, 1, 2, 3, 4]

    await producer.stop()
    await consumer.stop()


async def test_broadcast_dispatcher():
    """BroadcastDispatcher sends the same events to all consumers."""

    class BroadcastProducer(Producer[int]):
        dispatcher_class = BroadcastDispatcher

        async def init(self):  # type: ignore[override]
            self.counter = 0

        async def handle_demand(self, demand: int) -> list[int]:  # type: ignore[override]
            batch = list(range(self.counter, self.counter + demand))
            self.counter += demand
            return batch

    producer = await BroadcastProducer.start()
    c1 = await CollectorConsumer.start(max_demand=5)
    c2 = await CollectorConsumer.start(max_demand=5)

    await c1.subscribe_to(producer, max_demand=5)
    await c2.subscribe_to(producer, max_demand=5)

    # Both consumers should eventually get events
    r1 = await c1.await_n(5)
    r2 = await c2.await_n(5)

    assert len(r1) == 5
    assert len(r2) == 5
    # Both receive the same events (broadcast)
    assert set(r1) == set(r2)

    await producer.stop()
    await c1.stop()
    await c2.stop()


async def test_partition_dispatcher():
    """PartitionDispatcher routes events by partition to the right consumer."""

    class PartProducer(Producer[int]):
        dispatcher_class = PartitionDispatcher
        dispatcher_opts = {"hash_fn": lambda e: e % 4}

        async def init(self):  # type: ignore[override]
            self.counter = 0

        async def handle_demand(self, demand: int) -> list[int]:  # type: ignore[override]
            batch = list(range(self.counter, self.counter + demand))
            self.counter += demand
            return batch

    producer = await PartProducer.start()

    consumers = [
        await PartitionedConsumer.start(partition=p, max_demand=4) for p in range(4)
    ]
    for c in consumers:
        await c.subscribe_to(producer, max_demand=4, partition=c.partition)

    # Give some time for events to flow
    await sleep(0.3)

    # Each consumer should only receive events for their partition
    for c in consumers:
        for event in c.received:
            assert event % 4 == c.partition, (
                f"Consumer partition={c.partition} got event {event} (% 4 = {event % 4})"
            )

    await producer.stop()
    for c in consumers:
        await c.stop()


async def test_producer_consumer_stage():
    """NumberProducer → DoublerProducerConsumer → CollectorConsumer doubles all values."""
    producer = await NumberProducer.start(start=1, chunk=5)
    doubler = await DoublerProducerConsumer.start()
    consumer = await CollectorConsumer.start(max_demand=5)

    await doubler.subscribe_to(producer, max_demand=5)
    await consumer.subscribe_to(doubler, max_demand=5)

    received = await consumer.await_n(5)
    assert received == [2, 4, 6, 8, 10]

    await producer.stop()
    await doubler.stop()
    await consumer.stop()


async def test_cancel_subscription():
    """After cancel_subscription, no more events reach the consumer."""
    producer = await NumberProducer.start(start=0, chunk=3)
    consumer = await CollectorConsumer.start(max_demand=3)
    sub_id = await consumer.subscribe_to(producer, max_demand=3)

    # Wait for first batch
    await consumer.await_n(3)
    snapshot = len(consumer.received)

    await consumer.cancel_subscription(sub_id)
    await sleep(0.2)  # give time for any stray events

    assert len(consumer.received) == snapshot

    await producer.stop()
    await consumer.stop()


async def test_handle_call_alongside_events():
    """GenServer call works on a Producer even while it is serving demand."""
    producer = await EchoProducer.start()
    consumer = await CollectorConsumer.start(max_demand=10)
    await consumer.subscribe_to(producer, max_demand=10)

    # Calls should still work
    reply = await producer.call("ping")
    assert reply == "ping"

    await producer.stop()
    await consumer.stop()


async def test_async_generator_handle_demand():
    """Producer that uses `yield` (async generator) delivers events correctly."""
    producer = await NumberProducer.start(start=10, chunk=5)
    consumer = await CollectorConsumer.start(max_demand=5)
    await consumer.subscribe_to(producer, max_demand=5)

    received = await consumer.await_n(5)
    assert received == [10, 11, 12, 13, 14]

    await producer.stop()
    await consumer.stop()


async def test_multiple_subscriptions_demand_dispatcher():
    """With DemandDispatcher, two consumers each get their fair share of events."""
    producer = await NumberProducer.start(start=0)
    c1 = await CollectorConsumer.start(max_demand=5)
    c2 = await CollectorConsumer.start(max_demand=5)

    await c1.subscribe_to(producer, max_demand=5)
    await c2.subscribe_to(producer, max_demand=5)

    # Wait for both consumers to get some events
    await c1.await_n(5)
    await c2.await_n(5)

    # No event should appear in both consumers (DemandDispatcher does not broadcast)
    s1 = set(c1.received)
    s2 = set(c2.received)
    assert s1.isdisjoint(s2), f"Overlap: {s1 & s2}"

    await producer.stop()
    await c1.stop()
    await c2.stop()


async def test_min_demand_refill():
    """Consumer with min_demand refills demand automatically when buffer drains."""
    # chunk=3 so each handle_demand returns ≤3 items
    producer = await NumberProducer.start(start=0, chunk=3)
    consumer = await CollectorConsumer.start(max_demand=6, min_demand=2)
    await consumer.subscribe_to(producer, max_demand=6, min_demand=2)

    # Should receive more than one batch due to refill
    received = await consumer.await_n(9, timeout=4.0)
    assert len(received) >= 9

    await producer.stop()
    await consumer.stop()
