"""Dry-run broker simulator — emits broker events without touching a live API.

Fill model:
- **Taker (market) orders** use IOC semantics: walk L2 depth up to
  ``max_slip_ticks`` from the reference price, fill whatever is available,
  and cancel the unfilled remainder.  Fill price is the depth-weighted average
  (VWAP) of swept levels.
- **Maker (limit) orders** are trade-print-driven: call ``on_trade()`` before
  ``on_board()``.  The queue position decrements only on actual trades at the
  order price — cancel-only size drops are ignored.  Fill triggers when the
  accumulated trade volume exceeds ``queue_ahead_qty`` (i.e. our position in
  the queue is reached).

The simulator never mutates strategy state.  All returned ``BrokerOrderEvent``
and ``BrokerFillEvent`` objects must be fed back through the strategy.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderStatus, TradePrint


@dataclass(slots=True)
class SimulatedOrder:
    intent: OrderIntent
    leaves_qty: int
    queue_ahead_qty: int


class DryRunSimulator:
    """Minimal broker/fill simulator that emits broker events only.

    It never mutates strategy position directly. Callers must feed returned
    BrokerOrderEvent/BrokerFillEvent objects back through CombinedMakerTakerStrategy.

    Fill model:
    - Taker market orders are IOC-style aggressive fills across visible depth
      within max_slip_ticks. Any remainder is canceled, never passively queued.
    - Maker limit orders are trade-print-driven. Queue consumption uses actual
      trade volume at the order price, so cancel-only size drops do not fill.
    """

    def __init__(self, *, tick_size: float, slippage_ticks: float = 0.0) -> None:
        self.tick_size = max(tick_size, 1e-9)
        self.slippage_ticks = max(slippage_ticks, 0.0)
        self._orders: dict[str, SimulatedOrder] = {}
        self._acc_fills: dict[tuple[float, int], int] = {}

    def on_trade(self, trade: TradePrint, now_ns: int = 0) -> None:  # noqa: ARG002
        """Accumulate a trade print for queue-consumption in the next on_board()."""
        key = (trade.price, trade.side)
        self._acc_fills[key] = self._acc_fills.get(key, 0) + trade.size

    def submit(self, intent: OrderIntent, snapshot: BoardSnapshot, now_ns: int) -> list[BrokerOrderEvent | BrokerFillEvent]:
        ack = BrokerOrderEvent(
            order_id=intent.client_order_id,
            status=OrderStatus.WORKING,
            ts_ns=now_ns,
            broker_order_id=f"SIM-{intent.client_order_id}",
        )
        if intent.is_market:
            return self._submit_market(intent, snapshot, now_ns, ack)
        self._orders[intent.client_order_id] = SimulatedOrder(
            intent=intent,
            leaves_qty=intent.qty,
            queue_ahead_qty=self._initial_queue_ahead(intent, snapshot),
        )
        return [ack]

    def _submit_market(
        self,
        intent: OrderIntent,
        snapshot: BoardSnapshot,
        now_ns: int,
        ack: BrokerOrderEvent,
    ) -> list[BrokerOrderEvent | BrokerFillEvent]:
        """Fill market intent as an IOC aggressive order against visible depth."""
        fill_qty, fill_price = self._market_fill(intent, snapshot)
        events: list[BrokerOrderEvent | BrokerFillEvent] = [ack]
        if fill_qty > 0:
            events.append(
                BrokerFillEvent(
                    order_id=intent.client_order_id,
                    broker_order_id=ack.broker_order_id,
                    qty=fill_qty,
                    price=fill_price,
                    ts_ns=now_ns,
                    trade_id=f"SIMF-{intent.client_order_id}",
                )
            )
        if fill_qty < intent.qty:
            events.append(
                BrokerOrderEvent(
                    order_id=intent.client_order_id,
                    broker_order_id=ack.broker_order_id,
                    status=OrderStatus.CANCELED,
                    ts_ns=now_ns,
                    cum_qty=fill_qty,
                    avg_fill_price=fill_price if fill_qty > 0 else 0.0,
                    reason="ioc_unfilled_canceled",
                )
            )
        return events

    def on_board(self, snapshot: BoardSnapshot, now_ns: int) -> list[BrokerFillEvent]:
        fills: list[BrokerFillEvent] = []
        completed: list[str] = []
        acc = self._acc_fills
        self._acc_fills = {}
        for order_id, order in list(self._orders.items()):
            fill_qty = self._maker_fill_qty(order, snapshot, acc)
            if fill_qty <= 0:
                continue
            order.leaves_qty -= fill_qty
            fills.append(
                BrokerFillEvent(
                    order_id=order.intent.client_order_id,
                    qty=fill_qty,
                    price=order.intent.price,
                    ts_ns=now_ns,
                    trade_id=f"SIMF-{order.intent.client_order_id}-{order.intent.qty - order.leaves_qty}",
                )
            )
            if order.leaves_qty <= 0:
                completed.append(order_id)
        for order_id in completed:
            self._orders.pop(order_id, None)
        return fills

    def cancel(self, order_id: str, now_ns: int) -> list[BrokerOrderEvent]:
        """Simulate immediate broker acknowledgement of a cancel request."""
        order = self._orders.pop(order_id, None)
        if order is None:
            return []
        return [
            BrokerOrderEvent(
                order_id=order_id,
                status=OrderStatus.CANCELED,
                ts_ns=now_ns,
                broker_order_id=f"SIM-{order_id}",
            )
        ]

    def queue_ahead(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        return order.queue_ahead_qty if order is not None else 0

    def _market_fill(self, intent: OrderIntent, snapshot: BoardSnapshot) -> tuple[int, float]:
        reference = intent.reference_price or (snapshot.ask if intent.side > 0 else snapshot.bid) or intent.price
        if reference <= 0:
            return 0, 0.0
        slip_ticks = intent.max_slip_ticks if intent.max_slip_ticks > 0 else self.slippage_ticks
        limit_price = reference + intent.side * slip_ticks * self.tick_size
        remaining = max(intent.qty, 0)
        filled = 0
        value = 0.0
        for price, size in self._market_levels(intent, snapshot):
            if remaining <= 0:
                break
            if price <= 0 or size <= 0:
                continue
            if intent.side > 0 and price > limit_price + 1e-9:
                break
            if intent.side < 0 and price < limit_price - 1e-9:
                break
            qty = min(remaining, size)
            filled += qty
            value += qty * price
            remaining -= qty
        if filled <= 0:
            return 0, 0.0
        return filled, value / filled

    @staticmethod
    def _market_levels(intent: OrderIntent, snapshot: BoardSnapshot) -> list[tuple[float, int]]:
        if intent.side > 0:
            levels = snapshot.asks or ((snapshot.ask, snapshot.ask_size),)
            normalized = [(level.price, level.size) if hasattr(level, "price") else level for level in levels]
            return sorted(((float(price), int(size)) for price, size in normalized), key=lambda item: item[0])
        levels = snapshot.bids or ((snapshot.bid, snapshot.bid_size),)
        normalized = [(level.price, level.size) if hasattr(level, "price") else level for level in levels]
        return sorted(((float(price), int(size)) for price, size in normalized), key=lambda item: item[0], reverse=True)

    def _maker_fill_qty(
        self,
        order: SimulatedOrder,
        snapshot: BoardSnapshot,
        acc_fills: dict[tuple[float, int], int],
    ) -> int:
        """Compute fill qty using trade-print-driven queue consumption."""
        intent = order.intent
        if intent.side > 0:
            if snapshot.ask > 0 and snapshot.ask <= intent.price:
                return order.leaves_qty
            if snapshot.bid == intent.price:
                return self._queue_fill(order, acc_fills.get((intent.price, -1), 0))
            return 0
        if snapshot.bid > 0 and snapshot.bid >= intent.price:
            return order.leaves_qty
        if snapshot.ask == intent.price:
            return self._queue_fill(order, acc_fills.get((intent.price, 1), 0))
        return 0

    @staticmethod
    def _queue_fill(order: SimulatedOrder, trade_consumed: int) -> int:
        """Reduce queue_ahead by actual trades; fill when it reaches zero."""
        if trade_consumed <= 0:
            return 0
        prev_queue = order.queue_ahead_qty
        order.queue_ahead_qty = max(0, prev_queue - trade_consumed)
        if order.queue_ahead_qty > 0:
            return 0
        overflow = trade_consumed - prev_queue
        return min(order.leaves_qty, max(overflow, 0))

    @staticmethod
    def _initial_queue_ahead(intent: OrderIntent, snapshot: BoardSnapshot) -> int:
        if intent.side > 0 and intent.price == snapshot.bid:
            return max(snapshot.bid_size, 0)
        if intent.side < 0 and intent.price == snapshot.ask:
            return max(snapshot.ask_size, 0)
        return 0
