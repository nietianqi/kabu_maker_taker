from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    size: int

    @classmethod
    def from_any(cls, value: Any) -> "Level":
        if isinstance(value, Level):
            return value
        if isinstance(value, dict):
            return cls(price=float(value.get("price", value.get("Price", 0.0))), size=int(value.get("size", value.get("Qty", 0))))
        price, size = value
        return cls(price=float(price), size=int(size))


@dataclass(slots=True)
class BoardSnapshot:
    symbol: str
    ts_ns: int
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    bids: tuple[Level, ...] = field(default_factory=tuple)
    asks: tuple[Level, ...] = field(default_factory=tuple)
    exchange: int = 27
    last: float = 0.0
    duplicate: bool = False
    out_of_order: bool = False

    def __post_init__(self) -> None:
        if not self.bids and self.bid > 0:
            self.bids = (Level(self.bid, self.bid_size),)
        if not self.asks and self.ask > 0:
            self.asks = (Level(self.ask, self.ask_size),)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid and self.bid_size >= 0 and self.ask_size >= 0

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        kabu_bidask_reversed: bool = False,
        auto_fix_negative_spread: bool = True,
    ) -> "BoardSnapshot":
        symbol = str(payload.get("symbol", payload.get("Symbol", "")))
        exchange = int(payload.get("exchange", payload.get("Exchange", 27)))
        ts_ns = int(payload.get("ts_ns", payload.get("timestamp_ns", payload.get("ExchangeTimeNs", 0))))

        if "bid" in payload or "ask" in payload:
            bid = float(payload.get("bid", 0.0))
            ask = float(payload.get("ask", 0.0))
            bid_size = int(payload.get("bid_size", 0))
            ask_size = int(payload.get("ask_size", 0))
        else:
            raw_ask_price = float(payload.get("AskPrice", payload.get("Ask", 0.0)))
            raw_bid_price = float(payload.get("BidPrice", payload.get("Bid", 0.0)))
            raw_ask_qty = int(payload.get("AskQty", payload.get("AskSize", 0)))
            raw_bid_qty = int(payload.get("BidQty", payload.get("BidSize", 0)))
            if kabu_bidask_reversed:
                bid, ask = raw_ask_price, raw_bid_price
                bid_size, ask_size = raw_ask_qty, raw_bid_qty
            else:
                bid, ask = raw_bid_price, raw_ask_price
                bid_size, ask_size = raw_bid_qty, raw_ask_qty

        bids = tuple(Level.from_any(level) for level in payload.get("bids", payload.get("Bids", [])))
        asks = tuple(Level.from_any(level) for level in payload.get("asks", payload.get("Asks", [])))
        if auto_fix_negative_spread and bid > ask > 0:
            bid, ask = ask, bid
            bid_size, ask_size = ask_size, bid_size
            bids, asks = asks, bids

        return cls(
            symbol=symbol,
            exchange=exchange,
            ts_ns=ts_ns,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            bids=bids,
            asks=asks,
            last=float(payload.get("last", payload.get("CurrentPrice", 0.0))),
            duplicate=bool(payload.get("duplicate", False)),
            out_of_order=bool(payload.get("out_of_order", False)),
        )


@dataclass(frozen=True, slots=True)
class TradePrint:
    symbol: str
    ts_ns: int
    price: float
    size: int
    side: int
    exchange: int = 27

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradePrint":
        return cls(
            symbol=str(payload.get("symbol", payload.get("Symbol", ""))),
            exchange=int(payload.get("exchange", payload.get("Exchange", 27))),
            ts_ns=int(payload.get("ts_ns", payload.get("timestamp_ns", 0))),
            price=float(payload.get("price", payload.get("Price", 0.0))),
            size=int(payload.get("size", payload.get("Qty", 0))),
            side=1 if int(payload.get("side", payload.get("Side", 0))) > 0 else -1,
        )


@dataclass(frozen=True, slots=True)
class SignalPacket:
    ts_ns: int
    obi_raw: float
    lob_ofi_raw: float
    tape_ofi_raw: float
    micro_momentum_raw: float
    microprice_tilt_raw: float
    microprice: float
    mid: float
    obi_z: float
    lob_ofi_z: float
    tape_ofi_z: float
    micro_momentum_z: float
    microprice_tilt_z: float
    composite: float
    mid_std_ticks: float = 0.0
    microprice_gap_ticks: float = 0.0
    integrated_ofi: float = 0.0
    trade_burst_score: float = 0.0
    # Tape multi-window (T-02 enhancement)
    tape_ofi_1s: float = 0.0
    # Wall detection (T-04)
    wall_ask_detected: bool = False
    wall_bid_detected: bool = False
    wall_ask_consumed: bool = False
    wall_bid_consumed: bool = False
    wall_ask_consumed_ratio: float = 0.0
    wall_bid_consumed_ratio: float = 0.0
    # Cancel imbalance (T-05)
    bid_cancel_ratio: float = 0.0
    ask_cancel_ratio: float = 0.0
    # Price breakout (T-06)
    breakout_long: bool = False
    breakout_short: bool = False
    # Volatility expansion (T-09)
    vol_expansion: bool = False
    # Microprice streak (T-03 enhancement)
    microprice_up_streak: int = 0
    microprice_down_streak: int = 0


@dataclass(slots=True)
class PositionState:
    side: int = 0
    qty: int = 0
    avg_price: float = 0.0
    entry_mode: str = ""
    entry_ts_ns: int = 0

    @property
    def signed_qty(self) -> int:
        return self.side * self.qty


@dataclass(frozen=True, slots=True)
class EntryDecision:
    allow: bool
    reason: str
    entry_mode: str = ""
    side: int = 0
    entry_score: int = 0
    required_confirm: int = 1


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    exchange: int
    side: int
    qty: int
    price: float
    is_market: bool
    strategy: str
    reason: str
    score: int
    reference_price: float
    max_slip_ticks: float = 0.0
    client_order_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrderStatus(str, Enum):
    NEW_PENDING = "new_pending"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class OrderState:
    client_order_id: str
    intent: OrderIntent
    role: str
    status: OrderStatus = OrderStatus.NEW_PENDING
    broker_order_id: str = ""
    submitted_ts_ns: int = 0
    updated_ts_ns: int = 0
    cum_qty: int = 0
    avg_fill_price: float = 0.0
    cancel_reason: str = ""
    reject_reason: str = ""

    @property
    def leaves_qty(self) -> int:
        return max(self.intent.qty - self.cum_qty, 0)

    @property
    def is_final(self) -> bool:
        return self.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["intent"] = self.intent.to_dict()
        payload["leaves_qty"] = self.leaves_qty
        return payload


@dataclass(frozen=True, slots=True)
class BrokerOrderEvent:
    order_id: str
    status: OrderStatus | str
    ts_ns: int = 0
    broker_order_id: str = ""
    cum_qty: int | None = None
    avg_fill_price: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BrokerFillEvent:
    order_id: str
    qty: int
    price: float
    ts_ns: int = 0
    trade_id: str = ""
    broker_order_id: str = ""


class MarketState(str, Enum):
    NORMAL = "normal"
    QUEUE = "queue"
    ABNORMAL = "abnormal"


@dataclass(frozen=True, slots=True)
class StrategyResult:
    intent: OrderIntent | None
    decision: EntryDecision
    signal: SignalPacket | None
    blocked_reason: str = ""
    confirm_progress: int = 0
    exit_intent: OrderIntent | None = None
    # Outbound signal to execution layer: non-empty → cancel the working entry order.
    # Named distinctly from OrderState.cancel_reason (which records what the broker did).
    entry_cancel_signal: str = ""
    entry_cancel_blocked_reason: str = ""
    market_state: MarketState = MarketState.NORMAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict() if self.intent else None,
            "exit_intent": self.exit_intent.to_dict() if self.exit_intent else None,
            "decision": asdict(self.decision),
            "blocked_reason": self.blocked_reason,
            "confirm_progress": self.confirm_progress,
            "signal": asdict(self.signal) if self.signal else None,
            "entry_cancel_signal": self.entry_cancel_signal,
            "entry_cancel_blocked_reason": self.entry_cancel_blocked_reason,
            "market_state": self.market_state.value,
        }


class LollipopPhase(str, Enum):
    IDLE = "idle"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    TIMEOUT = "timeout"


@dataclass(slots=True)
class LollipopState:
    phase: LollipopPhase = LollipopPhase.IDLE
    tp_price: float = 0.0
    entry_mode: str = ""
    entry_side: int = 0
    entry_ts_ns: int = 0
    submit_after_ns: int = 0
    retry_count: int = 0
    force_exit_requested: bool = False


@dataclass(frozen=True, slots=True)
class LollipopAction:
    action: str  # "none" | "submit_tp" | "cancel_tp" | "force_exit"
    intent: OrderIntent | None = None
