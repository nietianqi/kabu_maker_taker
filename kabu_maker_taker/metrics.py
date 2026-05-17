"""Performance-tracking metrics for the combined maker/taker strategy.

Collects per-session counters (fills, PnL, win/loss) and computes markout
ticks at configurable board-count and wall-clock horizons.  Everything is
append-only or in-place; no external I/O.

Hot-path note: ``on_board()`` is called on every market tick.  The pending
markout lists are only processed when non-empty, avoiding unnecessary list
allocations on the vast majority of ticks.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import BoardSnapshot, OrderIntent, OrderState
from .strategy import ENTRY_MODE_MAKER, ENTRY_MODE_TAKER, ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT


@dataclass(slots=True)
class PendingMarkout:
    side: int
    reference_price: float
    remaining_boards: int
    setup_type: str = ""


@dataclass(frozen=True, slots=True)
class MarkoutBucket:
    name: str
    horizon_ns: int


@dataclass(slots=True)
class PendingTimedMarkout:
    side: int
    reference_price: float
    target_ts_ns: int
    bucket_name: str


class MetricsCollector:
    def __init__(self, *, tick_size: float, markout_horizon_boards: int = 3) -> None:
        self.tick_size = max(tick_size, 1e-9)
        self.markout_horizon_boards = max(markout_horizon_boards, 1)
        self.markout_buckets = (
            MarkoutBucket("100ms", 100_000_000),
            MarkoutBucket("500ms", 500_000_000),
            MarkoutBucket("1s", 1_000_000_000),
            MarkoutBucket("3s", 3_000_000_000),
        )
        self.entry_intent_count = 0
        self.maker_entry_intent_count = 0
        self.taker_entry_intent_count = 0
        self.maker_fill_count = 0
        self.taker_fill_count = 0
        # Exit fill counts (incremented by record_fill on actual broker fills)
        self.limit_exit_count = 0
        self.market_exit_count = 0
        # Exit intent submission counts (incremented by record_exit_intent)
        self.limit_exit_submitted = 0
        self.market_exit_submitted = 0
        self.cancel_signal_count = 0
        self.cancel_blocked_count = 0
        self.order_rate_blocks = 0
        self.cancel_rate_blocks = 0
        self.api_circuit_opens = 0
        self.latency_circuit_opens = 0
        self.latency_blocks = 0
        self.realized_pnl = 0.0
        self.partial_exit_pnl = 0.0
        self.closed_trades = 0
        self.win_count = 0
        self.loss_count = 0
        self.total_hold_ns = 0
        self.markout_sum_ticks = 0.0
        self.markout_count = 0
        self._entry_setup_count: dict[str, int] = {}
        self._fill_setup_count: dict[str, int] = {}
        self._setup_markout_sum_ticks: dict[str, float] = {}
        self._setup_markout_count: dict[str, int] = {}
        self._pending_markouts: list[PendingMarkout] = []
        self._timed_markout_sum_ticks = {bucket.name: 0.0 for bucket in self.markout_buckets}
        self._timed_markout_count = {bucket.name: 0 for bucket in self.markout_buckets}
        self._pending_timed_markouts: list[PendingTimedMarkout] = []
        self._rest_latency_count = {"submit": 0, "cancel": 0, "poll": 0}
        self._rest_latency_sum_ms = {"submit": 0.0, "cancel": 0.0, "poll": 0.0}
        self._rest_latency_max_ms = {"submit": 0.0, "cancel": 0.0, "poll": 0.0}
        self._rest_latency_last_ms = {"submit": 0.0, "cancel": 0.0, "poll": 0.0}

    def on_board(self, snapshot: BoardSnapshot) -> None:
        if snapshot.mid <= 0:
            return
        mid = snapshot.mid
        tick = self.tick_size

        # Board-based markouts: decrement counters and flush when horizon reached.
        # Skip the loop entirely (no allocation) when nothing is pending —
        # the common case on most ticks.
        if self._pending_markouts:
            still: list[PendingMarkout] = []
            for pending in self._pending_markouts:
                pending.remaining_boards -= 1
                if pending.remaining_boards <= 0:
                    markout = pending.side * (mid - pending.reference_price) / tick
                    self.markout_sum_ticks += markout
                    self.markout_count += 1
                    setup = self._setup_key(pending.setup_type)
                    if setup:
                        self._setup_markout_sum_ticks[setup] = self._setup_markout_sum_ticks.get(setup, 0.0) + markout
                        self._setup_markout_count[setup] = self._setup_markout_count.get(setup, 0) + 1
                else:
                    still.append(pending)
            self._pending_markouts = still

        # Timed markouts: flush entries whose wall-clock horizon has passed.
        if self._pending_timed_markouts:
            ts = snapshot.ts_ns
            still_timed: list[PendingTimedMarkout] = []
            for pending in self._pending_timed_markouts:
                if ts >= pending.target_ts_ns:
                    markout = pending.side * (mid - pending.reference_price) / tick
                    self._timed_markout_sum_ticks[pending.bucket_name] += markout
                    self._timed_markout_count[pending.bucket_name] += 1
                else:
                    still_timed.append(pending)
            self._pending_timed_markouts = still_timed

    def record_entry_intent(self, intent: OrderIntent, *, now_ns: int = 0) -> None:
        self.entry_intent_count += 1
        if intent.strategy == ENTRY_MODE_TAKER:
            self.taker_entry_intent_count += 1
        elif intent.strategy == ENTRY_MODE_MAKER:
            self.maker_entry_intent_count += 1
        setup = self._setup_key(intent.setup_type)
        if setup:
            self._entry_setup_count[setup] = self._entry_setup_count.get(setup, 0) + 1
        reference_price = intent.reference_price or intent.price
        self._pending_markouts.append(
            PendingMarkout(
                side=intent.side,
                reference_price=reference_price,
                remaining_boards=self.markout_horizon_boards,
                setup_type=setup,
            )
        )
        if now_ns > 0:
            for bucket in self.markout_buckets:
                self._pending_timed_markouts.append(
                    PendingTimedMarkout(
                        side=intent.side,
                        reference_price=reference_price,
                        target_ts_ns=now_ns + bucket.horizon_ns,
                        bucket_name=bucket.name,
                    )
                )

    def record_partial_exit(self, *, pnl: float) -> None:
        """Accumulate realized PnL from a partial position exit.

        Updates ``realized_pnl`` and ``partial_exit_pnl`` but does NOT
        increment ``closed_trades`` — a partial fill is not a full trade close.
        """
        self.realized_pnl += pnl
        self.partial_exit_pnl += pnl

    def record_exit_intent(self, intent: OrderIntent) -> None:
        """Count exit orders submitted to the broker (independent of whether they fill)."""
        if intent.is_market:
            self.market_exit_submitted += 1
        else:
            self.limit_exit_submitted += 1

    def record_fill(self, order: OrderState, outcome: str) -> None:
        if order.role == ORDER_ROLE_ENTRY and outcome == "entry":
            if order.intent.strategy == ENTRY_MODE_TAKER:
                self.taker_fill_count += 1
            elif order.intent.strategy == ENTRY_MODE_MAKER:
                self.maker_fill_count += 1
            setup = self._setup_key(order.intent.setup_type)
            if setup:
                self._fill_setup_count[setup] = self._fill_setup_count.get(setup, 0) + 1
        elif order.role == ORDER_ROLE_EXIT and outcome == "exit":
            if order.intent.is_market:
                self.market_exit_count += 1
            else:
                self.limit_exit_count += 1

    def record_cancel_signal(self, *, blocked_reason: str = "") -> None:
        if blocked_reason:
            self.cancel_blocked_count += 1
            if blocked_reason == "cancel_rate_limit":
                self.cancel_rate_blocks += 1
        else:
            self.cancel_signal_count += 1

    def record_risk_block(self, reason: str) -> None:
        if reason == "order_rate_limit":
            self.order_rate_blocks += 1
        elif reason == "api_circuit_open":
            self.api_circuit_opens += 1
        elif reason == "latency_circuit_open":
            self.latency_blocks += 1

    def record_api_circuit_open(self) -> None:
        self.api_circuit_opens += 1

    def record_latency_circuit_open(self) -> None:
        self.latency_circuit_opens += 1

    def record_rest_latency(self, request_kind: str, latency_ms: float) -> None:
        kind = request_kind.strip().lower()
        if kind not in self._rest_latency_count:
            return
        value = max(float(latency_ms), 0.0)
        self._rest_latency_count[kind] += 1
        self._rest_latency_sum_ms[kind] += value
        self._rest_latency_max_ms[kind] = max(self._rest_latency_max_ms[kind], value)
        self._rest_latency_last_ms[kind] = value

    def record_trade_close(self, *, pnl: float, hold_ns: int, classification_pnl: float | None = None) -> None:
        self.realized_pnl += pnl
        self.closed_trades += 1
        self.total_hold_ns += max(hold_ns, 0)
        result_pnl = pnl if classification_pnl is None else classification_pnl
        if result_pnl > 0:
            self.win_count += 1
        elif result_pnl < 0:
            self.loss_count += 1

    def to_dict(self) -> dict[str, float | int]:
        avg_hold_seconds = self.total_hold_ns / self.closed_trades / 1_000_000_000 if self.closed_trades else 0.0
        avg_markout_ticks = self.markout_sum_ticks / self.markout_count if self.markout_count else 0.0
        payload = {
            "entry_intent_count": self.entry_intent_count,
            "maker_entry_intent_count": self.maker_entry_intent_count,
            "taker_entry_intent_count": self.taker_entry_intent_count,
            "maker_fill_count": self.maker_fill_count,
            "taker_fill_count": self.taker_fill_count,
            "limit_exit_count": self.limit_exit_count,
            "market_exit_count": self.market_exit_count,
            "limit_exit_submitted": self.limit_exit_submitted,
            "market_exit_submitted": self.market_exit_submitted,
            "cancel_signal_count": self.cancel_signal_count,
            "cancel_blocked_count": self.cancel_blocked_count,
            "order_rate_blocks": self.order_rate_blocks,
            "cancel_rate_blocks": self.cancel_rate_blocks,
            "api_circuit_opens": self.api_circuit_opens,
            "latency_circuit_opens": self.latency_circuit_opens,
            "latency_blocks": self.latency_blocks,
            "realized_pnl": self.realized_pnl,
            "partial_exit_pnl": self.partial_exit_pnl,
            "closed_trades": self.closed_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_count / self.closed_trades if self.closed_trades else 0.0,
            "average_hold_seconds": avg_hold_seconds,
            "markout_count": self.markout_count,
            "average_markout_ticks": avg_markout_ticks,
        }
        for bucket in self.markout_buckets:
            count = self._timed_markout_count[bucket.name]
            total = self._timed_markout_sum_ticks[bucket.name]
            slug = bucket.name
            payload[f"markout_{slug}_count"] = count
            payload[f"average_markout_{slug}_ticks"] = total / count if count else 0.0
        setup_keys = set(self._entry_setup_count) | set(self._fill_setup_count) | set(self._setup_markout_count)
        for setup in sorted(setup_keys):
            entry_count = self._entry_setup_count.get(setup, 0)
            fill_count = self._fill_setup_count.get(setup, 0)
            markout_count = self._setup_markout_count.get(setup, 0)
            markout_total = self._setup_markout_sum_ticks.get(setup, 0.0)
            payload[f"entry_setup_{setup}_count"] = entry_count
            payload[f"fill_setup_{setup}_count"] = fill_count
            payload[f"markout_setup_{setup}_count"] = markout_count
            payload[f"average_markout_setup_{setup}_ticks"] = markout_total / markout_count if markout_count else 0.0
        for kind in ("submit", "cancel", "poll"):
            count = self._rest_latency_count[kind]
            total = self._rest_latency_sum_ms[kind]
            payload[f"{kind}_latency_ms_count"] = count
            payload[f"{kind}_latency_ms_max"] = self._rest_latency_max_ms[kind]
            payload[f"{kind}_latency_ms_avg"] = total / count if count else 0.0
            payload[f"{kind}_latency_ms_last"] = self._rest_latency_last_ms[kind]
        return payload

    def _setup_key(self, setup_type: str) -> str:
        setup = (setup_type or "").strip().lower()
        if not setup:
            return ""
        return "".join(ch if ch.isalnum() else "_" for ch in setup).strip("_")
