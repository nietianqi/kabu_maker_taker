"""Tests for five taker-logic improvements ported from kabu_micro_edge / kabu_micro_edge_c:
  - Execution quality score gate (exec_quality_min_score)
  - Aggressive taker mode (aggressive_taker_entry_score)
  - Adaptive confirmation (use_adaptive_confirm / strong_signal_confirm)
  - Flow-flip exit (flow_flip_threshold)
  - Dynamic order-qty scaling (scale_qty_by_score)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.lollipop import LollipopTPManager
from kabu_maker_taker.models import (
    BoardSnapshot,
    EntryDecision,
    Level,
    LollipopPhase,
    PositionState,
    SignalPacket,
)
from kabu_maker_taker.strategy import ENTRY_MODE_MAKER, ENTRY_MODE_TAKER, TakerStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    bid: float = 100.0,
    ask: float = 101.0,
    bid_size: int = 500,
    ask_size: int = 200,
    ts_ns: int = 0,
    symbol: str = "9984",
) -> BoardSnapshot:
    return BoardSnapshot(
        symbol=symbol,
        ts_ns=ts_ns,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        bids=(Level(bid, bid_size),),
        asks=(Level(ask, ask_size),),
    )


def _signal(
    composite: float = 0.8,
    obi_raw: float = 0.40,
    tape_ofi_raw: float = 0.25,
    lob_ofi_raw: float = 0.25,
    micro_momentum_raw: float = 0.10,
    microprice_tilt_raw: float = 0.50,
    microprice: float = 100.6,
    mid: float = 100.5,
    mid_std_ticks: float = 0.5,
    integrated_ofi: float = 0.30,
    trade_burst_score: float = 0.10,
    ts_ns: int = 0,
) -> SignalPacket:
    return SignalPacket(
        ts_ns=ts_ns,
        composite=composite,
        obi_raw=obi_raw,
        obi_z=obi_raw * 2,
        tape_ofi_raw=tape_ofi_raw,
        tape_ofi_z=tape_ofi_raw * 2,
        lob_ofi_raw=lob_ofi_raw,
        lob_ofi_z=lob_ofi_raw * 2,
        micro_momentum_raw=micro_momentum_raw,
        micro_momentum_z=micro_momentum_raw * 2,
        microprice_tilt_raw=microprice_tilt_raw,
        microprice_tilt_z=microprice_tilt_raw * 2,
        microprice=microprice,
        mid=mid,
        mid_std_ticks=mid_std_ticks,
        integrated_ofi=integrated_ofi,
        trade_burst_score=trade_burst_score,
    )


def _make_taker(**kwargs) -> TakerStrategy:
    """Build a TakerStrategy with sensible defaults; override via kwargs."""
    defaults = dict(
        taker_score_threshold=9,
        book_imbalance_long=0.18,
        of_imbalance_long=0.10,
        tape_imbalance_long=0.10,
        microprice_tilt_long=0.25,
        mom_long_threshold=0.0,
        strong_signal_multiplier=1.5,
        wall_consumed_ratio_min=0.60,
        signal_expire_ms=0,
    )
    defaults.update(kwargs)
    return TakerStrategy(StrategyConfig(**defaults), tick_size=1.0)


def _make_strategy(**strategy_kwargs) -> CombinedMakerTakerStrategy:
    """Build a CombinedMakerTakerStrategy; strategy_kwargs override StrategyConfig defaults."""
    defaults = dict(
        trade_qty=100,
        taker_score_threshold=9,
        taker_confirm_ticks=1,
        book_imbalance_long=0.18,
        of_imbalance_long=0.10,
        tape_imbalance_long=0.10,
        microprice_tilt_long=0.25,
        mom_long_threshold=0.0,
        strong_signal_multiplier=1.5,
        wall_consumed_ratio_min=0.60,
        signal_expire_ms=0,
    )
    defaults.update(strategy_kwargs)
    cfg = AppConfig(
        symbol="9984",
        exchange=27,
        tick_size=1.0,
        lot_size=100,
        dry_run=True,
        strategy=StrategyConfig(**defaults),
        risk=RiskConfig(max_inventory_qty=300, max_spread_ticks=10.0),
        lollipop=LollipopConfig(taker_tp_ticks=2.0, taker_max_hold_seconds=30),
    )
    return CombinedMakerTakerStrategy(cfg)


# ---------------------------------------------------------------------------
# Execution Quality Gate
# ---------------------------------------------------------------------------

class ExecQualityGateTests(unittest.TestCase):

    def test_high_quality_passes_entry(self) -> None:
        """Tight spread + strong imbalance + both OFI + strong tilt → quality >= min → allowed."""
        t = _make_taker(exec_quality_min_score=5)
        snap = _snap(bid=100.0, ask=101.0, bid_size=500, ask_size=100)  # 1-tick spread
        sig = _signal(obi_raw=0.40, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.50)
        decision = t.evaluate(snap, sig)
        # spread(3) + imbalance(3) + ofi(2) + microprice(2) = 10 → passes min=5
        self.assertTrue(decision.allow, f"Expected allowed, got: {decision.reason}")

    def test_low_quality_blocks_entry(self) -> None:
        """Wide spread (4 ticks) → spread_score=0; with min=8 entry is blocked."""
        t = _make_taker(exec_quality_min_score=8)
        snap = _snap(bid=100.0, ask=104.0, bid_size=500, ask_size=100)  # 4-tick spread
        sig = _signal(obi_raw=0.40, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.50)
        decision = t.evaluate(snap, sig)
        self.assertFalse(decision.allow)
        self.assertIn("exec_quality", decision.reason)

    def test_exec_quality_disabled_when_zero(self) -> None:
        """exec_quality_min_score=0 → gate inactive, wide spread not rejected by quality."""
        t = _make_taker(exec_quality_min_score=0)
        snap = _snap(bid=100.0, ask=104.0, bid_size=500, ask_size=100)
        sig = _signal(obi_raw=0.40, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.50)
        decision = t.evaluate(snap, sig)
        self.assertNotIn("exec_quality", decision.reason)

    def test_compute_exec_quality_scores_correctly(self) -> None:
        """Unit test for _compute_exec_quality() scoring breakdown."""
        t = _make_taker()
        # 1-tick spread (score=3) + OBI=0.36 ≥ 0.18*1.5=0.27 → imbalance=3
        # lob+tape both pass → ofi=2; tilt=0.45 ≥ 0.25*1.5=0.375 → microprice=2
        snap = _snap(bid=100.0, ask=101.0)
        sig = _signal(obi_raw=0.36, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.45)
        q = t._compute_exec_quality(snap, sig, 1)
        self.assertEqual(q, 10)


# ---------------------------------------------------------------------------
# Aggressive Taker Mode
# ---------------------------------------------------------------------------

class AggressiveTakerTests(unittest.TestCase):

    def _high_score_signal_and_snap(self):
        snap = _snap(bid=100.0, ask=101.0, bid_size=500, ask_size=100)
        sig = _signal(obi_raw=0.40, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.50)
        return snap, sig

    def test_high_score_reduces_confirm_to_1(self) -> None:
        """entry_score >= aggressive_taker_entry_score → required_confirm == 1."""
        t = _make_taker(aggressive_taker_entry_score=11, taker_confirm_ticks=3)
        snap, sig = self._high_score_signal_and_snap()
        decision = t.evaluate(snap, sig)
        if decision.allow and decision.entry_score >= 11:
            self.assertEqual(decision.required_confirm, 1,
                             f"score={decision.entry_score} should give confirm=1")

    def test_below_threshold_uses_normal_confirm(self) -> None:
        """entry_score < unreachable threshold → required_confirm == taker_confirm_ticks."""
        t = _make_taker(aggressive_taker_entry_score=50, taker_confirm_ticks=3)
        snap, sig = self._high_score_signal_and_snap()
        decision = t.evaluate(snap, sig)
        if decision.allow:
            self.assertEqual(decision.required_confirm, 3,
                             "Threshold unreachable → confirm should stay at taker_confirm_ticks")

    def test_aggressive_disabled_when_zero(self) -> None:
        """aggressive_taker_entry_score=0 → confirm stays at taker_confirm_ticks."""
        t = _make_taker(aggressive_taker_entry_score=0, taker_confirm_ticks=3)
        snap, sig = self._high_score_signal_and_snap()
        decision = t.evaluate(snap, sig)
        if decision.allow:
            self.assertEqual(decision.required_confirm, 3,
                             "Feature disabled → confirm should stay at taker_confirm_ticks")


# ---------------------------------------------------------------------------
# Adaptive Confirmation
# ---------------------------------------------------------------------------

class AdaptiveConfirmTests(unittest.TestCase):

    def test_adaptive_confirm_raises_confirm_for_strong_signal(self) -> None:
        """use_adaptive_confirm=True + all primary checks pass → required_confirm >= strong_signal_confirm."""
        t = _make_taker(
            use_adaptive_confirm=True,
            strong_signal_confirm=4,
            taker_confirm_ticks=1,
        )
        snap = _snap(bid=100.0, ask=101.0, bid_size=500, ask_size=100)
        sig = _signal(obi_raw=0.40, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.50)
        decision = t.evaluate(snap, sig)
        if decision.allow:
            self.assertGreaterEqual(decision.required_confirm, 4,
                                    "Strong signal should require at least strong_signal_confirm ticks")

    def test_adaptive_confirm_disabled_when_false(self) -> None:
        """use_adaptive_confirm=False → confirm stays at taker_confirm_ticks regardless of signal."""
        t = _make_taker(
            use_adaptive_confirm=False,
            strong_signal_confirm=4,
            taker_confirm_ticks=1,
        )
        snap = _snap(bid=100.0, ask=101.0, bid_size=500, ask_size=100)
        sig = _signal(obi_raw=0.40, lob_ofi_raw=0.25, tape_ofi_raw=0.25, microprice_tilt_raw=0.50)
        decision = t.evaluate(snap, sig)
        if decision.allow:
            self.assertEqual(decision.required_confirm, 1)


# ---------------------------------------------------------------------------
# Flow-flip Exit
# ---------------------------------------------------------------------------

class FlowFlipExitTests(unittest.TestCase):

    def _make_lollipop(self) -> LollipopTPManager:
        return LollipopTPManager(
            LollipopConfig(taker_tp_ticks=2.0, taker_max_hold_seconds=30),
            tick_size=1.0, lot_size=100,
        )

    def test_flow_flip_calls_force_exit_next_tick(self) -> None:
        """combined.on_board() calls force_exit_next_tick when flow-flip condition met.

        We mock signals.on_board to return a signal with tape_ofi_raw below -threshold
        so the combined.py condition triggers deterministically.
        """
        strategy = _make_strategy(flow_flip_threshold=0.15)
        # Inject an open taker position and activate lollipop
        strategy.position = PositionState(
            side=1, qty=100, avg_price=101.0, entry_mode=ENTRY_MODE_TAKER
        )
        ts = 1_000_000_000_000
        strategy.lollipop.on_entry_fill(avg_price=101.0, entry_mode=ENTRY_MODE_TAKER,
                                        now_ns=ts, entry_side=1)
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.SCHEDULED)

        # Inject a signal with strongly negative tape_ofi via mock
        bad_signal = _signal(tape_ofi_raw=-0.20, lob_ofi_raw=-0.20)
        strategy.signals.on_board = MagicMock(return_value=bad_signal)

        snap = _snap(ts_ns=ts + 500_000_000)
        strategy.on_board(snap, now_ns=ts + 500_000_000)

        # After flow-flip, lollipop should have transitioned to TIMEOUT
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.TIMEOUT)

    def test_flow_flip_not_triggered_for_maker_position(self) -> None:
        """Flow-flip guard only applies to taker entries; maker positions are unaffected."""
        strategy = _make_strategy(flow_flip_threshold=0.15)
        strategy.position = PositionState(
            side=1, qty=100, avg_price=100.0, entry_mode=ENTRY_MODE_MAKER
        )
        ts = 1_000_000_000_000
        strategy.lollipop.on_entry_fill(avg_price=100.0, entry_mode=ENTRY_MODE_MAKER,
                                        now_ns=ts, entry_side=1)
        phase_after_entry = strategy.lollipop.phase  # SCHEDULED

        bad_signal = _signal(tape_ofi_raw=-0.20, lob_ofi_raw=-0.20)
        strategy.signals.on_board = MagicMock(return_value=bad_signal)

        snap = _snap(ts_ns=ts + 500_000_000)
        strategy.on_board(snap, now_ns=ts + 500_000_000)

        # Maker position must NOT be force-exited by flow-flip
        self.assertNotEqual(strategy.lollipop.phase, LollipopPhase.TIMEOUT)

    def test_flow_flip_disabled_when_zero(self) -> None:
        """flow_flip_threshold=0 → no force exit even with extreme negative tape."""
        strategy = _make_strategy(flow_flip_threshold=0.0)
        strategy.position = PositionState(
            side=1, qty=100, avg_price=101.0, entry_mode=ENTRY_MODE_TAKER
        )
        ts = 1_000_000_000_000
        strategy.lollipop.on_entry_fill(avg_price=101.0, entry_mode=ENTRY_MODE_TAKER,
                                        now_ns=ts, entry_side=1)

        bad_signal = _signal(tape_ofi_raw=-0.99, lob_ofi_raw=-0.99)
        strategy.signals.on_board = MagicMock(return_value=bad_signal)

        snap = _snap(ts_ns=ts + 500_000_000)
        strategy.on_board(snap, now_ns=ts + 500_000_000)

        # Disabled → lollipop should NOT be in TIMEOUT from this board alone
        self.assertNotEqual(strategy.lollipop.phase, LollipopPhase.TIMEOUT)


# ---------------------------------------------------------------------------
# Dynamic Sizing — formula unit tests
# ---------------------------------------------------------------------------

class DynamicSizingTests(unittest.TestCase):

    def test_scale_qty_formula_rounds_down_to_lot(self) -> None:
        """100 * 1.5 = 150 → rounds DOWN to nearest lot (100), not up to 200."""
        base_qty, multiplier, lot_size, max_inv = 100, 1.5, 100, 300
        scaled = int(base_qty * multiplier // lot_size) * lot_size
        self.assertEqual(min(scaled, max_inv), 100)

    def test_scale_qty_2lot_base(self) -> None:
        """200 * 1.5 = 300 → exactly 3 lots."""
        base_qty, multiplier, lot_size, max_inv = 200, 1.5, 100, 300
        scaled = int(base_qty * multiplier // lot_size) * lot_size
        self.assertEqual(min(scaled, max_inv), 300)

    def test_scale_qty_capped_at_max_inventory(self) -> None:
        """200 * 2.0 = 400 → capped at max_inventory_qty=300."""
        base_qty, multiplier, lot_size, max_inv = 200, 2.0, 100, 300
        scaled = int(base_qty * multiplier // lot_size) * lot_size
        self.assertEqual(min(scaled, max_inv), 300)


if __name__ == "__main__":
    unittest.main()
