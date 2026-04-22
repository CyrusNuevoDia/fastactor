"""GenStage — demand-driven, backpressure-aware pipeline primitives.

Three role classes share a common ``GenStage`` base (itself a ``GenServer``):

- ``Producer[E]``              — source of events; overrides ``handle_demand``
- ``Consumer[E]``              — sink; calls ``subscribe_to``, overrides ``handle_events``
- ``ProducerConsumer[In, Out]``— middle stage; both roles combined

Dispatchers control how events are routed from a producer to its subscribers:

- ``DemandDispatcher``   — events go to the subscriber with the most pending demand
- ``BroadcastDispatcher``— all subscribers receive the same event batch
- ``PartitionDispatcher``— routes events by ``hash_fn(event)`` to a registered partition

``handle_demand`` (Producer) and ``handle_events`` (Consumer/ProducerConsumer) may
return a plain list *or* be ``async def`` functions that ``yield`` events one by one.
The framework collects either form into a list before dispatching.
"""

from __future__ import annotations

import inspect
import logging
import typing as t
from dataclasses import dataclass, field
from typing import AsyncGenerator, ClassVar, Literal

from fastactor.utils import id_generator

from ._messages import Cancel, Demand, Events, Subscribe, SubscribeAck
from .gen_server import GenServer

logger = logging.getLogger(__name__)

_sub_id_gen = id_generator("sub")


# ---------------------------------------------------------------------------
# Dispatcher implementations
# ---------------------------------------------------------------------------


class DemandDispatcher:
    """Route events to the subscriber with the most outstanding demand (greedy)."""

    def __init__(self) -> None:
        self._demand: dict[str, int] = {}

    def subscribe(self, sub_id: str, opts: dict[str, t.Any]) -> None:
        self._demand[sub_id] = 0

    def cancel(self, sub_id: str) -> None:
        self._demand.pop(sub_id, None)

    def ask(self, sub_id: str, demand: int) -> int:
        self._demand[sub_id] = self._demand.get(sub_id, 0) + demand
        return demand

    def total_pending(self) -> int:
        return sum(self._demand.values())

    def dispatch(
        self, events: list[t.Any], subs: dict[str, t.Any]
    ) -> tuple[list[t.Any], dict[str, list[t.Any]]]:
        result: dict[str, list[t.Any]] = {sid: [] for sid in subs}
        remaining = list(events)
        while remaining and any(self._demand.get(sid, 0) > 0 for sid in subs):
            best = max(
                (sid for sid in subs if self._demand.get(sid, 0) > 0),
                key=lambda sid: self._demand.get(sid, 0),
            )
            n = min(len(remaining), self._demand[best])
            result[best].extend(remaining[:n])
            remaining = remaining[n:]
            self._demand[best] -= n
        return remaining, result


class BroadcastDispatcher:
    """Send the same event batch to all subscribers.

    Effective demand is ``min`` across all subscribers — demand is only consumed
    when every subscriber has sent at least some demand.
    """

    def __init__(self) -> None:
        self._demand: dict[str, int] = {}

    def subscribe(self, sub_id: str, opts: dict[str, t.Any]) -> None:
        self._demand[sub_id] = 0

    def cancel(self, sub_id: str) -> None:
        self._demand.pop(sub_id, None)

    def ask(self, sub_id: str, demand: int) -> int:
        old_min = min(self._demand.values(), default=0)
        self._demand[sub_id] = self._demand.get(sub_id, 0) + demand
        new_min = min(self._demand.values(), default=0)
        return max(0, new_min - old_min)

    def total_pending(self) -> int:
        return min(self._demand.values(), default=0)

    def dispatch(
        self, events: list[t.Any], subs: dict[str, t.Any]
    ) -> tuple[list[t.Any], dict[str, list[t.Any]]]:
        if not subs:
            return events, {}
        min_dem = min(self._demand.get(sid, 0) for sid in subs)
        batch = events[:min_dem]
        leftover = events[min_dem:]
        result: dict[str, list[t.Any]] = {}
        for sid in subs:
            result[sid] = list(batch)
            self._demand[sid] = max(0, self._demand.get(sid, 0) - len(batch))
        return leftover, result


class PartitionDispatcher:
    """Route events by partition.

    Each subscriber registers with ``partition=<int>`` in its subscription opts.
    ``hash_fn(event)`` maps each event to a partition integer; events for
    unregistered partitions are buffered as leftover.
    """

    def __init__(self, hash_fn: t.Callable[[t.Any], int]) -> None:
        self.hash_fn = hash_fn
        self._demand: dict[str, int] = {}
        self._partition_map: dict[int, str] = {}

    def subscribe(self, sub_id: str, opts: dict[str, t.Any]) -> None:
        partition = opts.get("partition")
        if partition is None:
            raise ValueError(
                f"PartitionDispatcher requires 'partition' in subscription opts for {sub_id}"
            )
        self._partition_map[int(partition)] = sub_id
        self._demand[sub_id] = 0

    def cancel(self, sub_id: str) -> None:
        self._demand.pop(sub_id, None)
        self._partition_map = {
            p: s for p, s in self._partition_map.items() if s != sub_id
        }

    def ask(self, sub_id: str, demand: int) -> int:
        self._demand[sub_id] = self._demand.get(sub_id, 0) + demand
        return demand

    def total_pending(self) -> int:
        return sum(self._demand.values())

    def dispatch(
        self, events: list[t.Any], subs: dict[str, t.Any]
    ) -> tuple[list[t.Any], dict[str, list[t.Any]]]:
        result: dict[str, list[t.Any]] = {sid: [] for sid in subs}
        leftover: list[t.Any] = []
        for event in events:
            partition = self.hash_fn(event)
            sub_id = self._partition_map.get(partition)
            if sub_id is None or sub_id not in subs:
                leftover.append(event)
                continue
            if self._demand.get(sub_id, 0) > 0:
                result[sub_id].append(event)
                self._demand[sub_id] -= 1
            else:
                leftover.append(event)
        return leftover, result


# ---------------------------------------------------------------------------
# Internal subscription record
# ---------------------------------------------------------------------------


@dataclass
class _SubscriptionInfo:
    sub_id: str
    peer: "GenStage"
    max_demand: int = 1000
    min_demand: int = 0
    cancel: str = "permanent"
    in_flight: int = 0  # events requested but not yet received (consumer side)


# ---------------------------------------------------------------------------
# GenStage base
# ---------------------------------------------------------------------------


@dataclass(repr=False)
class GenStage(GenServer):
    """Shared base for Producer, Consumer, ProducerConsumer."""

    # Subclasses may override these at class level
    dispatcher_class: ClassVar[type] = DemandDispatcher
    dispatcher_opts: ClassVar[dict[str, t.Any]] = {}

    # True for Producer; False for Consumer / ProducerConsumer.
    # Controls whether _do_demand calls handle_demand.
    _is_producer: ClassVar[bool] = False

    _dispatcher: t.Any = field(default=None, init=False)

    # Producer side: subscriptions from downstream consumers
    _producer_subs: dict[str, _SubscriptionInfo] = field(
        default_factory=dict, init=False
    )

    # Consumer side: subscriptions to upstream producers
    _consumer_subs: dict[str, _SubscriptionInfo] = field(
        default_factory=dict, init=False
    )

    # Output event buffer — holds events that couldn't be dispatched because
    # downstream demand hadn't arrived yet (mainly for ProducerConsumer).
    _output_buffer: list[t.Any] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------
    # Dispatcher helpers
    # ------------------------------------------------------------------

    def _init_dispatcher(self) -> None:
        opts = dict(self.dispatcher_opts)
        cls = self.dispatcher_class
        try:
            self._dispatcher = cls(**opts)
        except TypeError:
            self._dispatcher = cls()

    # ------------------------------------------------------------------
    # Async-generator / coroutine result collector
    # ------------------------------------------------------------------

    @staticmethod
    async def _collect(result: t.Any) -> list[t.Any]:
        if inspect.isasyncgen(result):
            return [item async for item in result]
        if result is None:
            return []
        return list(result)

    # ------------------------------------------------------------------
    # Message routing — intercept stage messages before GenServer
    # ------------------------------------------------------------------

    async def _handle_message(self, message: t.Any) -> None:
        match message:
            case Subscribe():
                await self._do_subscribe(message)
            case SubscribeAck():
                await self._do_subscribe_ack(message)
            case Demand():
                await self._do_demand(message)
            case Events():
                await self._do_events(message)
            case Cancel():
                await self._do_cancel(message)
            case _:
                await super()._handle_message(message)

    # ------------------------------------------------------------------
    # Producer side: Subscribe / Demand
    # ------------------------------------------------------------------

    async def _do_subscribe(self, msg: Subscribe) -> None:
        sub_id = msg.subscription_id
        opts = msg.opts
        consumer = msg.sender
        assert consumer is not None

        info = _SubscriptionInfo(
            sub_id=sub_id,
            peer=consumer,  # type: ignore[arg-type]
            max_demand=opts.get("max_demand", 1000),
            min_demand=opts.get("min_demand", 0),
            cancel=opts.get("cancel", "permanent"),
        )
        self._producer_subs[sub_id] = info

        if self._dispatcher is None:
            self._init_dispatcher()
        self._dispatcher.subscribe(sub_id, opts)

        await consumer.send(SubscribeAck(self, sub_id, dict(opts)))
        logger.debug("%s accepted subscription %s from %s", self, sub_id, consumer)

        # If we have buffered output, try dispatching now that there is a new subscriber
        if self._output_buffer:
            buf, self._output_buffer = self._output_buffer, []
            await self._dispatch_events(buf)

    async def _do_demand(self, msg: Demand) -> None:
        sub_id = msg.subscription_id
        count = msg.count

        if sub_id not in self._producer_subs:
            logger.warning("%s received Demand for unknown sub %s", self, sub_id)
            return

        if self._dispatcher is None:
            self._init_dispatcher()

        self._dispatcher.ask(sub_id, count)

        # Always flush the output buffer first (handles ProducerConsumer backlog)
        if self._output_buffer:
            buf, self._output_buffer = self._output_buffer, []
            await self._dispatch_events(buf)

        # Pure producers (not ProducerConsumer) call handle_demand to generate events
        if type(self)._is_producer:
            while self._dispatcher.total_pending() > 0:
                pending = self._dispatcher.total_pending()
                raw = self.handle_demand(pending)
                if inspect.isasyncgen(raw):
                    events = [item async for item in raw]
                else:
                    events = await self._collect(await raw)
                if not events:
                    break
                await self._dispatch_events(events)

    async def _dispatch_events(self, events: list[t.Any]) -> None:
        """Route events to subscribers; buffer any that cannot be dispatched."""
        if not events:
            return

        if self._dispatcher is None:
            self._init_dispatcher()

        if not self._producer_subs:
            # No subscribers yet — buffer everything
            self._output_buffer.extend(events)
            return

        leftover, routed = self._dispatcher.dispatch(events, self._producer_subs)
        if leftover:
            # Buffer undispatched events; they will be flushed when demand arrives
            self._output_buffer.extend(leftover)

        for sub_id, batch in routed.items():
            if not batch:
                continue
            info = self._producer_subs.get(sub_id)
            if info is None:
                continue
            try:
                await info.peer.send(Events(self, sub_id, batch))
            except Exception as exc:
                logger.error("%s failed sending events to %s: %r", self, info.peer, exc)

    # ------------------------------------------------------------------
    # Consumer side: SubscribeAck / Events
    # ------------------------------------------------------------------

    async def _do_subscribe_ack(self, msg: SubscribeAck) -> None:
        sub_id = msg.subscription_id
        info = self._consumer_subs.get(sub_id)
        if info is None:
            logger.warning("%s got SubscribeAck for unknown sub %s", self, sub_id)
            return
        logger.debug("%s subscription %s confirmed by %s", self, sub_id, msg.sender)
        await self._send_demand(info)

    async def _do_events(self, msg: Events) -> None:
        sub_id = msg.subscription_id
        events = msg.events
        info = self._consumer_subs.get(sub_id)
        if info is None:
            logger.warning("%s got Events for unknown sub %s", self, sub_id)
            return

        info.in_flight = max(0, info.in_flight - len(events))

        # Deliver to subclass; collect result (list or async generator)
        raw = self.handle_events(events)
        if inspect.isasyncgen(raw):
            produced = [item async for item in raw]
        elif inspect.isawaitable(raw):
            produced = await self._collect(await raw)
        else:
            produced = []

        # ProducerConsumer: dispatch produced events downstream (may buffer)
        if produced:
            await self._dispatch_events(produced)

        # Refill upstream demand when we drop below min_demand
        if info.in_flight < info.min_demand or info.in_flight == 0:
            refill = info.max_demand - info.in_flight
            if refill > 0:
                await self._send_demand(info, refill)

    async def _send_demand(
        self, info: _SubscriptionInfo, count: int | None = None
    ) -> None:
        if count is None:
            count = info.max_demand
        if count <= 0:
            return
        info.in_flight += count
        try:
            await info.peer.send(Demand(self, info.sub_id, count))
        except Exception as exc:
            logger.error("%s failed sending Demand to %s: %r", self, info.peer, exc)

    # ------------------------------------------------------------------
    # Cancel — both sides
    # ------------------------------------------------------------------

    async def _do_cancel(self, msg: Cancel) -> None:
        sub_id = msg.subscription_id

        if sub_id in self._producer_subs:
            info = self._producer_subs.pop(sub_id)
            if self._dispatcher is not None:
                self._dispatcher.cancel(sub_id)
            logger.debug("%s consumer %s cancelled sub %s", self, info.peer, sub_id)
            return

        if sub_id in self._consumer_subs:
            info = self._consumer_subs.pop(sub_id)
            logger.debug("%s cancelled sub %s to %s", self, sub_id, info.peer)

    # ------------------------------------------------------------------
    # User-overrideable callbacks (no-ops in base)
    # ------------------------------------------------------------------

    def handle_demand(self, demand: int) -> t.Any:  # noqa: ARG002
        return []

    def handle_events(self, events: list[t.Any]) -> t.Any:  # noqa: ARG002
        return []


# ---------------------------------------------------------------------------
# Role classes
# ---------------------------------------------------------------------------


@dataclass(repr=False)
class Producer[E](GenStage):
    """Data source. Override ``handle_demand(demand)`` to produce events.

    ``handle_demand`` may ``return`` a list or ``yield`` events one at a time
    (async generator syntax).
    """

    _is_producer: ClassVar[bool] = True

    async def handle_demand(self, demand: int) -> list[E] | AsyncGenerator[E, None]:  # type: ignore[override]
        return []


@dataclass(repr=False)
class Consumer[E](GenStage):
    """Data sink. Call ``subscribe_to(producer)`` then override ``handle_events``."""

    async def subscribe_to(
        self,
        producer: "Producer[E] | ProducerConsumer[t.Any, E]",
        *,
        max_demand: int = 1000,
        min_demand: int = 0,
        cancel: Literal["permanent", "transient", "temporary"] = "permanent",
        **dispatcher_opts: t.Any,
    ) -> str:
        """Subscribe to *producer* and return the new subscription id."""
        sub_id = _sub_id_gen()
        opts: dict[str, t.Any] = {
            "max_demand": max_demand,
            "min_demand": min_demand,
            "cancel": cancel,
            **dispatcher_opts,
        }
        info = _SubscriptionInfo(
            sub_id=sub_id,
            peer=producer,  # type: ignore[arg-type]
            max_demand=max_demand,
            min_demand=min_demand,
            cancel=cancel,
        )
        self._consumer_subs[sub_id] = info
        await producer.send(Subscribe(self, sub_id, opts))
        return sub_id

    async def cancel_subscription(self, sub_id: str, reason: t.Any = "normal") -> None:
        """Cancel an active subscription."""
        # Pop BEFORE the first await so any Events still in the mailbox for this
        # sub_id are ignored by _do_events (which checks _consumer_subs).
        info = self._consumer_subs.pop(sub_id, None)
        if info is None:
            return
        try:
            await info.peer.send(Cancel(self, sub_id, reason))
        except Exception:
            pass

    async def handle_events(self, events: list[E]) -> None:  # type: ignore[override]
        pass


@dataclass(repr=False)
class ProducerConsumer[In, Out](GenStage):
    """Middle stage — consumes from upstream and produces for downstream.

    Call ``subscribe_to(upstream)`` to connect to a producer.
    Override ``handle_events`` to transform events; return a list or ``yield``
    events. Output events are dispatched to this stage's own subscribers.
    """

    async def subscribe_to(
        self,
        producer: "Producer[In] | ProducerConsumer[t.Any, In]",
        *,
        max_demand: int = 1000,
        min_demand: int = 0,
        cancel: Literal["permanent", "transient", "temporary"] = "permanent",
        **dispatcher_opts: t.Any,
    ) -> str:
        """Subscribe to an upstream producer."""
        sub_id = _sub_id_gen()
        opts: dict[str, t.Any] = {
            "max_demand": max_demand,
            "min_demand": min_demand,
            "cancel": cancel,
            **dispatcher_opts,
        }
        info = _SubscriptionInfo(
            sub_id=sub_id,
            peer=producer,  # type: ignore[arg-type]
            max_demand=max_demand,
            min_demand=min_demand,
            cancel=cancel,
        )
        self._consumer_subs[sub_id] = info
        await producer.send(Subscribe(self, sub_id, opts))
        return sub_id

    async def cancel_subscription(self, sub_id: str, reason: t.Any = "normal") -> None:
        info = self._consumer_subs.get(sub_id)
        if info is None:
            return
        try:
            await info.peer.send(Cancel(self, sub_id, reason))
        except Exception:
            pass
        self._consumer_subs.pop(sub_id, None)

    async def handle_events(  # type: ignore[override]
        self, events: list[In]
    ) -> list[Out] | AsyncGenerator[Out, None]:
        return []
