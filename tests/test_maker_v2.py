from __future__ import annotations

import unittest

from kabu_maker_taker.config import AppConfig, MarketStateConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import (
    BoardSnapshot,
    EntryDecision,
    Level,
    MarketState,
    PositionState,
    SignalPacket,
    StrategyResult,
)
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


class BoardSnapshotParsingTests(unittest.TestCase):
    def test_from_dict_parses_kabu_quote_diagnostics(self) -> None:
        snap = BoardSnapshot.from_dict(
            {
                "Symbol": "9984",
                "Exchange": 27,
                "ExchangeTimeNs": 1_000_000_000,
                "BidPrice": 100.0,
                "AskPrice": 101.0,
                "BidQty": 500,
                "AskQty": 300,
                "BidSign": "0101",
                "AskSign": "0102",
                "BidTimeNs": 900_000_000,
                "AskTimeNs": 950_000_000,
                "CurrentPriceTimeNs": 980_000_000,
                "CurrentPriceSize": 100,
            }
        )

        self.assertEqual(snap.bid_sign, "0101")
        self.assertEqual(snap.ask_sign, "0102")
        self.assertEqual(snap.bid_ts_ns, 900_000_000)
        self.assertEqual(snap.ask_ts_ns, 950_000_000)
        self.assertEqual(snap.current_ts_ns, 980_000_000)
        self.assertEqual(snap.last_size, 100)


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

    def test_pending_timeout_bypasses_min_order_age(self) -> None:
        m = self._maker(max_pending_ms=2500, min_order_age_ms=5000)
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.20, microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(
            sig,
            working_side=1,
            working_price=100.0,
            order_age_ns=2_500_000_000,
        )
        self.assertEqual(reason, "pending_timeout")


class MakerQuoteDiagnosticsTests(unittest.TestCase):
    def _preview(self, *, bid_size: int, market_state: MarketState):
        cfg = StrategyConfig(
            queue_min_top_qty=300,
            queue_retreat_ticks=1.0,
            maker_join_best=True,
            min_half_spread_ticks=1.0,
            mid_half_spread_ticks=1.0,
        )
        maker = MakerStrategy(cfg, tick_size=1.0)
        decision = EntryDecision(True, "", entry_mode="maker", side=1, entry_score=8, required_confirm=1)
        return maker.preview_quote(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            qty=100,
            snapshot=_snapshot(bid=100.0, ask=101.0, bid_size=bid_size),
            decision=decision,
            signal=_signal(mid=100.5),
            position=PositionState(),
            max_inventory_qty=300,
            market_state=market_state,
        )

    def test_queue_mode_retreats_when_top_queue_thin(self) -> None:
        intent, diagnostics = self._preview(bid_size=100, market_state=MarketState.QUEUE)
        self.assertLess(intent.price, 100.0)
        self.assertEqual(diagnostics.quote_mode, "QUEUE_DEFENSE")
        self.assertEqual(diagnostics.queue_threshold, 300)
        self.assertEqual(diagnostics.top_queue_qty, 100)

    def test_queue_mode_joins_best_when_top_queue_enough(self) -> None:
        intent, diagnostics = self._preview(bid_size=500, market_state=MarketState.QUEUE)
        self.assertEqual(intent.price, 100.0)
        self.assertEqual(diagnostics.quote_mode, "QUEUE_DEFENSE")
        self.assertGreater(diagnostics.edge_ticks, 0.0)

    def test_strategy_result_to_dict_includes_maker_fields(self) -> None:
        _, diagnostics = self._preview(bid_size=500, market_state=MarketState.NORMAL)
        result = StrategyResult(
            None,
            EntryDecision(False, "confirming"),
            None,
            maker_quote_mode=diagnostics.quote_mode,
            maker_fair_price=diagnostics.fair_price,
            maker_reservation_price=diagnostics.reservation_price,
            maker_edge_ticks=diagnostics.edge_ticks,
            maker_half_spread_ticks=diagnostics.half_spread_ticks,
            maker_queue_threshold=diagnostics.queue_threshold,
            maker_top_queue_qty=diagnostics.top_queue_qty,
        )
        payload = result.to_dict()
        self.assertEqual(payload["maker_quote_mode"], "PASSIVE_FAIR_VALUE")
        self.assertIn("maker_edge_ticks", payload)


class MarketStateDetectorTests(unittest.TestCase):
    def _make(self, stale_quote_ms: int = 2000, **kw) -> MarketStateDetector:
        cfg = MarketStateConfig(enabled=True, **kw)
        return MarketStateDetector(cfg, tick_size=1.0, stale_quote_ms=stale_quote_ms)

    def test_normal_spread_returns_normal(self) -> None:
        det = self._make(abnormal_spread_ticks=6.0, abnormal_price_jump_ticks=4.0)
        snap = _snapshot(bid=100.0, ask=102.0)
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.NORMAL)  # enum comparison
        self.assertEqual(det.last_diagnostics.reason, "normal_flow")

    def test_wide_spread_returns_abnormal(self) -> None:
        det = self._make(abnormal_spread_ticks=6.0)
        snap = _snapshot(bid=100.0, ask=107.0)  # spread=7 ticks
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)
        self.assertEqual(det.last_diagnostics.reason, "spread_blowout")

    def test_large_price_jump_returns_abnormal(self) -> None:
        det = self._make(abnormal_price_jump_ticks=4.0)
        snap1 = _snapshot(bid=100.0, ask=101.0)
        det.update(snap1, now_ns=1_000_000_000)
        snap2 = _snapshot(bid=105.0, ask=106.0)  # jump = 5.5 ticks
        state = det.update(snap2, now_ns=2_000_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)
        self.assertEqual(det.last_diagnostics.reason, "price_jump")

    def test_one_tick_spread_returns_queue(self) -> None:
        det = self._make()
        snap = _snapshot(bid=100.0, ask=101.0)  # spread = 1 tick
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.QUEUE)
        self.assertEqual(det.last_diagnostics.reason, "one_tick_queue")

    def test_invalid_board_reason(self) -> None:
        det = self._make()
        snap = _snapshot(bid=0.0, ask=101.0)
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)
        self.assertEqual(det.last_diagnostics.reason, "invalid_quote")

    def test_stale_quote_reason(self) -> None:
        det = self._make(stale_quote_ms=100)
        snap = _snapshot(ts_ns=1_000_000_000, bid=100.0, ask=102.0)
        state = det.update(snap, now_ns=1_200_000_001)
        self.assertEqual(state, MarketState.ABNORMAL)
        self.assertEqual(det.last_diagnostics.reason, "stale_quote")
        self.assertGreater(det.last_diagnostics.stale_ms, 200.0)

    def test_special_quote_reason(self) -> None:
        det = self._make()
        snap = _snapshot(bid=100.0, ask=102.0, ask_sign="0102")
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)
        self.assertEqual(det.last_diagnostics.reason, "special_quote_sign")

    def test_event_burst_reason(self) -> None:
        det = self._make(
            abnormal_event_rate_hz=3.0,
            event_rate_window_seconds=1,
            event_burst_min_events=3,
            abnormal_price_jump_ticks=99.0,
        )
        for idx in range(3):
            ts = 1_000_000_000 + idx * 100_000_000
            state = det.update(_snapshot(ts_ns=ts, bid=100.0, ask=102.0), now_ns=ts)
        self.assertEqual(state, MarketState.ABNORMAL)
        self.assertEqual(det.last_diagnostics.reason, "event_burst")

    def test_trade_lag_ms_uses_quote_minus_last_trade_time(self) -> None:
        det = self._make()
        snap = _snapshot(
            bid=100.0,
            ask=102.0,
            bid_ts_ns=1_500_000_000,
            ask_ts_ns=1_400_000_000,
            current_ts_ns=1_000_000_000,
        )
        det.update(snap, now_ns=1_600_000_000)
        self.assertAlmostEqual(det.last_diagnostics.trade_lag_ms, 500.0)

    def test_disabled_always_returns_normal(self) -> None:
        cfg = MarketStateConfig(enabled=False)
        det = MarketStateDetector(cfg, tick_size=1.0)
        snap = _snapshot(bid=100.0, ask=110.0)  # would be ABNORMAL if enabled
        state = det.update(snap, now_ns=1_000_000_000)
        self.assertEqual(state, MarketState.NORMAL)  # enum comparison
        self.assertEqual(det.last_diagnostics.reason, "disabled")

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
        self.assertEqual(result.market_state_reason, "disabled")
        self.assertEqual(result.maker_quote_mode, "PASSIVE_FAIR_VALUE")

    def test_maker_min_edge_blocks_low_edge_entry(self) -> None:
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy

        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            lot_size=100,
            strategy=StrategyConfig(
                maker_confirm_ticks=1,
                taker_confirm_ticks=1,
                maker_min_edge_ticks=2.0,
            ),
            risk=RiskConfig(max_inventory_qty=300, max_spread_ticks=5.0),
            market_state=MarketStateConfig(enabled=False),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(mid=100.5, composite=0.20)
        strategy._choose_decision = lambda snapshot, signal, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            True,
            "",
            entry_mode="maker",
            side=1,
            entry_score=8,
            required_confirm=1,
        )

        result = strategy.on_board(_snapshot(bid=100.0, ask=101.0), now_ns=1_000_000_000)

        self.assertIsNone(result.intent)
        self.assertEqual(result.blocked_reason, "maker_edge_too_low")
        self.assertLess(result.maker_edge_ticks, 2.0)
        self.assertEqual(result.maker_quote_mode, "PASSIVE_FAIR_VALUE")

    def test_working_maker_pending_timeout_emits_cancel_signal(self) -> None:
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy

        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            lot_size=100,
            strategy=StrategyConfig(
                maker_confirm_ticks=1,
                taker_confirm_ticks=1,
                min_order_age_ms=5000,
                max_pending_ms=2500,
            ),
            risk=RiskConfig(max_inventory_qty=300, max_spread_ticks=5.0),
            market_state=MarketStateConfig(enabled=False),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(mid=100.5, composite=0.45)
        strategy._choose_decision = lambda snapshot, signal, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            True,
            "",
            entry_mode="maker",
            side=1,
            entry_score=8,
            required_confirm=1,
        )
        base = 1_000_000_000
        result1 = strategy.on_board(_snapshot(ts_ns=base), now_ns=base)
        self.assertIsNotNone(result1.intent)

        result2 = strategy.on_board(_snapshot(ts_ns=base + 2_500_000_000), now_ns=base + 2_500_000_000)

        self.assertEqual(result2.blocked_reason, "working_entry")
        self.assertEqual(result2.entry_cancel_signal, "pending_timeout")
        self.assertAlmostEqual(result2.maker_working_age_ms, 2500.0)


if __name__ == "__main__":
    unittest.main()
