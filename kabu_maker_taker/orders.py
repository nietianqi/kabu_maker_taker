"""Order ledger — tracks every order from intent through final status.

Responsibilities:
- Assign and track client order IDs (sequential, prefixed by role).
- Record broker order IDs returned after submission.
- Apply incremental and cumulative fill events; deduplicate by ``trade_id``.
- Maintain a bounded history of completed orders (``max_final_history``).

The ledger never touches position state — that is the strategy's job after
it calls ``apply_fill_event()`` or ``apply_order_event()`` and receives back
the net fill qty.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace

from .models import BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderState, OrderStatus


class OrderLedger:
    """Small broker-facing order ledger.

    The ledger stores local client ids, optional broker ids, cumulative fill
    state, and final order status. Position updates are handled by the strategy
    only after this ledger reports a real fill delta.
    """

    def __init__(self, max_final_history: int = 10_000) -> None:
        self._orders: dict[str, OrderState] = {}
        # Tracks only non-final order ids; hot-path active scans stay bounded.
        self._active_ids: set[str] = set()
        self._final_ids: deque[str] = deque()
        self._broker_to_client: dict[str, str] = {}
        self._max_final_history = max(0, int(max_final_history))
        self._next_sequence = 1
        # Per-order set of already-applied fill trade_ids for replay deduplication.
        # Cleaned up when an order is pruned from final history.
        self._order_fill_ids: dict[str, set[str]] = {}

    def next_client_order_id(self, prefix: str = "local") -> str:
        value = f"{prefix}-{self._next_sequence}"
        self._next_sequence += 1
        return value

    def add_intent(self, intent: OrderIntent, *, role: str, now_ns: int = 0) -> OrderState:
        client_order_id = intent.client_order_id or self.next_client_order_id(role)
        if intent.client_order_id != client_order_id:
            intent = replace(intent, client_order_id=client_order_id)
        self._bump_sequence_from_id(client_order_id)
        state = OrderState(
            client_order_id=client_order_id,
            intent=intent,
            role=role,
            submitted_ts_ns=now_ns,
            updated_ts_ns=now_ns,
        )
        self._orders[client_order_id] = state
        self._active_ids.add(client_order_id)
        return state

    def restore_order(
        self,
        intent: OrderIntent,
        *,
        role: str,
        status: OrderStatus | str = OrderStatus.WORKING,
        broker_order_id: str = "",
        submitted_ts_ns: int = 0,
        updated_ts_ns: int = 0,
        cum_qty: int = 0,
        avg_fill_price: float = 0.0,
    ) -> OrderState:
        """Restore an already-submitted broker order without applying position fills."""
        client_order_id = intent.client_order_id or self.next_client_order_id(role)
        if intent.client_order_id != client_order_id:
            intent = replace(intent, client_order_id=client_order_id)
        self._bump_sequence_from_id(client_order_id)
        normalized_status = _coerce_status(status)
        normalized_cum = min(max(cum_qty, 0), intent.qty)
        if normalized_cum >= intent.qty:
            normalized_status = OrderStatus.FILLED
        elif normalized_cum > 0 and normalized_status in {OrderStatus.NEW_PENDING, OrderStatus.WORKING, OrderStatus.UNKNOWN}:
            normalized_status = OrderStatus.PARTIALLY_FILLED
        state = OrderState(
            client_order_id=client_order_id,
            intent=intent,
            role=role,
            status=normalized_status,
            broker_order_id=broker_order_id,
            submitted_ts_ns=submitted_ts_ns,
            updated_ts_ns=updated_ts_ns or submitted_ts_ns,
            cum_qty=normalized_cum,
            avg_fill_price=avg_fill_price if normalized_cum > 0 else 0.0,
        )
        self._orders[client_order_id] = state
        if broker_order_id:
            self._broker_to_client[broker_order_id] = client_order_id
        self._sync_active(state)
        return state

    def get(self, order_id: str) -> OrderState | None:
        return self._orders.get(self._resolve(order_id))

    def active(self) -> list[OrderState]:
        # Iterate the set directly — no intermediate list allocation.
        return [self._orders[oid] for oid in self._active_ids if oid in self._orders]

    def active_by_role(self, role: str) -> list[OrderState]:
        # Iterate the set directly — no intermediate list allocation.
        return [
            self._orders[oid]
            for oid in self._active_ids
            if oid in self._orders and self._orders[oid].role == role
        ]

    def mark_cancel_pending(self, order_id: str, reason: str = "", now_ns: int = 0) -> OrderState | None:
        order = self.get(order_id)
        if order is None or order.is_final:
            return order
        order.status = OrderStatus.CANCEL_PENDING
        order.cancel_reason = reason
        order.updated_ts_ns = now_ns
        return order

    def apply_order_event(self, event: BrokerOrderEvent) -> tuple[OrderState | None, int, float]:
        order = self.get(event.order_id)
        if order is None:
            return None, 0, 0.0

        broker_order_id = event.broker_order_id or order.broker_order_id
        if broker_order_id:
            order.broker_order_id = broker_order_id
            self._broker_to_client[broker_order_id] = order.client_order_id

        status = _coerce_status(event.status)
        if not order.is_final:
            order.status = status
        if status == OrderStatus.REJECTED:
            order.reject_reason = event.reason
        if status in {OrderStatus.CANCELED, OrderStatus.CANCEL_PENDING}:
            order.cancel_reason = event.reason or order.cancel_reason

        fill_qty = 0
        fill_price = event.avg_fill_price
        cum_qty = event.cum_qty
        if cum_qty is None and status == OrderStatus.FILLED:
            cum_qty = order.intent.qty
        if cum_qty is not None:
            fill_qty, fill_price = self._apply_cumulative_fill(order, cum_qty, event.avg_fill_price)

        if order.cum_qty >= order.intent.qty:
            order.status = OrderStatus.FILLED
        elif status in {OrderStatus.CANCELED, OrderStatus.REJECTED}:
            order.status = status
        elif order.cum_qty > 0:
            order.status = OrderStatus.PARTIALLY_FILLED

        order.updated_ts_ns = event.ts_ns or order.updated_ts_ns
        self._sync_active(order)
        return order, fill_qty, fill_price

    def apply_fill_event(self, event: BrokerFillEvent) -> tuple[OrderState | None, int, float]:
        order = self.get(event.order_id)
        if order is None:
            return None, 0, 0.0
        if event.broker_order_id:
            order.broker_order_id = event.broker_order_id
            self._broker_to_client[event.broker_order_id] = order.client_order_id
        if event.price <= 0:
            return order, 0, 0.0
        # Deduplicate fills by trade_id to handle broker replays.
        # Fall back to a composite key when trade_id is absent so that empty-id
        # fills (broker replay, simulator) are also deduplicated correctly.
        dedup_key = event.trade_id or f"{event.ts_ns}:{event.qty}:{event.price}"
        seen = self._order_fill_ids.get(order.client_order_id)
        if seen is not None and dedup_key in seen:
            return order, 0, event.price  # duplicate — already applied
        fill_qty = min(max(event.qty, 0), order.leaves_qty)
        if fill_qty <= 0 or order.is_final:
            return order, 0, event.price
        self._apply_incremental_fill(order, fill_qty, event.price)
        self._order_fill_ids.setdefault(order.client_order_id, set()).add(dedup_key)
        order.updated_ts_ns = event.ts_ns or order.updated_ts_ns
        self._sync_active(order)
        return order, fill_qty, event.price

    def snapshot(self) -> dict[str, dict]:
        return {order_id: state.to_dict() for order_id, state in self._orders.items()}

    def _resolve(self, order_id: str) -> str:
        return self._broker_to_client.get(order_id, order_id)

    def _sync_active(self, order: OrderState) -> None:
        """Move final orders out of the active set and trim old history."""
        if order.is_final:
            was_active = order.client_order_id in self._active_ids
            self._active_ids.discard(order.client_order_id)
            if was_active:
                self._final_ids.append(order.client_order_id)
                self._trim_final_history()
            return
        self._active_ids.add(order.client_order_id)

    def _trim_final_history(self) -> None:
        while self._max_final_history >= 0 and len(self._final_ids) > self._max_final_history:
            order_id = self._final_ids.popleft()
            if order_id in self._active_ids:
                continue
            order = self._orders.pop(order_id, None)
            if order is not None and order.broker_order_id:
                self._broker_to_client.pop(order.broker_order_id, None)
            self._order_fill_ids.pop(order_id, None)  # prevent unbounded growth

    def _bump_sequence_from_id(self, client_order_id: str) -> None:
        try:
            suffix = int(str(client_order_id).rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return
        self._next_sequence = max(self._next_sequence, suffix + 1)

    def _apply_cumulative_fill(self, order: OrderState, cum_qty: int, avg_price: float) -> tuple[int, float]:
        normalized_cum = min(max(cum_qty, 0), order.intent.qty)
        if normalized_cum <= order.cum_qty:
            return 0, avg_price or order.avg_fill_price

        old_cum = order.cum_qty
        old_avg = order.avg_fill_price
        fill_qty = normalized_cum - old_cum

        if avg_price > 0:
            if old_cum > 0 and old_avg > 0:
                old_value = old_cum * old_avg
                new_value = normalized_cum * avg_price
                fill_price = (new_value - old_value) / fill_qty
                if fill_price <= 0:
                    fill_price = avg_price
            else:
                fill_price = avg_price
            order.avg_fill_price = avg_price
            order.cum_qty = normalized_cum
        else:
            # Broker cumulative snapshots may omit price. Fall back only to
            # already-known safe anchors, never to intent.price (market orders use 0).
            fill_price = order.avg_fill_price if order.avg_fill_price > 0 else order.intent.reference_price
            if fill_price <= 0:
                return 0, 0.0
            self._apply_incremental_fill(order, fill_qty, fill_price)
        return fill_qty, fill_price

    def _apply_incremental_fill(self, order: OrderState, fill_qty: int, fill_price: float) -> None:
        if fill_qty <= 0 or fill_price <= 0:
            return
        new_cum = min(order.intent.qty, order.cum_qty + fill_qty)
        if new_cum <= 0:
            return
        if order.cum_qty == 0:
            order.avg_fill_price = fill_price
        else:
            prev_value = order.cum_qty * order.avg_fill_price
            fill_value = fill_qty * fill_price
            order.avg_fill_price = (prev_value + fill_value) / max(new_cum, 1)
        order.cum_qty = new_cum
        order.status = OrderStatus.FILLED if order.cum_qty >= order.intent.qty else OrderStatus.PARTIALLY_FILLED


def _coerce_status(status: OrderStatus | str) -> OrderStatus:
    if isinstance(status, OrderStatus):
        return status
    normalized = str(status).strip().lower()
    aliases = {
        "new": OrderStatus.NEW_PENDING,
        "new_pending": OrderStatus.NEW_PENDING,
        "pending": OrderStatus.NEW_PENDING,
        "accepted": OrderStatus.WORKING,
        "working": OrderStatus.WORKING,
        "open": OrderStatus.WORKING,
        "partial": OrderStatus.PARTIALLY_FILLED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "cancel_pending": OrderStatus.CANCEL_PENDING,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
        "unknown": OrderStatus.UNKNOWN,
    }
    return aliases.get(normalized, OrderStatus.UNKNOWN)
