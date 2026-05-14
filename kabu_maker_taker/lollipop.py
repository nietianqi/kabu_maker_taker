from __future__ import annotations

import math

from .config import LollipopConfig
from .models import (
    BoardSnapshot,
    LollipopAction,
    LollipopPhase,
    LollipopState,
    OrderIntent,
    PositionState,
)


class LollipopTPManager:
    """
    Stateful take-profit workflow.

    After an entry fill the caller invokes on_entry_fill().  On every subsequent
    board event the caller invokes tick(), which returns a LollipopAction
    describing the next order to submit (or "none" if nothing is needed yet).

    State transitions:
      IDLE ──on_entry_fill()──► SCHEDULED ──delay elapsed──► ACTIVE ──tp fills──► IDLE
                                                                │
                                                         hold timeout
                                                                │
                                                            TIMEOUT ──force-exit fills──► IDLE
    """

    def __init__(self, config: LollipopConfig, tick_size: float, lot_size: int) -> None:
        self.config = config
        self.tick_size = max(tick_size, 1e-9)
        self.lot_size = max(lot_size, 1)
        self.state = LollipopState()

    # ------------------------------------------------------------------
    # Public event API
    # ------------------------------------------------------------------

    def on_entry_fill(self, avg_price: float, entry_mode: str, now_ns: int, entry_side: int = 1) -> None:
        delay_ns = self.config.tp_delay_ms * 1_000_000
        tp = self._calc_tp_price(avg_price, entry_mode, entry_side)
        self.state = LollipopState(
            phase=LollipopPhase.SCHEDULED,
            tp_price=tp,
            entry_mode=entry_mode,
            entry_side=entry_side,
            entry_ts_ns=now_ns,
            submit_after_ns=now_ns + delay_ns,
            retry_count=0,
            force_exit_requested=False,
        )

    def on_exit_fill(self) -> None:
        self.state = LollipopState()

    def reset(self) -> None:
        self.state = LollipopState()

    # ------------------------------------------------------------------
    # Main tick — called on every board event
    # ------------------------------------------------------------------

    def tick(
        self,
        snapshot: BoardSnapshot,
        position: PositionState,
        now_ns: int,
        *,
        symbol: str,
        exchange: int,
    ) -> LollipopAction:
        phase = self.state.phase

        if phase == LollipopPhase.IDLE:
            return LollipopAction(action="none")

        if phase == LollipopPhase.SCHEDULED:
            return self._handle_scheduled(snapshot, position, now_ns, symbol=symbol, exchange=exchange)

        if phase == LollipopPhase.ACTIVE:
            # Stop-loss check before timeout
            if self.config.stop_loss_ticks > 0 and position.qty > 0:
                if position.side > 0:
                    loss = (position.avg_price - snapshot.bid) / self.tick_size
                else:
                    loss = (snapshot.ask - position.avg_price) / self.tick_size
                if loss >= self.config.stop_loss_ticks:
                    self.state.phase = LollipopPhase.TIMEOUT
                    return self._handle_timeout(snapshot, position, now_ns, symbol=symbol, exchange=exchange)

            if self._hold_exceeded(now_ns):
                self.state.phase = LollipopPhase.TIMEOUT
                return self._handle_timeout(snapshot, position, now_ns, symbol=symbol, exchange=exchange)

            return LollipopAction(action="none")

        if phase == LollipopPhase.TIMEOUT:
            return self._handle_timeout(snapshot, position, now_ns, symbol=symbol, exchange=exchange)

        return LollipopAction(action="none")

    # ------------------------------------------------------------------
    # Phase handlers
    # ------------------------------------------------------------------

    def _handle_scheduled(
        self,
        snapshot: BoardSnapshot,
        position: PositionState,
        now_ns: int,
        *,
        symbol: str,
        exchange: int,
    ) -> LollipopAction:
        if now_ns < self.state.submit_after_ns:
            return LollipopAction(action="none")
        if self.state.retry_count >= self.config.max_retries:
            self.state.phase = LollipopPhase.TIMEOUT
            return self._handle_timeout(snapshot, position, now_ns, symbol=symbol, exchange=exchange)
        intent = self._build_tp_intent(position, symbol=symbol, exchange=exchange)
        if intent is None:
            return LollipopAction(action="none")
        self.state.phase = LollipopPhase.ACTIVE
        self.state.retry_count += 1
        return LollipopAction(action="submit_tp", intent=intent)

    def _handle_timeout(
        self,
        snapshot: BoardSnapshot,
        position: PositionState,
        now_ns: int,
        *,
        symbol: str,
        exchange: int,
    ) -> LollipopAction:
        if position.qty <= 0:
            self.state = LollipopState()
            return LollipopAction(action="none")
        intent = self._build_force_exit_intent(snapshot, position, symbol=symbol, exchange=exchange)
        if intent is None:
            return LollipopAction(action="none")
        return LollipopAction(action="force_exit", intent=intent)

    # ------------------------------------------------------------------
    # Intent builders
    # ------------------------------------------------------------------

    def _build_tp_intent(
        self,
        position: PositionState,
        *,
        symbol: str,
        exchange: int,
    ) -> OrderIntent | None:
        if position.qty <= 0 or position.side == 0:
            return None
        qty = _align_qty(position.qty, self.lot_size)
        if qty <= 0:
            return None
        tp_price = self.state.tp_price
        exit_side = -position.side
        return OrderIntent(
            symbol=symbol,
            exchange=exchange,
            side=exit_side,
            qty=qty,
            price=tp_price,
            is_market=False,
            strategy="lollipop_tp",
            reason="limit_tp",
            score=0,
            reference_price=position.avg_price,
        )

    def _build_force_exit_intent(
        self,
        snapshot: BoardSnapshot,
        position: PositionState,
        *,
        symbol: str,
        exchange: int,
    ) -> OrderIntent | None:
        if position.qty <= 0 or position.side == 0:
            return None
        qty = _align_qty(position.qty, self.lot_size)
        if qty <= 0:
            return None
        exit_side = -position.side
        # For a long position exit at bid; for short exit at ask
        exit_price = snapshot.bid if position.side > 0 else snapshot.ask
        return OrderIntent(
            symbol=symbol,
            exchange=exchange,
            side=exit_side,
            qty=qty,
            price=0.0,
            is_market=True,
            strategy="lollipop_tp",
            reason="timeout_exit",
            score=0,
            reference_price=exit_price,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calc_tp_price(self, avg_price: float, entry_mode: str, entry_side: int = 1) -> float:
        tp_ticks = (
            self.config.maker_tp_ticks if entry_mode == "maker" else self.config.taker_tp_ticks
        )
        if entry_side >= 0:
            raw = avg_price + tp_ticks * self.tick_size
            exit_side = -1
        else:
            raw = avg_price - tp_ticks * self.tick_size
            exit_side = 1
        return _align_price(raw, side=exit_side, tick_size=self.tick_size)

    def _hold_exceeded(self, now_ns: int) -> bool:
        max_hold_s = (
            self.config.maker_max_hold_seconds
            if self.state.entry_mode == "maker"
            else self.config.taker_max_hold_seconds
        )
        elapsed_ns = now_ns - self.state.entry_ts_ns
        return elapsed_ns >= max_hold_s * 1_000_000_000

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def phase(self) -> LollipopPhase:
        return self.state.phase

    @property
    def is_busy(self) -> bool:
        return self.state.phase != LollipopPhase.IDLE

    def reschedule(self, now_ns: int) -> None:
        """Re-enter SCHEDULED (e.g. after an external TP cancellation)."""
        if self.state.phase == LollipopPhase.ACTIVE:
            delay_ns = self.config.tp_delay_ms * 1_000_000
            self.state.phase = LollipopPhase.SCHEDULED
            self.state.submit_after_ns = now_ns + delay_ns

    def force_exit_next_tick(self) -> None:
        """Move an active exit workflow to the taker escape path."""
        if self.state.phase != LollipopPhase.IDLE:
            self.state.phase = LollipopPhase.TIMEOUT


def _align_price(price: float, *, side: int, tick_size: float) -> float:
    tick = max(tick_size, 1e-9)
    if price <= 0:
        return 0.0
    steps = price / tick
    snapped = math.ceil(steps - 1e-9) if side > 0 else math.floor(steps + 1e-9)
    return round(max(snapped * tick, tick), 10)


def _align_qty(qty: int, lot_size: int) -> int:
    lot = max(lot_size, 1)
    return (max(qty, 0) // lot) * lot
