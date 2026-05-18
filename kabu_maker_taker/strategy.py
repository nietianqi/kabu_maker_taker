"""Entry policy layer — MakerStrategy, TakerStrategy, and supporting utilities.

Responsibilities:
- ``MakerStrategy``:  Evaluates whether conditions favour a passive limit order
  and computes a fair/reservation price, quote price, and cancel signals.
- ``TakerStrategy``:  Evaluates breakout conditions for aggressive market-order entry.
- ``ConfirmationTracker``:  Counts consecutive ticks satisfying entry criteria.
- ``MarketStateDetector``:  Flags abnormal market conditions (wide spread, high
  event rate, large price jumps).

Module-level constants exposed for cross-module use:
  ``ENTRY_MODE_MAKER / ENTRY_MODE_TAKER`` — order strategy label strings.
  ``ORDER_ROLE_ENTRY / ORDER_ROLE_EXIT``  — order ledger role labels.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .config import MarketStateConfig, StrategyConfig
from .models import (
    BoardSnapshot,
    EntryDecision,
    MakerQuoteDiagnostics,
    MarketState,
    OrderIntent,
    PositionState,
    SignalPacket,
)

ENTRY_MODE_MAKER = "maker"
ENTRY_MODE_TAKER = "taker"
ORDER_ROLE_ENTRY = "entry"
ORDER_ROLE_EXIT = "exit"
QUOTE_MODE_PASSIVE_FAIR_VALUE = "PASSIVE_FAIR_VALUE"
QUOTE_MODE_QUEUE_DEFENSE = "QUEUE_DEFENSE"
QUOTE_MODE_CLOSE_ONLY = "CLOSE_ONLY"
_SPECIAL_QUOTE_SIGNS = frozenset({"0102", "0103", "0107"})


@dataclass(frozen=True, slots=True)
class MarketStateDiagnostics:
    state: MarketState
    reason: str = ""
    spread_ticks: float = 0.0
    event_rate_hz: float = 0.0
    stale_ms: float = 0.0
    jump_ticks: float = 0.0
    trade_lag_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class EntryLayerDiagnostics:
    direction: int
    direction_score: int = 0
    confirmation_score: int = 0
    trigger_score: int = 0
    filter_score: int = 0
    book: bool = False
    microprice_tilt: bool = False
    lob_ofi: bool = False
    tape: bool = False
    micro_momentum: bool = False
    opposite_light: bool = False
    integrated_ofi_aligned: bool = False

    @property
    def entry_score(self) -> int:
        return self.direction_score + self.confirmation_score + self.trigger_score + self.filter_score


def entry_layer_diagnostics(
    snapshot: BoardSnapshot,
    signal: SignalPacket,
    config: StrategyConfig,
    *,
    direction: int,
) -> EntryLayerDiagnostics:
    sign = 1 if direction >= 0 else -1
    book = sign * signal.obi_raw >= config.book_imbalance_long
    tilt = sign * signal.microprice_tilt_raw >= config.microprice_tilt_long
    lob = sign * signal.lob_ofi_raw >= config.of_imbalance_long
    tape = sign * signal.tape_ofi_raw >= config.tape_imbalance_long
    momentum = sign * signal.micro_momentum_raw >= config.mom_long_threshold
    opposite_light = _opposite_depth(snapshot, direction) <= _same_side_depth(snapshot, direction)
    integrated = sign * signal.integrated_ofi > 0.0
    # Microprice streak bonus (+1 direction score when recent microprice consistently moves our way)
    streak_min = config.microprice_streak_min
    streak_bonus = (
        1 if streak_min > 0 and (
            (sign > 0 and signal.microprice_up_streak >= streak_min)
            or (sign < 0 and signal.microprice_down_streak >= streak_min)
        ) else 0
    )
    return EntryLayerDiagnostics(
        direction=direction,
        direction_score=(2 if book else 0) + (2 if tilt else 0) + streak_bonus,
        confirmation_score=(2 if lob else 0) + (3 if tape else 0),
        trigger_score=2 if momentum else 0,
        filter_score=(1 if opposite_light else 0) + (1 if integrated else 0),
        book=book,
        microprice_tilt=tilt,
        lob_ofi=lob,
        tape=tape,
        micro_momentum=momentum,
        opposite_light=opposite_light,
        integrated_ofi_aligned=integrated,
    )


def primary_checks_pass(diagnostics: EntryLayerDiagnostics) -> bool:
    return (diagnostics.book or diagnostics.microprice_tilt) and (
        diagnostics.lob_ofi or diagnostics.tape
    ) and diagnostics.micro_momentum


class MarketStateDetector:
    """Classifies each board tick as NORMAL, QUEUE, or ABNORMAL."""

    def __init__(self, config: MarketStateConfig, tick_size: float, *, stale_quote_ms: int = 2000):
        self.config = config
        self.tick_size = max(tick_size, 1e-9)
        self.stale_quote_ms = max(int(stale_quote_ms), 0)
        self._prev_mid: float = 0.0
        # Hard-bound the deque: at most 2× the max expected events in the window.
        _max = max(int(config.abnormal_event_rate_hz * config.event_rate_window_seconds * 2), 64)
        self._event_times: deque[int] = deque(maxlen=_max)
        self._state: MarketState = MarketState.NORMAL
        self._last_diagnostics = MarketStateDiagnostics(MarketState.NORMAL, reason="init")

    def update(self, snapshot: BoardSnapshot, now_ns: int) -> MarketState:
        diagnostics = self.evaluate(snapshot, now_ns)
        self._state = diagnostics.state
        self._last_diagnostics = diagnostics
        return self._state

    def evaluate(self, snapshot: BoardSnapshot, now_ns: int) -> MarketStateDiagnostics:
        if not self.config.enabled:
            diagnostics = self._diagnostics(
                MarketState.NORMAL,
                "disabled",
                snapshot=snapshot,
                now_ns=now_ns,
                event_rate_hz=0.0,
                jump_ticks=0.0,
            )
            if snapshot.mid > 0:
                self._prev_mid = snapshot.mid
            return diagnostics

        window_ns = self.config.event_rate_window_seconds * 1_000_000_000
        event_ts = snapshot.ts_ns if snapshot.ts_ns > 0 else now_ns
        self._event_times.append(event_ts)
        while self._event_times and event_ts - self._event_times[0] > window_ns:
            self._event_times.popleft()
        legacy_event_rate_hz = len(self._event_times) / max(self.config.event_rate_window_seconds, 1)
        if len(self._event_times) >= 2:
            duration_ns = max(self._event_times[-1] - self._event_times[0], 1)
            event_rate_hz = max((len(self._event_times) - 1) * 1_000_000_000 / duration_ns, legacy_event_rate_hz)
        else:
            event_rate_hz = legacy_event_rate_hz

        price_jump_ticks = 0.0
        if self._prev_mid > 0 and snapshot.mid > 0:
            price_jump_ticks = abs(snapshot.mid - self._prev_mid) / self.tick_size
        if snapshot.mid > 0:
            self._prev_mid = snapshot.mid

        spread_ticks = snapshot.spread / self.tick_size if snapshot.spread > 0 else 0.0
        event_burst = (
            legacy_event_rate_hz >= self.config.abnormal_event_rate_hz
            or (
                len(self._event_times) >= max(self.config.event_burst_min_events, 1)
                and event_rate_hz >= self.config.abnormal_event_rate_hz
            )
        )
        diagnostics_kwargs = {
            "snapshot": snapshot,
            "now_ns": now_ns,
            "event_rate_hz": event_rate_hz,
            "jump_ticks": price_jump_ticks,
        }
        if not snapshot.valid:
            return self._diagnostics(MarketState.ABNORMAL, "invalid_quote", **diagnostics_kwargs)
        if _is_special_quote_sign(snapshot.bid_sign) or _is_special_quote_sign(snapshot.ask_sign):
            return self._diagnostics(MarketState.ABNORMAL, "special_quote_sign", **diagnostics_kwargs)
        if self.stale_quote_ms > 0 and now_ns > 0 and snapshot.ts_ns > 0:
            if now_ns - snapshot.ts_ns > self.stale_quote_ms * 1_000_000:
                return self._diagnostics(MarketState.ABNORMAL, "stale_quote", **diagnostics_kwargs)
        if spread_ticks >= self.config.abnormal_spread_ticks:
            return self._diagnostics(MarketState.ABNORMAL, "spread_blowout", **diagnostics_kwargs)
        if event_burst:
            return self._diagnostics(MarketState.ABNORMAL, "event_burst", **diagnostics_kwargs)
        if price_jump_ticks >= self.config.abnormal_price_jump_ticks:
            return self._diagnostics(MarketState.ABNORMAL, "price_jump", **diagnostics_kwargs)
        if spread_ticks > 0 and spread_ticks <= self.config.queue_spread_max_ticks:
            return self._diagnostics(MarketState.QUEUE, "one_tick_queue", **diagnostics_kwargs)
        return self._diagnostics(MarketState.NORMAL, "normal_flow", **diagnostics_kwargs)

    def _diagnostics(
        self,
        state: MarketState,
        reason: str,
        *,
        snapshot: BoardSnapshot,
        now_ns: int,
        event_rate_hz: float,
        jump_ticks: float,
    ) -> MarketStateDiagnostics:
        stale_ms = max((now_ns - snapshot.ts_ns) / 1_000_000, 0.0) if now_ns > 0 and snapshot.ts_ns > 0 else 0.0
        quote_ts = max(snapshot.bid_ts_ns, snapshot.ask_ts_ns)
        trade_lag_ms = (
            max((quote_ts - snapshot.current_ts_ns) / 1_000_000, 0.0)
            if quote_ts > 0 and snapshot.current_ts_ns > 0
            else 0.0
        )
        spread_ticks = snapshot.spread / self.tick_size if snapshot.spread > 0 else 0.0
        return MarketStateDiagnostics(
            state=state,
            reason=reason,
            spread_ticks=spread_ticks,
            event_rate_hz=event_rate_hz,
            stale_ms=stale_ms,
            jump_ticks=jump_ticks,
            trade_lag_ms=trade_lag_ms,
        )

    @property
    def state(self) -> MarketState:
        return self._state

    @property
    def last_diagnostics(self) -> MarketStateDiagnostics:
        return self._last_diagnostics


class MakerStrategy:
    mode = ENTRY_MODE_MAKER

    def __init__(self, config: StrategyConfig, tick_size: float = 1.0):
        self.config = config
        self.tick_size = max(tick_size, 1e-9)

    def evaluate(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        market_state: MarketState = MarketState.NORMAL,
    ) -> EntryDecision:
        if market_state == MarketState.ABNORMAL:
            return EntryDecision(False, "market_abnormal")
        best = self._best_direction(snapshot, signal)
        if best is None:
            return EntryDecision(False, "maker_no_direction")
        diagnostics = best
        if not primary_checks_pass(diagnostics):
            return EntryDecision(False, "maker_primary")
        if diagnostics.entry_score < self.config.maker_score_threshold:
            return EntryDecision(False, f"maker_score:{diagnostics.entry_score}/{self.config.maker_score_threshold}")
        return EntryDecision(
            True,
            "",
            entry_mode=ENTRY_MODE_MAKER,
            side=diagnostics.direction,
            entry_score=diagnostics.entry_score,
            required_confirm=max(self.config.maker_confirm_ticks, 1),
        )

    def quote_mode_for_market(self, market_state: MarketState) -> str:
        if market_state == MarketState.ABNORMAL:
            return QUOTE_MODE_CLOSE_ONLY
        if market_state == MarketState.QUEUE:
            return QUOTE_MODE_QUEUE_DEFENSE
        return QUOTE_MODE_PASSIVE_FAIR_VALUE

    def preview_quote(
        self,
        *,
        symbol: str,
        exchange: int,
        tick_size: float,
        lot_size: int,
        qty: int,
        snapshot: BoardSnapshot,
        decision: EntryDecision,
        signal: SignalPacket | None = None,
        position: PositionState | None = None,
        max_inventory_qty: int = 0,
        market_state: MarketState = MarketState.NORMAL,
        working_age_ms: float = 0.0,
        setup_type: str = "",
        selection_reason: str = "",
    ) -> tuple[OrderIntent, MakerQuoteDiagnostics]:
        tick = max(tick_size, 1e-9)
        quote_mode = self.quote_mode_for_market(market_state)
        half_spread_ticks = self._calc_half_spread(signal) if signal is not None else self.config.min_half_spread_ticks
        queue_threshold = self._queue_threshold(market_state)
        top_queue_qty = snapshot.bid_size if decision.side > 0 else snapshot.ask_size

        if signal is not None and position is not None and max_inventory_qty > 0:
            fair = self._calc_fair_price(signal, snapshot.mid)
            reservation = self._calc_reservation_price(fair, position, max_inventory_qty)
            raw_price = self._select_quote_price(
                snapshot,
                signal,
                decision.side,
                reservation,
                tick,
                quote_mode=quote_mode,
                queue_threshold=queue_threshold,
            )
            reference_price = reservation
        elif decision.side > 0:
            fair = snapshot.mid
            reservation = snapshot.ask
            raw_price = snapshot.bid if self.config.maker_join_best else snapshot.bid - self.config.maker_retreat_ticks * tick
            reference_price = snapshot.ask
        else:
            fair = snapshot.mid
            reservation = snapshot.bid
            raw_price = snapshot.ask if self.config.maker_join_best else snapshot.ask + self.config.maker_retreat_ticks * tick
            reference_price = snapshot.bid

        price = align_price(raw_price, side=decision.side, tick_size=tick_size)
        edge_ticks = self._edge_ticks(side=decision.side, quote_price=price, reference_price=reference_price, tick=tick)
        intent = OrderIntent(
            symbol=symbol,
            exchange=exchange,
            side=decision.side,
            qty=align_qty(qty, lot_size),
            price=price,
            is_market=False,
            strategy=ENTRY_MODE_MAKER,
            reason="maker_passive_edge",
            score=decision.entry_score,
            reference_price=reference_price,
            setup_type=setup_type,
            selection_reason=selection_reason,
        )
        diagnostics = MakerQuoteDiagnostics(
            quote_mode=quote_mode,
            fair_price=fair,
            reservation_price=reservation,
            quote_price=price,
            edge_ticks=edge_ticks,
            half_spread_ticks=half_spread_ticks,
            queue_threshold=queue_threshold,
            top_queue_qty=top_queue_qty,
            working_age_ms=working_age_ms,
        )
        return intent, diagnostics

    def build_intent(
        self,
        *,
        symbol: str,
        exchange: int,
        tick_size: float,
        lot_size: int,
        qty: int,
        snapshot: BoardSnapshot,
        decision: EntryDecision,
        signal: SignalPacket | None = None,
        position: PositionState | None = None,
        max_inventory_qty: int = 0,
    ) -> OrderIntent:
        intent, _ = self.preview_quote(
            symbol=symbol,
            exchange=exchange,
            tick_size=tick_size,
            lot_size=lot_size,
            qty=qty,
            snapshot=snapshot,
            decision=decision,
            signal=signal,
            position=position,
            max_inventory_qty=max_inventory_qty,
        )
        return intent

    def compute_quote_price(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        side: int,
    ) -> float:
        """Recompute the ideal quote price (no inventory skew) for drift detection."""
        fair = self._calc_fair_price(signal, snapshot.mid)
        return self._select_quote_price(
            snapshot,
            signal,
            side,
            fair,
            self.tick_size,
            quote_mode=QUOTE_MODE_PASSIVE_FAIR_VALUE,
            queue_threshold=self._queue_threshold(MarketState.NORMAL),
        )

    def calc_cancel_reason(
        self,
        signal: SignalPacket,
        working_side: int,
        working_price: float,
        market_state: MarketState = MarketState.NORMAL,
        *,
        current_spread: float = 0.0,
        order_age_ns: int = 0,
        desired_price: float = 0.0,
        board_stale: bool = False,
        same_side_top_qty: int = 0,
    ) -> str:
        """Return a non-empty string if the working maker order should be cancelled.

        Urgent market-quality checks (abnormal_market, spread_expanded) are evaluated
        before the min-order-age guard so they can fire immediately on order placement.
        Signal-based cancels (alpha, ofi, book, microprice, fair) are suppressed until
        the order has been alive for at least min_order_age_ms milliseconds.
        """
        # Urgent: abnormal market state
        if market_state == MarketState.ABNORMAL:
            return "abnormal_market"
        # Urgent: stale board (inter-board gap exceeded threshold)
        if board_stale:
            return "stale_board"
        sign = working_side
        if sign == 0:
            return ""
        # Urgent: spread expanded beyond acceptable threshold
        if current_spread > 0 and self.config.spread_expanded_ticks > 0:
            if current_spread / self.tick_size >= self.config.spread_expanded_ticks:
                return "spread_expanded"
        if self.config.max_pending_ms > 0 and order_age_ns >= self.config.max_pending_ms * 1_000_000:
            return "pending_timeout"
        if self.config.maker_cancel_cancel_ratio_min > 0:
            same_side_cancel_ratio = signal.bid_cancel_ratio if sign > 0 else signal.ask_cancel_ratio
            if same_side_cancel_ratio >= self.config.maker_cancel_cancel_ratio_min:
                return "same_side_cancel"
        if self.config.queue_min_top_qty > 0 and 0 < same_side_top_qty < self.config.queue_min_top_qty:
            return "queue_thin"
        # Min order age guard — suppress signal-based cancels during order's min lifetime
        if self.config.min_order_age_ms > 0 and 0 < order_age_ns < self.config.min_order_age_ms * 1_000_000:
            return ""
        if self.config.maker_cancel_tape_1s_threshold > 0:
            if sign * signal.tape_ofi_1s <= -self.config.maker_cancel_tape_1s_threshold:
                return "tape_1s_flip"
        if self.config.maker_cancel_burst_threshold > 0:
            if sign * signal.trade_burst_score <= -self.config.maker_cancel_burst_threshold:
                return "burst_flip"
        if sign * signal.composite < -self.config.alpha_exit_threshold:
            return "alpha_flip"
        if abs(signal.composite) < self.config.alpha_entry_threshold * 0.6:
            return "alpha_decay"
        if sign * signal.tape_ofi_raw < -self.config.tape_imbalance_long:
            return "ofi_flip"
        if sign * signal.obi_raw < -self.config.book_imbalance_long:
            return "book_imbalance_flip"
        if sign > 0 and signal.microprice < signal.mid:
            return "microprice_flip"
        if sign < 0 and signal.microprice > signal.mid:
            return "microprice_flip"
        if working_price > 0 and self.config.max_fair_drift_ticks > 0:
            fair = self._calc_fair_price(signal, signal.mid)
            if abs(fair - working_price) / self.tick_size >= self.config.max_fair_drift_ticks:
                return "fair_drift"
        if desired_price > 0 and working_price > 0 and self.config.max_quote_drift_ticks > 0:
            if abs(desired_price - working_price) / self.tick_size >= self.config.max_quote_drift_ticks:
                return "quote_drift"
        return ""

    def _calc_fair_price(self, signal: SignalPacket, mid: float) -> float:
        shift = max(
            -self.config.max_fair_shift_ticks,
            min(self.config.max_fair_shift_ticks, self.config.fair_value_beta * signal.composite),
        )
        return mid + shift * self.tick_size

    def _calc_reservation_price(
        self, fair_price: float, position: PositionState, max_inventory_qty: int
    ) -> float:
        if max_inventory_qty <= 0 or self.config.inventory_skew_ticks <= 0:
            return fair_price
        signed_qty = position.side * position.qty
        inventory_ratio = signed_qty / max_inventory_qty
        multiplier = (
            self.config.inventory_high_multiplier
            if abs(inventory_ratio) >= self.config.inventory_high_threshold
            else 1.0
        )
        skew_ticks = self.config.inventory_skew_ticks * multiplier * inventory_ratio
        return fair_price - skew_ticks * self.tick_size

    def _calc_half_spread(self, signal: SignalPacket) -> float:
        if signal.vol_expansion or signal.mid_std_ticks > self.config.vol_high_ticks:
            return self.config.max_half_spread_ticks
        if signal.mid_std_ticks < self.config.vol_low_ticks:
            return self.config.min_half_spread_ticks
        return self.config.mid_half_spread_ticks

    def _queue_threshold(self, market_state: MarketState) -> int:
        threshold = max(int(self.config.queue_min_top_qty), 0)
        if market_state == MarketState.QUEUE and threshold <= 0:
            return 1
        return threshold

    def _edge_ticks(self, *, side: int, quote_price: float, reference_price: float, tick: float) -> float:
        if quote_price <= 0 or reference_price <= 0:
            return 0.0
        if side > 0:
            return (reference_price - quote_price) / tick
        if side < 0:
            return (quote_price - reference_price) / tick
        return 0.0

    def _select_quote_price(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        side: int,
        reservation: float,
        tick: float,
        *,
        quote_mode: str = QUOTE_MODE_PASSIVE_FAIR_VALUE,
        queue_threshold: int = 0,
    ) -> float:
        half_spread_ticks = self._calc_half_spread(signal)
        extra_retreat_ticks = max(0.0, half_spread_ticks - self.config.min_half_spread_ticks)
        # Queue-depth retreat: back away from a thin top-of-book to avoid adverse selection
        threshold = queue_threshold if queue_threshold > 0 else self.config.queue_min_top_qty
        if threshold > 0 and quote_mode != QUOTE_MODE_CLOSE_ONLY:
            top_qty = snapshot.bid_size if side > 0 else snapshot.ask_size
            if 0 < top_qty < threshold:
                extra_retreat_ticks += self.config.queue_retreat_ticks
        if side > 0:
            base = snapshot.bid if self.config.maker_join_best else snapshot.bid - self.config.maker_retreat_ticks * tick
            base -= extra_retreat_ticks * tick
            if reservation <= snapshot.bid - tick:
                return min(base, snapshot.bid - tick)
            improved = snapshot.bid + tick
            can_improve = (
                half_spread_ticks <= self.config.min_half_spread_ticks
                and signal.composite >= self.config.strong_signal_threshold
                and snapshot.spread >= 2 * tick
                and improved < snapshot.ask
                and reservation >= improved
            )
            if can_improve:
                return improved
            return min(base, snapshot.ask - tick)
        else:
            if reservation >= snapshot.ask + tick:
                base = snapshot.ask if self.config.maker_join_best else snapshot.ask + self.config.maker_retreat_ticks * tick
                base += extra_retreat_ticks * tick
                return max(base, snapshot.ask + tick)
            base = snapshot.ask if self.config.maker_join_best else snapshot.ask + self.config.maker_retreat_ticks * tick
            base += extra_retreat_ticks * tick
            improved = snapshot.ask - tick
            can_improve = (
                half_spread_ticks <= self.config.min_half_spread_ticks
                and signal.composite <= -self.config.strong_signal_threshold
                and snapshot.spread >= 2 * tick
                and improved > snapshot.bid
                and reservation <= improved
            )
            if can_improve:
                return improved
            return max(base, snapshot.bid + tick)

    def _best_direction(self, snapshot: BoardSnapshot, signal: SignalPacket) -> EntryLayerDiagnostics | None:
        long_diag = entry_layer_diagnostics(snapshot, signal, self.config, direction=1)
        if not self.config.allow_short:
            return long_diag
        short_diag = entry_layer_diagnostics(snapshot, signal, self.config, direction=-1)
        return long_diag if long_diag.entry_score >= short_diag.entry_score else short_diag


class TakerStrategy:
    mode = ENTRY_MODE_TAKER

    def __init__(self, config: StrategyConfig, tick_size: float = 1.0):
        self.config = config
        self.tick_size = max(tick_size, 1e-9)

    def evaluate(self, snapshot: BoardSnapshot, signal: SignalPacket, now_ns: int = 0) -> EntryDecision:
        # Adverse selection: reject stale signals
        if self.config.signal_expire_ms > 0 and now_ns > 0 and signal.ts_ns > 0:
            age_ns = now_ns - signal.ts_ns
            if age_ns > self.config.signal_expire_ms * 1_000_000:
                return EntryDecision(False, "signal_expired")

        best = self._best_direction(snapshot, signal)
        if best is None:
            return EntryDecision(False, "taker_no_direction")
        diagnostics = best
        if not primary_checks_pass(diagnostics):
            return EntryDecision(False, "taker_primary")
        if diagnostics.entry_score < self.config.taker_score_threshold:
            return EntryDecision(False, f"taker_score:{diagnostics.entry_score}/{self.config.taker_score_threshold}")
        # Execution quality gate: composite 0-10 score must meet minimum.
        # Checked before the expensive breakout calls to fail fast on poor conditions.
        if self.config.exec_quality_min_score > 0:
            q = self._compute_exec_quality(snapshot, signal, diagnostics.direction)
            if q < self.config.exec_quality_min_score:
                return EntryDecision(False, f"exec_quality:{q}/{self.config.exec_quality_min_score}")

        trigger, trigger_blocked_reason = self._classify_entry_trigger(snapshot, signal, diagnostics.direction)
        if not trigger:
            return EntryDecision(False, trigger_blocked_reason or "taker_breakout")

        # Determine required confirmation ticks
        confirm = max(self.config.taker_confirm_ticks, 1)
        if (self.config.aggressive_taker_entry_score > 0
                and diagnostics.entry_score >= self.config.aggressive_taker_entry_score):
            confirm = 1
        elif (self.config.use_adaptive_confirm
              and primary_checks_pass(diagnostics)
              and diagnostics.book and diagnostics.microprice_tilt):
            # Both direction signals (book + tilt) firing → require more ticks
            # to avoid chasing transient spikes; strong_signal_confirm >= taker_confirm_ticks.
            confirm = max(confirm, self.config.strong_signal_confirm)

        return EntryDecision(
            True,
            "",
            entry_mode=ENTRY_MODE_TAKER,
            side=diagnostics.direction,
            entry_score=diagnostics.entry_score,
            required_confirm=confirm,
        )

    def _compute_exec_quality(self, snapshot: BoardSnapshot, signal: SignalPacket, direction: int) -> int:
        """Composite execution quality score 0–10 (spread+imbalance+OFI+microprice)."""
        sign = 1 if direction > 0 else -1
        spread_ticks = snapshot.spread / self.tick_size if snapshot.spread > 0 else 0.0
        spread_score = (3 if spread_ticks <= 1.0
                        else 2 if spread_ticks <= 2.0
                        else 1 if spread_ticks <= 3.0
                        else 0)
        obi = sign * signal.obi_raw
        imbalance_score = (3 if obi >= self.config.book_imbalance_long * 1.5
                           else 2 if obi >= self.config.book_imbalance_long * 1.25
                           else 1 if obi >= self.config.book_imbalance_long
                           else 0)
        lob_ok = sign * signal.lob_ofi_raw >= self.config.of_imbalance_long
        tape_ok = sign * signal.tape_ofi_raw >= self.config.tape_imbalance_long
        if self.config.tape_ofi_1s_min > 0:
            # Both windows must agree; 15s alone is insufficient when 1s check is active
            tape_ok = tape_ok and (sign * signal.tape_ofi_1s >= self.config.tape_ofi_1s_min)
        ofi_score = 2 if (lob_ok and tape_ok) else 1 if (lob_ok or tape_ok) else 0
        tilt = sign * signal.microprice_tilt_raw
        microprice_score = (2 if tilt >= self.config.microprice_tilt_long * 1.5
                            else 1 if tilt >= self.config.microprice_tilt_long
                            else 0)
        return spread_score + imbalance_score + ofi_score + microprice_score

    def build_intent(
        self,
        *,
        symbol: str,
        exchange: int,
        lot_size: int,
        qty: int,
        snapshot: BoardSnapshot,
        decision: EntryDecision,
        setup_type: str = "",
        selection_reason: str = "",
    ) -> OrderIntent:
        reference = snapshot.ask if decision.side > 0 else snapshot.bid
        return OrderIntent(
            symbol=symbol,
            exchange=exchange,
            side=decision.side,
            qty=align_qty(qty, lot_size),
            price=0.0,
            is_market=True,
            strategy=ENTRY_MODE_TAKER,
            reason="taker_breakout",
            score=decision.entry_score,
            reference_price=reference,
            max_slip_ticks=self.config.max_slip_ticks,
            setup_type=setup_type,
            selection_reason=selection_reason,
        )

    def exec_quality_score(self, snapshot: BoardSnapshot, signal: SignalPacket, direction: int) -> int:
        return self._compute_exec_quality(snapshot, signal, direction)

    def classify_entry_trigger(self, snapshot: BoardSnapshot, signal: SignalPacket, direction: int) -> str:
        if direction == 0:
            return ""
        trigger, _ = self._classify_entry_trigger(snapshot, signal, direction)
        return trigger

    def _best_direction(self, snapshot: BoardSnapshot, signal: SignalPacket) -> EntryLayerDiagnostics | None:
        long_diag = entry_layer_diagnostics(snapshot, signal, self.config, direction=1)
        if not self.config.allow_short:
            return long_diag
        short_diag = entry_layer_diagnostics(snapshot, signal, self.config, direction=-1)
        return long_diag if long_diag.entry_score >= short_diag.entry_score else short_diag

    def _breakout_ready(self, snapshot: BoardSnapshot, signal: SignalPacket, direction: int) -> bool:
        trigger, _ = self._classify_entry_trigger(snapshot, signal, direction)
        return trigger in {"depth_thin", "wall_break", "cancel_imbalance"}

    def _classify_entry_trigger(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        direction: int,
    ) -> tuple[str, str]:
        if direction == 0:
            return "", ""
        sign = 1 if direction > 0 else -1
        cancel_ratio = self._opposite_cancel_ratio(signal, sign)
        if (
            self.config.cancel_imbalance_extreme_ratio > 0
            and cancel_ratio >= self.config.cancel_imbalance_extreme_ratio
        ):
            return "", "taker_cancel_extreme"
        if self._wall_break_ready(signal, sign) and self._trigger_confirmations_ready(signal, sign):
            return "wall_break", ""
        if self._cancel_imbalance_ready(signal, sign) and self._trigger_confirmations_ready(signal, sign):
            return "cancel_imbalance", ""
        if self._depth_thin_ready(snapshot, direction) and self._trigger_confirmations_ready(signal, sign):
            return "depth_thin", ""
        if self._breakout_price_ready(signal, direction):
            return "price_breakout", ""
        if self._vol_expansion_ready(snapshot, signal, direction):
            return "vol_expansion", ""
        return "", ""

    def _trigger_confirmations_ready(self, signal: SignalPacket, sign: int) -> bool:
        strong_tape = sign * signal.tape_ofi_raw >= self.config.tape_imbalance_long * max(
            self.config.strong_signal_multiplier, 1.0
        )
        tilt = sign * signal.microprice_tilt_raw >= self.config.microprice_tilt_long
        integrated = sign * signal.integrated_ofi > 0.0
        return strong_tape and tilt and integrated and self._burst_ready(signal, sign)

    def _burst_ready(self, signal: SignalPacket, sign: int) -> bool:
        return sign * signal.trade_burst_score > max(self.config.taker_burst_min, 0.0)

    def _opposite_cancel_ratio(self, signal: SignalPacket, sign: int) -> float:
        return signal.ask_cancel_ratio if sign > 0 else signal.bid_cancel_ratio

    def _depth_thin_ready(self, snapshot: BoardSnapshot, direction: int) -> bool:
        if not self.config.use_depth_thin_taker:
            return False
        same = _same_side_depth(snapshot, direction)
        opposite = _opposite_depth(snapshot, direction)
        if same <= 0:
            return False
        return opposite <= max(self.config.opposite_depth_ratio_max, 0.0) * same

    def _wall_break_ready(self, signal: SignalPacket, sign: int) -> bool:
        if not self.config.use_wall_break_taker:
            return False
        return (
            (
                sign > 0
                and signal.wall_ask_consumed
                and signal.wall_ask_consumed_ratio >= self.config.wall_consumed_ratio_min
            )
            or (
                sign < 0
                and signal.wall_bid_consumed
                and signal.wall_bid_consumed_ratio >= self.config.wall_consumed_ratio_min
            )
        )

    def _cancel_imbalance_ready(self, signal: SignalPacket, sign: int) -> bool:
        if not self.config.use_cancel_imbalance_taker:
            return False
        return self._opposite_cancel_ratio(signal, sign) >= self.config.cancel_imbalance_ratio_min

    def _breakout_price_ready(self, signal: SignalPacket, direction: int) -> bool:
        """T-06: price-breakout alternative entry path."""
        if not self.config.use_price_breakout_taker:
            return False
        sign = 1 if direction > 0 else -1
        strong_tape = sign * signal.tape_ofi_raw >= self.config.tape_imbalance_long * max(
            self.config.strong_signal_multiplier, 1.0
        )
        tape = sign * signal.tape_ofi_raw > self.config.tape_imbalance_long
        tilt = sign * signal.microprice_tilt_raw >= self.config.microprice_tilt_long
        integrated = sign * signal.integrated_ofi > 0.0
        burst_or_strong_tape = self._burst_ready(signal, sign) or strong_tape
        if sign > 0:
            return (
                signal.breakout_long
                and signal.obi_raw > 0
                and tape
                and tilt
                and integrated
                and burst_or_strong_tape
            )
        return (
            signal.breakout_short
            and signal.obi_raw < 0
            and tape
            and tilt
            and integrated
            and burst_or_strong_tape
        )

    def _vol_expansion_ready(self, snapshot: BoardSnapshot, signal: SignalPacket, direction: int) -> bool:
        """T-09: volatility-expansion alternative entry path.

        Fires when the market transitions from low to high volatility with directional
        confirmation from OBI, tape, and microprice tilt. A strict spread filter is
        applied because spread can widen simultaneously with vol expansion — entering
        into a wide spread erodes edge.
        """
        if not self.config.use_vol_expansion_taker or not signal.vol_expansion:
            return False
        # Spread filter: reject when spread exceeds the T-09-specific cap
        if self.config.vol_expansion_spread_max_ticks > 0 and snapshot.spread > 0:
            spread_ticks = snapshot.spread / self.tick_size
            if spread_ticks > self.config.vol_expansion_spread_max_ticks:
                return False
        sign = 1 if direction > 0 else -1
        obi_ok = sign * signal.obi_raw > self.config.book_imbalance_long
        tape_ok = sign * signal.tape_ofi_raw > self.config.tape_imbalance_long
        tilt_ok = sign * signal.microprice_tilt_raw >= self.config.microprice_tilt_long
        return obi_ok and tape_ok and tilt_ok


class ConfirmationTracker:
    def __init__(self):
        self.key: tuple[str, int] | None = None
        self.count = 0

    def observe(self, decision: EntryDecision) -> tuple[bool, int]:
        if not decision.allow:
            self.reset()
            return False, 0
        key = (decision.entry_mode, decision.side)
        if key != self.key:
            self.key = key
            self.count = 0
        self.count += 1
        return self.count >= decision.required_confirm, self.count

    def reset(self) -> None:
        self.key = None
        self.count = 0


def align_price(price: float, *, side: int, tick_size: float) -> float:
    tick = max(tick_size, 1e-9)
    if price <= 0:
        return 0.0
    steps = price / tick
    snapped = math.floor(steps + 1e-9) if side > 0 else math.ceil(steps - 1e-9)
    return round(max(snapped * tick, tick), 10)


def align_qty(qty: int, lot_size: int) -> int:
    lot = max(lot_size, 1)
    return (max(qty, 0) // lot) * lot


def _same_side_depth(snapshot: BoardSnapshot, direction: int) -> int:
    levels = snapshot.bids if direction > 0 else snapshot.asks
    return sum(max(level.size, 0) for level in levels[:2])


def _opposite_depth(snapshot: BoardSnapshot, direction: int) -> int:
    levels = snapshot.asks if direction > 0 else snapshot.bids
    return sum(max(level.size, 0) for level in levels[:2])


def _is_special_quote_sign(sign: str) -> bool:
    return str(sign or "").strip() in _SPECIAL_QUOTE_SIGNS
