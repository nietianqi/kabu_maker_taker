from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from .config import RiskConfig
from .models import BoardSnapshot, EntryDecision, PositionState

JST = timezone(timedelta(hours=9))
ONE_MINUTE_NS = 60_000_000_000
URGENT_CANCEL_REASONS = {"abnormal_market", "spread_expanded"}


class RiskManager:
    def __init__(self, *, config: RiskConfig, tick_size: float, lot_size: int):
        self.config = config
        self.tick_size = max(tick_size, 1e-9)
        self.lot_size = max(lot_size, 1)
        self._consecutive_losses: int = 0
        self._cooling_until_ns: int = 0
        self._daily_pnl: float = 0.0
        self._daily_date: str = ""
        self._entry_order_times: deque[int] = deque()
        self._cancel_request_times: deque[int] = deque()
        self._api_error_count: int = 0
        self._api_cooling_until_ns: int = 0

    def can_enter(
        self,
        *,
        snapshot: BoardSnapshot,
        decision: EntryDecision,
        position: PositionState,
        now_ns: int,
        expected_price: float,
    ) -> tuple[bool, str]:
        if not decision.allow:
            return False, decision.reason
        self._ensure_daily_date(now_ns or snapshot.ts_ns)
        if self._api_circuit_open(now_ns):
            return False, "api_circuit_open"
        if self.config.daily_loss_limit > 0 and self._daily_pnl <= -self.config.daily_loss_limit:
            return False, "daily_loss_limit"
        if self.config.max_entry_orders_per_minute > 0:
            self._trim_window(self._entry_order_times, now_ns)
            if len(self._entry_order_times) >= self.config.max_entry_orders_per_minute:
                return False, "order_rate_limit"
        if self.config.consecutive_loss_limit > 0:
            if self._consecutive_losses >= self.config.consecutive_loss_limit:
                if now_ns > 0 and now_ns < self._cooling_until_ns:
                    return False, "consecutive_loss_cooling"
                self._consecutive_losses = 0
        if not snapshot.valid:
            return False, "invalid_quote"
        if now_ns > 0 and snapshot.ts_ns > 0:
            stale_ns = self.config.stale_quote_ms * 1_000_000
            if now_ns - snapshot.ts_ns > stale_ns:
                return False, "stale_quote"
        if snapshot.spread > self.config.max_spread_ticks * self.tick_size:
            return False, "spread_too_wide"
        if position.qty > 0 and position.side not in (0, decision.side):
            return False, "opposite_inventory"
        if self.config.enforce_session and not self._session_open(now_ns or snapshot.ts_ns):
            return False, "outside_session"
        if self.config.max_notional > 0 and expected_price > 0:
            available_notional = self.config.max_notional - position.qty * position.avg_price
            if available_notional < self.lot_size * expected_price:
                return False, "notional_limit"
        if position.qty >= self.config.max_inventory_qty:
            return False, "inventory_limit"
        return True, "ok"

    def record_entry_order(self, now_ns: int) -> None:
        if now_ns <= 0:
            return
        self._trim_window(self._entry_order_times, now_ns)
        self._entry_order_times.append(now_ns)

    def can_send_cancel_signal(self, reason: str, now_ns: int) -> tuple[bool, str]:
        if not reason or reason in URGENT_CANCEL_REASONS or self.config.max_cancel_requests_per_minute <= 0:
            return True, ""
        self._trim_window(self._cancel_request_times, now_ns)
        if len(self._cancel_request_times) >= self.config.max_cancel_requests_per_minute:
            return False, "cancel_rate_limit"
        return True, ""

    def record_cancel_request(self, reason: str, now_ns: int) -> None:
        if not reason or reason in URGENT_CANCEL_REASONS or now_ns <= 0:
            return
        self._trim_window(self._cancel_request_times, now_ns)
        self._cancel_request_times.append(now_ns)

    def record_api_error(self, now_ns: int) -> bool:
        if self.config.api_error_limit <= 0:
            return False
        self._api_error_count += 1
        if self._api_error_count >= self.config.api_error_limit:
            self._api_cooling_until_ns = now_ns + self.config.api_cooling_seconds * 1_000_000_000
            return True
        return False

    def record_api_success(self) -> None:
        self._api_error_count = 0
        self._api_cooling_until_ns = 0

    def record_trade_result(self, won: bool, now_ns: int, *, pnl: float = 0.0, qty: int = 0) -> float:
        self._ensure_daily_date(now_ns)
        net_pnl = pnl - self.estimate_round_trip_cost(qty)
        self._daily_pnl += net_pnl
        effective_win = net_pnl > 0 if qty > 0 or pnl != 0 else won
        if effective_win:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if (
                self.config.consecutive_loss_limit > 0
                and self._consecutive_losses >= self.config.consecutive_loss_limit
            ):
                self._cooling_until_ns = now_ns + self.config.cooling_seconds * 1_000_000_000
        return net_pnl

    def estimate_round_trip_cost(self, qty: int) -> float:
        if qty <= 0:
            return 0.0
        fee = max(self.config.fee_per_share, 0.0) * qty * 2
        slip = max(self.config.slippage_ticks_default, 0.0) * self.tick_size * qty * 2
        return fee + slip

    def order_qty(self, *, base_qty: int, position: PositionState) -> int:
        remaining = max(self.config.max_inventory_qty - position.qty, 0)
        qty = min(max(base_qty, 0), remaining)
        return (qty // self.lot_size) * self.lot_size

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def api_cooling_until_ns(self) -> int:
        return self._api_cooling_until_ns

    def _api_circuit_open(self, now_ns: int) -> bool:
        if self.config.api_error_limit <= 0:
            return False
        if self._api_cooling_until_ns <= 0:
            return False
        if now_ns > 0 and now_ns >= self._api_cooling_until_ns:
            self.record_api_success()
            return False
        return True

    def _ensure_daily_date(self, now_ns: int) -> None:
        if now_ns <= 0:
            return
        today = datetime.fromtimestamp(now_ns / 1_000_000_000, JST).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_pnl = 0.0

    def _trim_window(self, events: deque[int], now_ns: int) -> None:
        if now_ns <= 0:
            return
        while events and now_ns - events[0] >= ONE_MINUTE_NS:
            events.popleft()

    def _session_open(self, now_ns: int) -> bool:
        if now_ns <= 0:
            return True
        now = datetime.fromtimestamp(now_ns / 1_000_000_000, JST).time()
        s1h, s1m = _parse_hhmm(self.config.open_start_hhmm)
        e1h, e1m = _parse_hhmm(self.config.open_end_hhmm)
        start1 = now.replace(hour=s1h, minute=s1m, second=0, microsecond=0)
        end1 = now.replace(hour=e1h, minute=e1m, second=0, microsecond=0)
        if start1 <= now <= end1:
            return True
        if self.config.open_start_hhmm_2 and self.config.open_end_hhmm_2:
            s2h, s2m = _parse_hhmm(self.config.open_start_hhmm_2)
            e2h, e2m = _parse_hhmm(self.config.open_end_hhmm_2)
            start2 = now.replace(hour=s2h, minute=s2m, second=0, microsecond=0)
            end2 = now.replace(hour=e2h, minute=e2m, second=0, microsecond=0)
            if start2 <= now <= end2:
                return True
        return False


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)
