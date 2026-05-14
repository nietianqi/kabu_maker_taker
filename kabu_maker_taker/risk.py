from __future__ import annotations

from datetime import datetime, timezone, timedelta

from .config import RiskConfig
from .models import BoardSnapshot, EntryDecision, PositionState

JST = timezone(timedelta(hours=9))


class RiskManager:
    def __init__(self, *, config: RiskConfig, tick_size: float, lot_size: int):
        self.config = config
        self.tick_size = max(tick_size, 1e-9)
        self.lot_size = max(lot_size, 1)
        self._consecutive_losses: int = 0
        self._cooling_until_ns: int = 0
        # Daily loss tracking — resets at JST midnight
        self._daily_pnl: float = 0.0
        self._daily_date: str = ""  # "YYYY-MM-DD" in JST

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
        # Daily loss limit
        if self.config.daily_loss_limit > 0 and self._daily_pnl <= -self.config.daily_loss_limit:
            return False, "daily_loss_limit"
        # Consecutive loss cooldown
        if self.config.consecutive_loss_limit > 0:
            if self._consecutive_losses >= self.config.consecutive_loss_limit:
                if now_ns > 0 and now_ns < self._cooling_until_ns:
                    return False, "consecutive_loss_cooling"
                else:
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

    def record_trade_result(self, won: bool, now_ns: int, *, pnl: float = 0.0) -> None:
        # Daily PnL reset at JST midnight
        if now_ns > 0:
            today = datetime.fromtimestamp(now_ns / 1_000_000_000, JST).strftime("%Y-%m-%d")
            if today != self._daily_date:
                self._daily_date = today
                self._daily_pnl = 0.0
        self._daily_pnl += pnl
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if (
                self.config.consecutive_loss_limit > 0
                and self._consecutive_losses >= self.config.consecutive_loss_limit
            ):
                self._cooling_until_ns = now_ns + self.config.cooling_seconds * 1_000_000_000

    def order_qty(self, *, base_qty: int, position: PositionState) -> int:
        remaining = max(self.config.max_inventory_qty - position.qty, 0)
        qty = min(max(base_qty, 0), remaining)
        return (qty // self.lot_size) * self.lot_size

    def _session_open(self, now_ns: int) -> bool:
        if now_ns <= 0:
            return True
        now = datetime.fromtimestamp(now_ns / 1_000_000_000, JST).time()
        # Window 1 (morning, e.g. 09:00–11:30)
        s1h, s1m = _parse_hhmm(self.config.open_start_hhmm)
        e1h, e1m = _parse_hhmm(self.config.open_end_hhmm)
        start1 = now.replace(hour=s1h, minute=s1m, second=0, microsecond=0)
        end1 = now.replace(hour=e1h, minute=e1m, second=0, microsecond=0)
        if start1 <= now <= end1:
            return True
        # Window 2 — optional (e.g. TSE afternoon 12:30–15:30)
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

