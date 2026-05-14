from __future__ import annotations

from dataclasses import dataclass

from .models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderStatus


@dataclass(slots=True)
class SimulatedOrder:
    intent: OrderIntent
    leaves_qty: int
    queue_ahead_qty: int


class DryRunSimulator:
    """Minimal broker/fill simulator that emits broker events only.

    It never mutates strategy position directly. Callers must feed returned
    BrokerOrderEvent/BrokerFillEvent objects back through CombinedMakerTakerStrategy.
    """

    def __init__(self, *, tick_size: float, slippage_ticks: float = 0.0) -> None:
        self.tick_size = max(tick_size, 1e-9)
        self.slippage_ticks = max(slippage_ticks, 0.0)
        self._orders: dict[str, SimulatedOrder] = {}

    def submit(self, intent: OrderIntent, snapshot: BoardSnapshot, now_ns: int) -> list[BrokerOrderEvent | BrokerFillEvent]:
        ack = BrokerOrderEvent(
            order_id=intent.client_order_id,
            status=OrderStatus.WORKING,
            ts_ns=now_ns,
            broker_order_id=f"SIM-{intent.client_order_id}",
        )
        if intent.is_market:
            fill_price = self._market_fill_price(intent)
            return [
                ack,
                BrokerFillEvent(
                    order_id=intent.client_order_id,
                    broker_order_id=ack.broker_order_id,
                    qty=intent.qty,
                    price=fill_price,
                    ts_ns=now_ns,
                    trade_id=f"SIMF-{intent.client_order_id}",
                ),
            ]
        self._orders[intent.client_order_id] = SimulatedOrder(
            intent=intent,
            leaves_qty=intent.qty,
            queue_ahead_qty=self._initial_queue_ahead(intent, snapshot),
        )
        return [ack]

    def on_board(self, snapshot: BoardSnapshot, now_ns: int) -> list[BrokerFillEvent]:
        fills: list[BrokerFillEvent] = []
        completed: list[str] = []
        for order_id, order in list(self._orders.items()):
            fill_qty = self._maker_fill_qty(order, snapshot)
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

    def queue_ahead(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        return order.queue_ahead_qty if order is not None else 0

    def _market_fill_price(self, intent: OrderIntent) -> float:
        reference = intent.reference_price or intent.price
        return max(reference + intent.side * self.slippage_ticks * self.tick_size, self.tick_size)

    def _maker_fill_qty(self, order: SimulatedOrder, snapshot: BoardSnapshot) -> int:
        intent = order.intent
        if intent.side > 0:
            if snapshot.ask > 0 and snapshot.ask <= intent.price:
                return order.leaves_qty
            if snapshot.bid == intent.price:
                consumed = max(order.queue_ahead_qty - snapshot.bid_size, 0)
                order.queue_ahead_qty = min(order.queue_ahead_qty, snapshot.bid_size)
                return min(order.leaves_qty, consumed)
            return 0
        if snapshot.bid > 0 and snapshot.bid >= intent.price:
            return order.leaves_qty
        if snapshot.ask == intent.price:
            consumed = max(order.queue_ahead_qty - snapshot.ask_size, 0)
            order.queue_ahead_qty = min(order.queue_ahead_qty, snapshot.ask_size)
            return min(order.leaves_qty, consumed)
        return 0

    @staticmethod
    def _initial_queue_ahead(intent: OrderIntent, snapshot: BoardSnapshot) -> int:
        if intent.side > 0 and intent.price == snapshot.bid:
            return max(snapshot.bid_size, 0)
        if intent.side < 0 and intent.price == snapshot.ask:
            return max(snapshot.ask_size, 0)
        return 0
