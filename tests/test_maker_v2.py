from __future__ import annotations

import unittest

from kabu_maker_taker.config import AppConfig, MarketStateConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, EntryDecision, Level, MarketState, PositionState, SignalPacket
from kabu_maker_taker.strategy import MakerStrategy, MarketStateDetector


def _signal(**overrides) -> SignalPacket:
    defaults = {
        "ts_ns": 1_000_000_000,
        "obi_raw": 0.35,
        "lob_ofi_raw": 0.20,
        "tape_ofi_raw": 0.20,
        "micro_momentum_raw": 0.10,
        "microprice_tilt_raw": 0.30,
        "microprice": 100.3,
        "mid": 100.0,
        "obi_z": 0.5,
        "lob_ofi_z": 0.4,
        "tape_ofi_z": 0.3,
        "micro_momentum_z": 0.2,
        "microprice_tilt_z": 0.1,
        "composite": 0.45,
        "integrated_ofi": 0.20,
        "trade_burst_score": 0.10,
        "mid_std_ticks": 1.0,
    }
    defaults.update(overrides)
    return SignalPacket(**defaults)


def _snapshot(**overrides) -> BoardSnapshot:
    defaults = dict(
        symbol="9984", ts_ns=1_000_000_000,
        bid=100.0, ask=101.0, bid_size=500, ask_size=200,
        bids=(Level(100.0, 500), Level(99.0, 300)),
        asks=(Level(101.0, 200), Level(102.0, 250)),
    )
    defaults.update(overrides)
    return BoardSnapshot(**defaults)


class FairPriceTests(unittest.TestCase):
    def _maker(self, **kw) -> MakerStrategy:
        return MakerStrategy(StrategyConfig(**kw), tick_size=1.0)

    def test_fair_price_shifts_up_on_positive_composite(self) -> None:
        m = self._maker(fair_value_beta=0.75, max_fair_shift_ticks=3.0)
        sig = _signal(composite=1.0)
        fair = m._calc_fair_price(sig, 100.0)
        self.assertAlmostEqual(fair, 100.75)

    def test_fair_price_shifts_down_on_negative_composite(self) -> None:
        m = self._maker(fair_value_beta=0.75, max_fair_shift_ticks=3.0)
        sig = _signal(composite=-1.0)
        fair = m._calc_fair_price(sig, 100.0)
        self.assertAlmostEqual(fair, 99.25)

    def test_fair_price_clamped_at_max(self) -> None:
        m = self._maker(fair_value_beta=0.75, max_fair_shift_ticks=2.0)
        sig = _signal(composite=10.0)
        fair = m._calc_fair_price(sig, 100.0)
        self.assertAlmostEqual(fair, 102.0)

    def test_fair_price_neutral_at_zero_composite(self) -> None:
        m = self._maker(fair_value_beta=0.75, max_fair_shift_ticks=3.0)
        sig = _signal(composite=0.0)
        fair = m._calc_fair_price(sig, 100.0)
        self.assertAlmostEqual(fair, 100.0)


class ReservationPriceTests(unittest.TestCase):
    def _maker(self, **kw) -> MakerStrategy:
        cfg = StrategyConfig(inventory_skew_ticks=1.0, **kw)
        return MakerStrategy(cfg, tick_size=1.0)

    def test_neutral_inventory_returns_fair(self) -> None:
        m = self._maker()
        pos = PositionState(side=0, qty=0)
        rp = m._calc_reservation_price(100.0, pos, max_inventory_qty=300)
        self.assertAlmostEqual(rp, 100.0)

    def test_50pct_long_inventory_skews_down(self) -> None:
        m = self._maker()
        pos = PositionState(side=1, qty=150)
        rp = m._calc_reservation_price(100.0, pos, max_inventory_qty=300)
        # inventory_ratio=0.5, multiplier=1.0, skew=0.5 ticks below fair
        self.assertAlmostEqual(rp, 99.5)

    def test_70pct_long_inventory_uses_1_5x_multiplier(self) -> None:
        m = self._maker()
        pos = PositionState(side=1, qty=210)
        rp = m._calc_reservation_price(100.0, pos, max_inventory_qty=300)
        # inventory_ratio=0.7, multiplier=1.5, skew=1.0*1.5*0.7=1.05 ticks
        self.assertAlmostEqual(rp, 98.95)

    def test_100pct_long_inventory_max_skew(self) -> None:
        m = self._maker()
        pos = PositionState(side=1, qty=300)
        rp = m._calc_reservation_price(100.0, pos, max_inventory_qty=300)
        # inventory_ratio=1.0, multiplier=1.5, skew=1.5 ticks
        self.assertAlmostEqual(rp, 98.5)


class DynamicSpreadTests(unittest.TestCase):
    def _maker(self, **kw) -> MakerStrategy:
        cfg = StrategyConfig(
            vol_high_ticks=2.0, vol_low_ticks=0.5,
            min_half_spread_ticks=1.0, mid_half_spread_ticks=1.5, max_half_spread_ticks=3.0,
            **kw,
        )
        return MakerStrategy(cfg, tick_size=1.0)

    def test_low_vol_returns_min_spread(self) -> None:
        m = self._maker()
        sig = _signal(mid_std_ticks=0.3, vol_expansion=False)
        self.assertAlmostEqual(m._calc_half_spread(sig), 1.0)

    def test_normal_vol_returns_mid_spread(self) -> None:
        m = self._maker()
        sig = _signal(mid_std_ticks=1.0, vol_expansion=False)
        self.assertAlmostEqual(m._calc_half_spread(sig), 1.5)

    def test_high_vol_returns_max_spread(self) -> None:
        m = self._maker()
        sig = _signal(mid_std_ticks=2.5, vol_expansion=False)
        self.assertAlmostEqual(m._calc_half_spread(sig), 3.0)

    def test_vol_expansion_flag_returns_max_spread(self) -> None:
        m = self._maker()
        sig = _signal(mid_std_ticks=0.3, vol_expansion=True)
        self.assertAlmostEqual(m._calc_half_spread(sig), 3.0)

    def test_high_vol_quote_uses_extra_retreat(self) -> None:
        m = self._maker(strong_signal_threshold=0.75, maker_join_best=True)
        snap = _snapshot(bid=100.0, ask=105.0)
        sig = _signal(composite=0.90, mid_std_ticks=2.5, vol_expansion=False)
        price = m._select_quote_price(snap, sig, side=1, reservation=104.0, tick=1.0)
        self.assertAlmostEqual(price, 98.0)

    def test_vol_expansion_quote_does_not_tick_improve(self) -> None:
        m = self._maker(strong_signal_threshold=0.75, maker_join_best=True)
        snap = _snapshot(bid=100.0, ask=105.0)
        sig = _signal(composite=0.90, mid_std_ticks=0.3, vol_expansion=True)
        price = m._select_quote_price(snap, sig, side=1, reservation=104.0, tick=1.0)
        self.assertAlmostEqual(price, 98.0)


class TickImprovementTests(unittest.TestCase):
    def test_strong_composite_with_wide_spread_improves(self) -> None:
        cfg = StrategyConfig(strong_signal_threshold=0.75, maker_join_best=True)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=102.0)  # spread=2 ticks
        sig = _signal(composite=0.90)           # composite >= 0.75
        price = m._select_quote_price(snap, sig, side=1, reservation=101.0, tick=1.0)
        self.assertAlmostEqual(price, 101.0)    # bid + 1 tick

    def test_reservation_retreat_blocks_tick_improvement(self) -> None:
        cfg = StrategyConfig(strong_signal_threshold=0.75, maker_join_best=True)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=102.0)
        sig = _signal(composite=0.90)
        price = m._select_quote_price(snap, sig, side=1, reservation=98.0, tick=1.0)
        self.assertAlmostEqual(price, 99.0)

    def test_weak_composite_joins_best(self) -> None:
        cfg = StrategyConfig(strong_signal_threshold=0.75, maker_join_best=True)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=102.0)
        sig = _signal(composite=0.40)
        price = m._select_quote_price(snap, sig, side=1, reservation=100.0, tick=1.0)
        self.assertAlmostEqual(price, 100.0)    # join best bid

    def test_step_back_when_reservation_too_low(self) -> None:
        cfg = StrategyConfig(strong_signal_threshold=0.75, maker_join_best=True)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=101.0)
        sig = _signal(composite=0.30)
        # reservation = 98.0 <= bid(100) - 1 tick → retreat
        price = m._select_quote_price(snap, sig, side=1, reservation=98.0, tick=1.0)
        self.assertAlmostEqual(price, 99.0)

    def test_narrow_spread_no_improvement_even_strong(self) -> None:
        cfg = StrategyConfig(strong_signal_threshold=0.75, maker_join_best=True)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=101.0)  # spread=1 tick (not >= 2)
        sig = _signal(composite=0.90)
        price = m._select_quote_price(snap, sig, side=1, reservation=100.0, tick=1.0)
        self.assertAlmostEqual(price, 100.0)    # join best (no improvement)

    def test_maker_intent_reference_price_uses_reservation(self) -> None:
        cfg = StrategyConfig(fair_value_beta=0.75, max_fair_shift_ticks=3.0, inventory_skew_ticks=1.0)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=101.0)
        sig = _signal(composite=1.0)
        decision = EntryDecision(True, "", entry_mode="maker", side=1, entry_score=8, required_confirm=1)
        intent = m.build_intent(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            qty=100,
            snapshot=snap,
            decision=decision,
            signal=sig,
            position=PositionState(side=0, qty=0),
            max_inventory_qty=300,
        )
        self.assertAlmostEqual(intent.reference_price, 101.25)


class CancelReasonTests(unittest.TestCase):
    def _maker(self, **kw) -> MakerStrategy:
        cfg = StrategyConfig(
            alpha_exit_threshold=0.15,
            alpha_entry_threshold=0.40,
            tape_imbalance_long=0.10,
            book_imbalance_long=0.18,
            max_fair_drift_ticks=1.5,
            fair_value_beta=0.75,
            max_fair_shift_ticks=3.0,
            **kw,
        )
        return MakerStrategy(cfg, tick_size=1.0)

    def test_no_cancel_on_neutral_signal(self) -> None:
        m = self._maker()
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.30, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0)
        self.assertEqual(reason, "")

    def test_alpha_flip_on_strong_reversal(self) -> None:
        m = self._maker()
        sig = _signal(composite=-0.50, tape_ofi_raw=0.15, obi_raw=0.20, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0)
        self.assertEqual(reason, "alpha_flip")

    def test_alpha_decay_on_weak_composite(self) -> None:
        m = self._maker()
        # composite=0.10 < 0.40 * 0.6 = 0.24
        sig = _signal(composite=0.10, tape_ofi_raw=0.15, obi_raw=0.20, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0)
        self.assertEqual(reason, "alpha_decay")

    def test_ofi_flip_on_tape_reversal(self) -> None:
        m = self._maker()
        # tape_ofi strongly negative for long position
        sig = _signal(composite=0.45, tape_ofi_raw=-0.25, obi_raw=0.20, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0)
        self.assertEqual(reason, "ofi_flip")

    def test_microprice_flip_when_microprice_below_mid(self) -> None:
        m = self._maker()
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.20, microprice=99.8, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0)
        self.assertEqual(reason, "microprice_flip")

    def test_fair_drift_triggers_cancel(self) -> None:
        m = self._maker()
        # composite=0.45 → fair_price = 100 + 0.75*0.45 ≈ 100.34
        # working_price=98.5 → drift ≈ 1.84 ticks ≥ 1.5
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.20, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=98.5)
        self.assertEqual(reason, "fair_drift")

    def test_abnormal_market_state_cancels(self) -> None:
        m = self._maker()
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.20, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0, market_state=MarketState.ABNORMAL)
        self.assertEqual(reason, "abnormal_market")


class MarketStateDetectorTests(unittest.TestCase):
    def _make(self, **kw) -> MarketStateDetector:
        cfg = MarketStateConfig(enabled=True, **kw)
        return MarketStateDetector(cfg, tick_size=1.0)

    def test_normal_spread_returns_normal(self) -> None:
        det = self._make(abnormal_spread_ticks=6.0, abnormal_price_jump_ticks=4.0)
        snap = _snapshot(bid=100.0, ask=102.0)
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.NORMAL)  # enum comparison

    def test_wide_spread_returns_abnormal(self) -> None:
        det = self._make(abnormal_spread_ticks=6.0)
        snap = _snapshot(bid=100.0, ask=107.0)  # spread=7 ticks
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)

    def test_large_price_jump_returns_abnormal(self) -> None:
        det = self._make(abnormal_price_jump_ticks=4.0)
        snap1 = _snapshot(bid=100.0, ask=101.0)
        det.update(snap1, now_ns=1_000_000_000)
        snap2 = _snapshot(bid=105.0, ask=106.0)  # jump = 5.5 ticks
        state = det.update(snap2, now_ns=2_000_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)

    def test_one_tick_spread_returns_queue(self) -> None:
        det = self._make()
        snap = _snapshot(bid=100.0, ask=101.0)  # spread = 1 tick
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.QUEUE)

    def test_disabled_always_returns_normal(self) -> None:
        cfg = MarketStateConfig(enabled=False)
        det = MarketStateDetector(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=110.0)  # would be ABNORMAL if enabled
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.NORMAL)  # enum comparison

    def test_abnormal_blocks_maker_entry(self) -> None:
        cfg = StrategyConfig(maker_score_threshold=6)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot()
        sig = _signal()
        decision = m.evaluate(snap, sig, market_state=MarketState.ABNORMAL)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "market_abnormal")

    def test_normal_state_allows_maker_entry(self) -> None:
        cfg = StrategyConfig(maker_score_threshold=6)
        m = MakerStrategy(cfg, tick_size=1.0)
        snap = _snapshot()
        sig = _signal()
        decision = m.evaluate(snap, sig, market_state=MarketState.NORMAL)
        self.assertTrue(decision.allow)


class CancelReasonInCombinedTests(unittest.TestCase):
    def _build_strategy(self) -> object:
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            strategy=StrategyConfig(
                maker_confirm_ticks=1,
                taker_confirm_ticks=1,
                alpha_exit_threshold=0.15,
                alpha_entry_threshold=0.40,
            ),
            risk=RiskConfig(max_spread_ticks=5.0),
        )
        return CombinedMakerTakerStrategy(config)

    def test_cancel_reason_emitted_when_entry_active(self) -> None:
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.models import TradePrint
        strategy = self._build_strategy()
        base = 1_770_000_000_000_000_000
        # Seed a strong long signal so maker fires
        strategy.on_trade(TradePrint("9984", base, 100.0, 200, 1))
        snap1 = BoardSnapshot(
            symbol="9984", ts_ns=base + 100_000_000,
            bid=100.0, ask=101.0, bid_size=500, ask_size=200,
            bids=(Level(100.0, 500), Level(99.0, 300)),
            asks=(Level(101.0, 200), Level(102.0, 250)),
        )
        result1 = strategy.on_board(snap1, now_ns=snap1.ts_ns)
        # If a maker intent was fired and entry is now active, next tick exposes cancel_reason
        if result1.intent is not None:
            snap2 = BoardSnapshot(
                symbol="9984", ts_ns=base + 200_000_000,
                bid=100.0, ask=101.0, bid_size=500, ask_size=200,
                bids=(Level(100.0, 500), Level(99.0, 300)),
                asks=(Level(101.0, 200), Level(102.0, 250)),
            )
            result2 = strategy.on_board(snap2, now_ns=snap2.ts_ns)
            self.assertEqual(result2.blocked_reason, "working_entry")
            self.assertIsInstance(result2.entry_cancel_signal, str)

    def test_market_state_in_result(self) -> None:
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.models import TradePrint
        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            strategy=StrategyConfig(maker_confirm_ticks=1, taker_confirm_ticks=1),
            risk=RiskConfig(max_spread_ticks=5.0),
            market_state=MarketStateConfig(enabled=False),
        )
        strategy = CombinedMakerTakerStrategy(config)
        base = 1_770_000_000_000_000_000
        strategy.on_trade(TradePrint("9984", base, 100.0, 100, 1))
        snap = BoardSnapshot(
            symbol="9984", ts_ns=base + 100_000_000,
            bid=100.0, ask=101.0, bid_size=500, ask_size=200,
            bids=(Level(100.0, 500),), asks=(Level(101.0, 200),),
        )
        result = strategy.on_board(snap, now_ns=snap.ts_ns)
        self.assertEqual(result.market_state, MarketState.NORMAL)
        self.assertEqual(result.to_dict()["market_state"], MarketState.NORMAL.value)


class MarketStateOutputPathTests(unittest.TestCase):
    def _build_strategy(self, risk: RiskConfig):
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy

        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            lot_size=100,
            strategy=StrategyConfig(maker_confirm_ticks=1, taker_confirm_ticks=1),
            risk=risk,
            market_state=MarketStateConfig(enabled=False),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal()
        strategy._choose_decision = lambda snapshot, signal, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            True,
            "",
            entry_mode="maker",
            side=1,
            entry_score=8,
            required_confirm=1,
        )
        return strategy

    def test_market_state_is_enum_on_qty_zero_path(self) -> None:
        strategy = self._build_strategy(RiskConfig(max_inventory_qty=0, max_spread_ticks=5.0))
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        self.assertEqual(result.blocked_reason, "qty_zero")
        self.assertEqual(result.market_state, MarketState.NORMAL)
        self.assertEqual(result.to_dict()["market_state"], MarketState.NORMAL.value)

    def test_market_state_is_enum_on_risk_block_path(self) -> None:
        strategy = self._build_strategy(RiskConfig(max_inventory_qty=300, max_spread_ticks=0.5))
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        self.assertEqual(result.blocked_reason, "spread_too_wide")
        self.assertEqual(result.market_state, MarketState.NORMAL)
        self.assertEqual(result.to_dict()["market_state"], MarketState.NORMAL.value)

    def test_market_state_is_enum_on_intent_path(self) -> None:
        strategy = self._build_strategy(RiskConfig(max_inventory_qty=300, max_spread_ticks=5.0))
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        self.assertIsNotNone(result.intent)
        self.assertEqual(result.market_state, MarketState.NORMAL)
        self.assertEqual(result.to_dict()["market_state"], MarketState.NORMAL.value)


if __name__ == "__main__":
    unittest.main()
