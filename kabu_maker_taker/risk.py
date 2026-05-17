"""Risk enforcement layer — pre-trade checks and circuit breakers.

``RiskManager.can_enter()`` is the final gate before any order is submitted.
It checks, in order:

  1. API / latency circuit breakers (hard stops)
  2. Consecutive loss cooldown
  3. Daily loss limit (JPY)
  4. Spread too wide
  5. Stale quote
  6. Session window (TSE 09:00–11:30 / 12:30–15:25, configurable)
  7. Max inventory size
  8. Max notional value
  9. Entry order rate limit

``record_partial_pnl()`` and ``record_trade_result()`` keep ``_daily_pnl``
current so the daily loss limit responds immediately to every fill, including
partial exits.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from .config import RiskConfig
from .models import BoardSnapshot, EntryDecision, MarketState, PositionState

JST = timezone(timedelta(hours=9))
ONE_MINUTE_NS = 60_000_000_000
URGENT_CANCEL_REASONS = {"abnormal_market", "spread_expanded", "stale_board"}
LATENCY_REQUEST_KINDS = ("submit", "cancel", "poll")


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
        self._latency_last_ms: dict[str, float] = {kind: 0.0 for kind in LATENCY_REQUEST_KINDS}
        self._latency_breach_counts: dict[str, int] = {kind: 0 for kind in LATENCY_REQUEST_KINDS}
        self._latency_circuit_open_until_ns: int = 0
        self._soft_kill_active: bool = False
        self._last_board_ts_ns: int = 0

    def update_board_ts(self, ts_ns: int) -> None:
        """Record the latest board timestamp for inter-board gap tracking."""
        if ts_ns > 0:
            self._last_board_ts_ns = ts_ns

    def is_stale_board(self, ts_ns: int) -> bool:
        """Return True if the gap from the previous board exceeds stale_board_ms."""
        if self.config.stale_board_ms <= 0 or self._last_board_ts_ns <= 0:
            return False
        return (ts_ns - self._last_board_ts_ns) / 1_000_000 > self.config.stale_board_ms

    def set_soft_kill(self, active: bool) -> None:
        """Activate or deactivate the soft kill switch.

        While active, ``can_enter()`` returns ``(False, "kill_switch_soft")``.
        Existing positions and exit orders are unaffected.
        """
        self._soft_kill_active = active

    def can_enter(
        self,
        *,
        snapshot: BoardSnapshot,
        decision: EntryDecision,
        position: PositionState,
        now_ns: int,
        expected_price: float,
        order_qty: int = 0,
        market_state: MarketState = MarketState.NORMAL,
    ) -> tuple[bool, str]:
        if not decision.allow:
            return False, decision.reason
        if self._soft_kill_active:
            return False, "kill_switch_soft"
        if self.is_stale_board(snapshot.ts_ns):
            return False, "stale_board"
        self._ensure_daily_date(now_ns or snapshot.ts_ns)
        if self._api_circuit_open(now_ns):
            return False, "api_circuit_open"
        if self._latency_circuit_open(now_ns):
            return False, "latency_circuit_open"
        if market_state == MarketState.ABNORMAL:
            return False, "market_abnormal"
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
            qty_for_check = order_qty if order_qty > 0 else self.lot_size
            if available_notional < qty_for_check * expected_price:
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

    def record_latency(self, request_kind: str, latency_ms: float, now_ns: int) -> bool:
        kind = request_kind.strip().lower()
        if kind not in LATENCY_REQUEST_KINDS:
            return False
        if self.config.latency_breach_limit <= 0:
            return False
        limit_ms = self._latency_limit_ms(kind)
        if limit_ms <= 0:
            return False

        was_open = self._latency_circuit_open(now_ns)
        self._latency_last_ms[kind] = max(float(latency_ms), 0.0)
        if was_open:
            return False

        if latency_ms <= limit_ms:
            self._latency_breach_counts[kind] = 0
            return False

        self._latency_breach_counts[kind] += 1
        if self._latency_breach_counts[kind] < self.config.latency_breach_limit:
            return False

        self._latency_circuit_open_until_ns = self._cooling_until_ns_from(now_ns)
        return True

    def record_partial_pnl(self, *, pnl: float, qty: int, now_ns: int, count_loss: bool = False) -> float:
        """Update daily PnL for a partial position exit.

        Charges the same round-trip fee/slippage as a full close — correct in
        aggregate because partial costs sum to the same total across all fills.
        ``count_loss`` lets the first losing partial exit trip cooling early
        without letting repeated partial fills from one position spam the streak.
        """
        self._ensure_daily_date(now_ns)
        net_pnl = pnl - self.estimate_round_trip_cost(qty)
        self._daily_pnl += net_pnl
        if count_loss and net_pnl < 0:
            self._record_loss_outcome(False, now_ns)
        return net_pnl

    def restore_daily_pnl(self, pnl: float, now_ns: int = 0) -> None:
        """Restore pre-existing daily realized PnL from broker reconciliation.

        Call once at startup — before any ``can_enter()`` checks — so the daily
        loss limit reflects trades executed before this process started.

        Typical startup sequence::

            strategy.restore_position(side, qty, avg_price, entry_mode, now_ns)
            strategy.restore_daily_pnl(pnl=today_net_pnl, now_ns=now_ns)
            # Feed in-flight BrokerOrderEvent(status=WORKING) for open orders
        """
        self._ensure_daily_date(now_ns)
        self._daily_pnl = float(pnl)

    def record_trade_result(
        self,
        won: bool,
        now_ns: int,
        *,
        pnl: float = 0.0,
        qty: int = 0,
        classification_pnl: float | None = None,
        update_loss_streak: bool = True,
    ) -> float:
        self._ensure_daily_date(now_ns)
        net_pnl = pnl - self.estimate_round_trip_cost(qty)
        self._daily_pnl += net_pnl
        effective_basis = net_pnl if classification_pnl is None else classification_pnl
        effective_win = effective_basis > 0 if qty > 0 or pnl != 0 or classification_pnl is not None else won
        if update_loss_streak:
            self._record_loss_outcome(effective_win, now_ns)
        return net_pnl

    def _record_loss_outcome(self, won: bool, now_ns: int) -> None:
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if (
                self.config.consecutive_loss_limit > 0
                and self._consecutive_losses >= self.config.consecutive_loss_limit
            ):
                self._cooling_until_ns = now_ns + self.config.cooling_seconds * 1_000_000_000

    def estimate_round_trip_cost(self, qty: int) -> float:
        if qty <= 0:
            return 0.0
        fee = max(self.config.fee_per_share, 0.0) * qty * 2
        slip = max(self.config.slippage_ticks_default, 0.0) * self.tick_size * qty * 2
        return fee + slip

    def order_qty(self, *, base_qty: int, position: PositionState, expected_price: float = 0.0) -> int:
        remaining = max(self.config.max_inventory_qty - position.qty, 0)
        qty = min(max(base_qty, 0), remaining)
        if self.config.max_notional > 0 and expected_price > 0:
            current_notional = max(position.qty, 0) * max(position.avg_price, 0.0)
            available_notional = max(self.config.max_notional - current_notional, 0.0)
            qty = min(qty, int(available_notional // expected_price))
        return (qty // self.lot_size) * self.lot_size

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def api_cooling_until_ns(self) -> int:
        return self._api_cooling_until_ns

    @property
    def latency_circuit_open_until_ns(self) -> int:
        return self._latency_circuit_open_until_ns

    def latency_breach_count(self, request_kind: str) -> int:
        return self._latency_breach_counts.get(request_kind.strip().lower(), 0)

    def last_latency_ms(self, request_kind: str) -> float:
        return self._latency_last_ms.get(request_kind.strip().lower(), 0.0)

    def _api_circuit_open(self, now_ns: int) -> bool:
        if self.config.api_error_limit <= 0:
            return False
        if self._api_cooling_until_ns <= 0:
            return False
        if now_ns > 0 and now_ns >= self._api_cooling_until_ns:
            self.record_api_success()
            return False
        return True

    def _latency_circuit_open(self, now_ns: int) -> bool:
        if self.config.latency_breach_limit <= 0:
            return False
        if self._latency_circuit_open_until_ns <= 0:
            return False
        if now_ns > 0 and now_ns >= self._latency_circuit_open_until_ns:
            self._latency_circuit_open_until_ns = 0
            for kind in LATENCY_REQUEST_KINDS:
                self._latency_breach_counts[kind] = 0
            return False
        return True

    def _latency_limit_ms(self, request_kind: str) -> int:
        if request_kind == "submit":
            return self.config.order_latency_limit_ms
        if request_kind == "cancel":
            return self.config.cancel_latency_limit_ms
        if request_kind == "poll":
            return self.config.poll_latency_limit_ms
        return 0

    def _cooling_until_ns_from(self, now_ns: int) -> int:
        cooling_ns = max(self.config.api_cooling_seconds, 0) * 1_000_000_000
        if now_ns <= 0:
            return 1
        return now_ns + cooling_ns

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
