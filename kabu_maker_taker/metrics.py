from __future__ import annotations

from dataclasses import dataclass

from .models import BoardSnapshot, OrderIntent, OrderState
from .strategy import ENTRY_MODE_MAKER, ENTRY_MODE_TAKER, ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT


@dataclass(slots=True)
class PendingMarkout:
    side: int
    reference_price: float
    remaining_boards: int


class MetricsCollector:
    def __init__(self, *, tick_size: float, markout_horizon_boards: int = 3) -> None:
        self.tick_size = max(tick_size, 1e-9)
        self.markout_horizon_boards = max(markout_horizon_boards, 1)
        self.entry_intent_count = 0
        self.maker_entry_intent_count = 0
        self.taker_entry_intent_count = 0
        self.maker_fill_count = 0
        self.taker_fill_count = 0
        self.limit_exit_count = 0
        self.market_exit_count = 0
        self.cancel_signal_count = 0
        self.cancel_blocked_count = 0
        self.order_rate_blocks = 0
        self.cancel_rate_blocks = 0
        self.api_circuit_opens = 0
        self.realized_pnl = 0.0
        self.closed_trades = 0
        self.total_hold_ns = 0
        self.markout_sum_ticks = 0.0
        self.markout_count = 0
        self._pending_markouts: list[PendingMarkout] = []

    def on_board(self, snapshot: BoardSnapshot) -> None:
        if snapshot.mid <= 0:
            return
        ready: list[PendingMarkout] = []
        still_pending: list[PendingMarkout] = []
        for pending in self._pending_markouts:
            pending.remaining_boards -= 1
            if pending.remaining_boards <= 0:
                ready.append(pending)
            else:
                still_pending.append(pending)
        self._pending_markouts = still_pending
        for pending in ready:
            markout = pending.side * (snapshot.mid - pending.reference_price) / self.tick_size
            self.markout_sum_ticks += markout
            self.markout_count += 1

    def record_entry_intent(self, intent: OrderIntent) -> None:
        self.entry_intent_count += 1
        if intent.strategy == ENTRY_MODE_TAKER:
            self.taker_entry_intent_count += 1
        elif intent.strategy == ENTRY_MODE_MAKER:
            self.maker_entry_intent_count += 1
        self._pending_markouts.append(
            PendingMarkout(
                side=intent.side,
                reference_price=intent.reference_price or intent.price,
                remaining_boards=self.markout_horizon_boards,
            )
        )

    def record_exit_intent(self, intent: OrderIntent) -> None:
        if intent.is_market:
            self.market_exit_count += 1
        else:
            self.limit_exit_count += 1

    def record_fill(self, order: OrderState, outcome: str) -> None:
        if order.role == ORDER_ROLE_ENTRY and outcome == "entry":
            if order.intent.strategy == ENTRY_MODE_TAKER:
                self.taker_fill_count += 1
            elif order.intent.strategy == ENTRY_MODE_MAKER:
                self.maker_fill_count += 1
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

    def record_api_circuit_open(self) -> None:
        self.api_circuit_opens += 1

    def record_trade_close(self, *, pnl: float, hold_ns: int) -> None:
        self.realized_pnl += pnl
        self.closed_trades += 1
        self.total_hold_ns += max(hold_ns, 0)

    def to_dict(self) -> dict[str, float | int]:
        avg_hold_seconds = self.total_hold_ns / self.closed_trades / 1_000_000_000 if self.closed_trades else 0.0
        avg_markout_ticks = self.markout_sum_ticks / self.markout_count if self.markout_count else 0.0
        return {
            "entry_intent_count": self.entry_intent_count,
            "maker_entry_intent_count": self.maker_entry_intent_count,
            "taker_entry_intent_count": self.taker_entry_intent_count,
            "maker_fill_count": self.maker_fill_count,
            "taker_fill_count": self.taker_fill_count,
            "limit_exit_count": self.limit_exit_count,
            "market_exit_count": self.market_exit_count,
            "cancel_signal_count": self.cancel_signal_count,
            "cancel_blocked_count": self.cancel_blocked_count,
            "order_rate_blocks": self.order_rate_blocks,
            "cancel_rate_blocks": self.cancel_rate_blocks,
            "api_circuit_opens": self.api_circuit_opens,
            "realized_pnl": self.realized_pnl,
            "closed_trades": self.closed_trades,
            "average_hold_seconds": avg_hold_seconds,
            "markout_count": self.markout_count,
            "average_markout_ticks": avg_markout_ticks,
        }
