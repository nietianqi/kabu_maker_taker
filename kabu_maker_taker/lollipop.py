"""Take-profit (TP) state machine — the "lollipop" exit manager.

State transitions::

    IDLE ──on_entry_fill()──► SCHEDULED ──(tp_delay expires)──► ACTIVE
                                                                    │
                              ◄──────── on_exit_fill() ────────────┘
                              │
                              ◄──────── timeout ──────────► (force-exit market order)

In ACTIVE state the manager emits a passive limit TP order each tick.
If the limit order is canceled the manager reschedules (re-enters SCHEDULED).
If it is rejected the manager immediately force-exits with a market order.
After ``max_retries`` the strategy falls back to a market order regardless.
"""
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
from .volatility import ATREstimator


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

    def __init__(
        self,
        config: LollipopConfig,
        tick_size: float,
        lot_size: int,
        atr_estimator: ATREstimator | None = None,
    ) -> None:
        self.config = config
        self.tick_size = max(tick_size, 1e-9)
        self.lot_size = max(lot_size, 1)
        self.state = LollipopState()
        self._atr: ATREstimator | None = atr_estimator if config.atr_tp_enabled else None

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

    def on_scale_in_fill(self, new_avg_price: float, entry_mode: str, entry_side: int) -> None:
        """Update TP price after a scale-in fill without resetting the state machine.

        Calling ``on_entry_fill()`` while ACTIVE would reset the phase to SCHEDULED,
        orphaning the already-submitted TP limit order and causing a second TP to be
        submitted on the next tick (double-exit risk).  This method only recalculates
        ``tp_price`` in-place, preserving the current phase, retry_count, and
        submit_after_ns.
        """
        if self.state.phase == LollipopPhase.IDLE:
            # Defensive fallback: no active workflow — treat as a fresh entry.
            self.on_entry_fill(new_avg_price, entry_mode, now_ns=0, entry_side=entry_side)
            return
        self.state.tp_price = self._calc_tp_price(new_avg_price, entry_mode, entry_side)

    def on_exit_fill(self) -> None:
        self.state = LollipopState()

    def reset(self) -> None:
        self.state = LollipopState()

    def restore_active_exit(
        self,
        *,
        tp_price: float,
        entry_mode: str,
        entry_side: int,
        entry_ts_ns: int,
        retry_count: int = 1,
    ) -> None:
        """Restore an already-working TP order from broker reconciliation."""
        self.state = LollipopState(
            phase=LollipopPhase.ACTIVE,
            tp_price=tp_price,
            entry_mode=entry_mode,
            entry_side=entry_side,
            entry_ts_ns=entry_ts_ns,
            submit_after_ns=0,
            retry_count=max(retry_count, 1),
            force_exit_requested=False,
        )

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
            # Stop-loss check before timeout.
            # Guard bid/ask > 0: a zero price (uninitialized board, auction, data gap)
            # would produce a huge loss value and trigger a spurious forced exit.
            if self.config.stop_loss_ticks > 0 and position.qty > 0 and snapshot.bid > 0 and snapshot.ask > 0:
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
        if self.state.force_exit_requested:
            # Already emitted once; wait for fill or an explicit reset_force_exit() call.
            return LollipopAction(action="none")
        intent = self._build_force_exit_intent(snapshot, position, symbol=symbol, exchange=exchange)
        if intent is None:
            return LollipopAction(action="none")
        self.state.force_exit_requested = True
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
        if tp_price <= 0:
            return None  # safety: never submit a zero-price limit order
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
        base_ticks = (
            self.config.maker_tp_ticks if entry_mode == "maker" else self.config.taker_tp_ticks
        )
        if self._atr is not None and self._atr.atr_ticks > 0.0:
            atr_ticks = self._atr.atr_ticks * self.config.atr_tp_multiplier
            max_ticks = self.config.atr_tp_max_ticks if self.config.atr_tp_max_ticks > 0 else atr_ticks
            tp_ticks = min(max(base_ticks, atr_ticks), max_ticks)
        else:
            tp_ticks = base_ticks
        if entry_side >= 0:
            raw = avg_price + tp_ticks * self.tick_size
            exit_side = -1
        else:
            raw = avg_price - tp_ticks * self.tick_size
            exit_side = 1
        aligned = _align_price(raw, side=exit_side, tick_size=self.tick_size)
        return max(aligned, self.tick_size)

    def _hold_exceeded(self, now_ns: int) -> bool:
        max_hold_s = (
            self.config.maker_max_hold_seconds
            if self.state.entry_mode == "maker"
            else self.config.taker_max_hold_seconds
        )
        if max_hold_s <= 0:
            return False
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

    def reset_force_exit(self) -> None:
        """Allow re-emitting force_exit on the next tick.

        Call after a force-exit order is cancelled so the manager retries.
        No-op when not in TIMEOUT phase.
        """
        if self.state.phase == LollipopPhase.TIMEOUT:
            self.state.force_exit_requested = False

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
