"""Broker reconciliation interfaces and adapters.

``BrokerReconciliationSnapshot`` carries a point-in-time view of open
positions and working orders retrieved from the broker at startup.
``CombinedMakerTakerStrategy.reconcile_from_broker()`` uses it to restore
position, lollipop state, and daily PnL before the live event loop begins.

For dry-run / backtesting use ``JsonBrokerSnapshotAdapter`` to load a
pre-saved broker JSON file (same format as the kabu Station REST response).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .models import OrderIntent, OrderStatus


@dataclass(frozen=True, slots=True)
class BrokerPositionSnapshot:
    symbol: str
    side: int
    qty: int
    avg_price: float
    exchange: int = 27
    entry_mode: str = "maker"
    entry_ts_ns: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BrokerPositionSnapshot":
        side = int(payload.get("side", payload.get("Side", 0)))
        signed_qty = int(payload.get("signed_qty", payload.get("SignedQty", 0)))
        qty = int(payload.get("qty", payload.get("Qty", abs(signed_qty))))
        if side == 0 and signed_qty != 0:
            side = 1 if signed_qty > 0 else -1
        return cls(
            symbol=str(payload.get("symbol", payload.get("Symbol", ""))),
            exchange=int(payload.get("exchange", payload.get("Exchange", 27))),
            side=side,
            qty=qty,
            avg_price=float(payload.get("avg_price", payload.get("AvgPrice", 0.0))),
            entry_mode=str(payload.get("entry_mode", payload.get("EntryMode", "maker"))),
            entry_ts_ns=int(payload.get("entry_ts_ns", payload.get("EntryTsNs", 0))),
        )


@dataclass(frozen=True, slots=True)
class BrokerOpenOrderSnapshot:
    symbol: str
    side: int
    qty: int
    price: float
    role: str
    exchange: int = 27
    is_market: bool = False
    strategy: str = ""
    reason: str = ""
    score: int = 0
    reference_price: float = 0.0
    max_slip_ticks: float = 0.0
    client_order_id: str = ""
    broker_order_id: str = ""
    status: OrderStatus = OrderStatus.WORKING
    submitted_ts_ns: int = 0
    updated_ts_ns: int = 0
    cum_qty: int = 0
    avg_fill_price: float = 0.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BrokerOpenOrderSnapshot":
        status = _coerce_order_status(payload.get("status", payload.get("Status", OrderStatus.WORKING)))
        strategy = str(payload.get("strategy", payload.get("Strategy", "")))
        role = str(payload.get("role", payload.get("Role", "")))
        is_market = bool(payload.get("is_market", payload.get("IsMarket", False)))
        if not strategy:
            strategy = "taker" if is_market else "maker"
        if not role:
            role = "exit" if strategy == "lollipop_tp" else "entry"
        return cls(
            symbol=str(payload.get("symbol", payload.get("Symbol", ""))),
            exchange=int(payload.get("exchange", payload.get("Exchange", 27))),
            side=int(payload.get("side", payload.get("Side", 0))),
            qty=int(payload.get("qty", payload.get("Qty", 0))),
            price=float(payload.get("price", payload.get("Price", 0.0))),
            is_market=is_market,
            role=role,
            strategy=strategy,
            reason=str(payload.get("reason", payload.get("Reason", ""))),
            score=int(payload.get("score", payload.get("Score", 0))),
            reference_price=float(payload.get("reference_price", payload.get("ReferencePrice", 0.0))),
            max_slip_ticks=float(payload.get("max_slip_ticks", payload.get("MaxSlipTicks", 0.0))),
            client_order_id=str(payload.get("client_order_id", payload.get("ClientOrderId", ""))),
            broker_order_id=str(payload.get("broker_order_id", payload.get("BrokerOrderId", ""))),
            status=status,
            submitted_ts_ns=int(payload.get("submitted_ts_ns", payload.get("SubmittedTsNs", 0))),
            updated_ts_ns=int(payload.get("updated_ts_ns", payload.get("UpdatedTsNs", 0))),
            cum_qty=int(payload.get("cum_qty", payload.get("CumQty", 0))),
            avg_fill_price=float(payload.get("avg_fill_price", payload.get("AvgFillPrice", 0.0))),
        )

    def to_intent(self) -> OrderIntent:
        return OrderIntent(
            symbol=self.symbol,
            exchange=self.exchange,
            side=self.side,
            qty=self.qty,
            price=self.price,
            is_market=self.is_market,
            strategy=self.strategy,
            reason=self.reason,
            score=self.score,
            reference_price=self.reference_price,
            max_slip_ticks=self.max_slip_ticks,
            client_order_id=self.client_order_id,
        )


@dataclass(frozen=True, slots=True)
class BrokerReconciliationSnapshot:
    ts_ns: int = 0
    daily_pnl: float = 0.0
    positions: tuple[BrokerPositionSnapshot, ...] = field(default_factory=tuple)
    open_orders: tuple[BrokerOpenOrderSnapshot, ...] = field(default_factory=tuple)
    ignored_open_orders: tuple[BrokerOpenOrderSnapshot, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BrokerReconciliationSnapshot":
        positions_payload = payload.get("positions", payload.get("Positions", ()))
        orders_payload = payload.get("open_orders", payload.get("OpenOrders", payload.get("orders", ())))
        ignored_orders_payload = payload.get(
            "ignored_open_orders",
            payload.get("IgnoredOpenOrders", payload.get("ignored_orders", ())),
        )
        return cls(
            ts_ns=int(payload.get("ts_ns", payload.get("timestamp_ns", payload.get("TimestampNs", 0)))),
            daily_pnl=float(payload.get("daily_pnl", payload.get("DailyPnl", 0.0))),
            positions=tuple(BrokerPositionSnapshot.from_dict(item) for item in positions_payload),
            open_orders=tuple(BrokerOpenOrderSnapshot.from_dict(item) for item in orders_payload),
            ignored_open_orders=tuple(BrokerOpenOrderSnapshot.from_dict(item) for item in ignored_orders_payload),
        )


class ReadOnlyBrokerAdapter(Protocol):
    def snapshot(self) -> BrokerReconciliationSnapshot:
        """Return a read-only broker/account snapshot for startup reconciliation."""


class JsonBrokerSnapshotAdapter:
    """Read-only adapter for local reconciliation fixtures.

    This intentionally does not send orders or call a live broker API. It gives
    the strategy the same startup shape a future kabu adapter should provide.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> BrokerReconciliationSnapshot:
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return BrokerReconciliationSnapshot.from_dict(payload)


def _coerce_order_status(value: OrderStatus | str) -> OrderStatus:
    if isinstance(value, OrderStatus):
        return value
    normalized = str(value).strip().lower()
    aliases = {
        "new": OrderStatus.NEW_PENDING,
        "new_pending": OrderStatus.NEW_PENDING,
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
