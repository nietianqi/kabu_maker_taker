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
from .models import BoardSnapshot, EntryDecision, MarketState, OrderIntent, PositionState, SignalPacket

ENTRY_MODE_MAKER = "maker"
ENTRY_MODE_TAKER = "taker"
ORDER_ROLE_ENTRY = "entry"
ORDER_ROLE_EXIT = "exit"


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

    def __init__(self, config: MarketStateConfig, tick_size: float):
        self.config = config
        self.tick_size = max(tick_size, 1e-9)
        self._prev_mid: float = 0.0
        # Hard-bound the deque: at most 2× the max expected events in the window.
        _max = max(int(config.abnormal_event_rate_hz * config.event_rate_window_seconds * 2), 64)
        self._event_times: deque[int] = deque(maxlen=_max)
        self._state: MarketState = MarketState.NORMAL

    def update(self, snapshot: BoardSnapshot, now_ns: int) -> MarketState:
        if not self.config.enabled:
            return MarketState.NORMAL

        window_ns = self.config.event_rate_window_seconds * 1_000_000_000
        self._event_times.append(now_ns)
        while self._event_times and now_ns - self._event_times[0] > window_ns:
            self._event_times.popleft()
        event_rate_hz = len(self._event_times) / max(self.config.event_rate_window_seconds, 1)

        tick = self.tick_size
        spread_ticks = snapshot.spread / tick if snapshot.spread > 0 else 0.0
        price_jump_ticks = 0.0
        if self._prev_mid > 0 and snapshot.mid > 0:
            price_jump_ticks = abs(snapshot.mid - self._prev_mid) / tick
        if snapshot.mid > 0:
            self._prev_mid = snapshot.mid

        if (not snapshot.valid
                or spread_ticks >= self.config.abnormal_spread_ticks
                or event_rate_hz >= self.config.abnormal_event_rate_hz
                or price_jump_ticks >= self.config.abnormal_price_jump_ticks):
            self._state = MarketState.ABNORMAL
        elif snapshot.spread <= tick:
            self._state = MarketState.QUEUE
        else:
            self._state = MarketState.NORMAL
        return self._state

    @property
    def state(self) -> MarketState:
        return self._state


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
        tick = max(tick_size, 1e-9)
        if signal is not None and position is not None and max_inventory_qty > 0:
            fair = self._calc_fair_price(signal, snapshot.mid)
            reservation = self._calc_reservation_price(fair, position, max_inventory_qty)
            raw_price = self._select_quote_price(snapshot, signal, decision.side, reservation, tick)
            # Reference = fair-value anchor (the mid-point the strategy priced off)
            reference_price = reservation
        elif decision.side > 0:
            raw_price = snapshot.bid if self.config.maker_join_best else snapshot.bid - self.config.maker_retreat_ticks * tick
            # Reference = the contra-side best (cost if adversely selected)
            reference_price = snapshot.ask
        else:
            raw_price = snapshot.ask if self.config.maker_join_best else snapshot.ask + self.config.maker_retreat_ticks * tick
            reference_price = snapshot.bid
        price = align_price(raw_price, side=decision.side, tick_size=tick_size)
        return OrderIntent(
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
        sign = working_side
        if sign == 0:
            return ""
        # Urgent: spread expanded beyond acceptable threshold
        if current_spread > 0 and self.config.spread_expanded_ticks > 0:
            if current_spread / self.tick_size >= self.config.spread_expanded_ticks:
                return "spread_expanded"
        # Min order age guard — suppress signal-based cancels during order's min lifetime
        if self.config.min_order_age_ms > 0 and 0 < order_age_ns < self.config.min_order_age_ms * 1_000_000:
            return ""
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

    def _select_quote_price(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        side: int,
        reservation: float,
        tick: float,
    ) -> float:
        half_spread_ticks = self._calc_half_spread(signal)
        extra_retreat_ticks = max(0.0, half_spread_ticks - self.config.min_half_spread_ticks)
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

    def __init__(self, config: StrategyConfig):
        self.config = config

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
        if not self._breakout_ready(snapshot, signal, diagnostics.direction):
            if not self._breakout_price_ready(signal, diagnostics.direction):
                return EntryDecision(False, "taker_breakout")
        return EntryDecision(
            True,
            "",
            entry_mode=ENTRY_MODE_TAKER,
            side=diagnostics.direction,
            entry_score=diagnostics.entry_score,
            required_confirm=max(self.config.taker_confirm_ticks, 1),
        )

    def build_intent(
        self,
        *,
        symbol: str,
        exchange: int,
        lot_size: int,
        qty: int,
        snapshot: BoardSnapshot,
        decision: EntryDecision,
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
        )

    def _best_direction(self, snapshot: BoardSnapshot, signal: SignalPacket) -> EntryLayerDiagnostics | None:
        long_diag = entry_layer_diagnostics(snapshot, signal, self.config, direction=1)
        if not self.config.allow_short:
            return long_diag
        short_diag = entry_layer_diagnostics(snapshot, signal, self.config, direction=-1)
        return long_diag if long_diag.entry_score >= short_diag.entry_score else short_diag

    def _breakout_ready(self, snapshot: BoardSnapshot, signal: SignalPacket, direction: int) -> bool:
        sign = 1 if direction > 0 else -1
        same = _same_side_depth(snapshot, direction)
        opposite = _opposite_depth(snapshot, direction)
        if same <= 0:
            return False

        # Original thin-opposite condition (T-01)
        opposite_thin = opposite <= 0.5 * same

        # T-04: wall on opposite side was consumed by trades
        wall_consumed = (
            (sign > 0 and signal.wall_ask_consumed
             and signal.wall_ask_consumed_ratio >= self.config.wall_consumed_ratio_min)
            or (sign < 0 and signal.wall_bid_consumed
                and signal.wall_bid_consumed_ratio >= self.config.wall_consumed_ratio_min)
        )

        # T-05: opposite side liquidity rapidly cancelled
        cancel_side_clearing = (
            (sign > 0 and signal.ask_cancel_ratio >= 0.40)
            or (sign < 0 and signal.bid_cancel_ratio >= 0.40)
        )

        depth_clear = opposite_thin or wall_consumed or cancel_side_clearing

        strong_tape = sign * signal.tape_ofi_raw >= self.config.tape_imbalance_long * max(
            self.config.strong_signal_multiplier, 1.0
        )
        tilt = sign * signal.microprice_tilt_raw >= self.config.microprice_tilt_long
        integrated = sign * signal.integrated_ofi > 0.0
        burst = sign * signal.trade_burst_score > 0.0
        return depth_clear and strong_tape and tilt and integrated and burst

    def _breakout_price_ready(self, signal: SignalPacket, direction: int) -> bool:
        """T-06: price-breakout alternative entry path."""
        sign = 1 if direction > 0 else -1
        strong_tape = sign * signal.tape_ofi_raw >= self.config.tape_imbalance_long * max(
            self.config.strong_signal_multiplier, 1.0
        )
        tape = sign * signal.tape_ofi_raw > self.config.tape_imbalance_long
        tilt = sign * signal.microprice_tilt_raw >= self.config.microprice_tilt_long
        integrated = sign * signal.integrated_ofi > 0.0
        burst_or_strong_tape = sign * signal.trade_burst_score > 0.0 or strong_tape
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
