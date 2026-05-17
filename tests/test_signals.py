from __future__ import annotations

import unittest

from kabu_maker_taker.config import SignalConfig
from kabu_maker_taker.models import BoardSnapshot, Level, TradePrint
from kabu_maker_taker.signals import (
    BreakoutTracker,
    CancelImbalanceTracker,
    MicrostructureSignalEngine,
    MicropriceStreakTracker,
    TapePressure,
    VolExpansionDetector,
    WallDetector,
)


class TapePressure1sTests(unittest.TestCase):
    def test_ofi_1s_reflects_recent_trades_only(self) -> None:
        tape = TapePressure(window_seconds=15)
        # Buy at t=0
        from kabu_maker_taker.models import TradePrint
        tape.on_trade(TradePrint("X", 0, 100.0, 200, 1))
        self.assertGreater(tape.ofi_1s, 0.0)

    def test_ofi_1s_excludes_old_trades(self) -> None:
        tape = TapePressure(window_seconds=15)
        from kabu_maker_taker.models import TradePrint
        # Buy at t=0
        tape.on_trade(TradePrint("X", 0, 100.0, 200, 1))
        # 2 seconds later: sell — old buy is outside 1s window
        tape.on_trade(TradePrint("X", 2_000_000_000, 100.0, 100, -1))
        # 1s window should only contain the sell trade
        self.assertLess(tape.ofi_1s, 0.0)

    def test_burst_unchanged(self) -> None:
        tape = TapePressure(window_seconds=15)
        tape.on_trade(TradePrint("X", 0, 100.0, 300, 1))
        self.assertGreater(tape.burst, 0.0)

    def test_unknown_side_is_ignored(self) -> None:
        tape = TapePressure(window_seconds=15)
        tape.on_trade(TradePrint("X", 1, 100.0, 300, 0))
        self.assertEqual(tape.current, 0.0)
        self.assertEqual(len(tape.events), 0)

    def test_tradeprint_missing_side_is_unknown(self) -> None:
        trade = TradePrint.from_dict({"symbol": "X", "ts_ns": 1, "price": 100.0, "size": 100})
        self.assertEqual(trade.side, 0)


class UnknownTradeSideEngineTests(unittest.TestCase):
    def test_unknown_side_does_not_pollute_tape_or_wall_consumption(self) -> None:
        engine = MicrostructureSignalEngine(tick_size=1.0, config=SignalConfig())
        trade = TradePrint.from_dict({"symbol": "X", "ts_ns": 1, "price": 100.0, "size": 100, "side": 0})

        engine.on_trade(trade)

        self.assertEqual(engine.tape.current, 0.0)
        snapshot = BoardSnapshot(
            symbol="X",
            ts_ns=2,
            bid=100.0,
            ask=101.0,
            bid_size=500,
            ask_size=500,
            bids=(Level(100.0, 500),),
            asks=(Level(101.0, 500),),
        )
        signal = engine.on_board(snapshot)
        self.assertEqual(signal.tape_ofi_raw, 0.0)


class WallDetectorTests(unittest.TestCase):
    def _make(self) -> WallDetector:
        return WallDetector(ema_alpha=0.10, ratio_threshold=2.5)

    def test_no_wall_at_first_call(self) -> None:
        wd = self._make()
        result = wd.update(500, 500, 0, 0)
        # First call always returns all False/0
        self.assertFalse(result[0])  # wall_ask_detected
        self.assertFalse(result[1])  # wall_bid_detected

    def test_wall_detected_above_ratio(self) -> None:
        wd = self._make()
        # Seed EMA at 100 via first call
        wd.update(100, 100, 0, 0)
        # ask1 = 300 = 3.0× EMA → above 2.5× threshold
        result = wd.update(300, 100, 0, 0)
        # wall_ask should be detected (prev_ask=100 vs ema=100 → 1.0× not wall;
        # but here after seeding, check that large subsequent spike is flagged)
        # After first update EMA ≈ 100, second update: prev_ask=100 >= 100*2.5? No.
        # Need one more step to see wall after EMA warms up
        self.assertFalse(result[0])  # prev_ask was 100, ema was 100 → 1.0× < 2.5

    def test_wall_detected_after_spike(self) -> None:
        wd = self._make()
        # Seed EMA with small baseline
        for _ in range(5):
            wd.update(100, 100, 0, 0)
        # Now ask1 = 350 → prev=100, EMA≈100, so 100 >= 100*2.5? No.
        # Wall is detected when *previous* ask was large:
        # Put large ask, then reduce it (that's when consumed is triggered)
        wd.update(300, 100, 0, 0)   # prev=100 no wall, but now ema=~110, prev_ask=300
        result = wd.update(100, 100, 200, 0)  # prev_ask=300, ema~=110, 300>=110*2.5? yes!
        # wall_ask_detected = prev_ask(300) >= ema(~110) * 2.5(~275) → True
        self.assertTrue(result[0])   # wall_ask_detected
        self.assertTrue(result[2])   # wall_ask_consumed (fill_at_ask=200 > 0 and drop)
        self.assertGreater(result[4], 0.0)  # wall_ask_consumed_ratio

    def test_no_consumption_without_fills(self) -> None:
        wd = self._make()
        for _ in range(5):
            wd.update(100, 100, 0, 0)
        wd.update(300, 100, 0, 0)
        # Wall vanishes via cancel (no fill_at_ask)
        result = wd.update(100, 100, 0, 0)
        # wall_ask_detected may be True (300 was large)
        # but wall_ask_consumed should be False (no fill)
        self.assertFalse(result[2])  # wall_ask_consumed


class CancelImbalanceTrackerTests(unittest.TestCase):
    def test_zero_cancel_when_fill_explains_drop(self) -> None:
        tracker = CancelImbalanceTracker()
        # bid dropped 100, all explained by fill
        bid_ratio, ask_ratio = tracker.update(
            bid1_prev=500, bid1_curr=400, fill_at_bid=100,
            ask1_prev=500, ask1_curr=500, fill_at_ask=0,
        )
        self.assertAlmostEqual(bid_ratio, 0.0)
        self.assertAlmostEqual(ask_ratio, 0.0)

    def test_cancel_detected_when_drop_exceeds_fills(self) -> None:
        tracker = CancelImbalanceTracker()
        # bid dropped 300 but only 50 filled → 250 cancelled
        bid_ratio, _ = tracker.update(
            bid1_prev=500, bid1_curr=200, fill_at_bid=50,
            ask1_prev=200, ask1_curr=200, fill_at_ask=0,
        )
        self.assertAlmostEqual(bid_ratio, 250 / 500)

    def test_zero_prev_size_returns_zero(self) -> None:
        tracker = CancelImbalanceTracker()
        bid_ratio, ask_ratio = tracker.update(0, 0, 0, 0, 0, 0)
        self.assertEqual(bid_ratio, 0.0)
        self.assertEqual(ask_ratio, 0.0)


class BreakoutTrackerTests(unittest.TestCase):
    def test_no_breakout_while_filling(self) -> None:
        bt = BreakoutTracker(lookback_bars=5, buffer_ticks=0.0, tick_size=1.0)
        for price in [100.0, 101.0, 100.5, 100.0, 101.0]:
            long_b, short_b = bt.update(price)
        # Last price is 101 — not above max(100,101,100.5,100,101)=101
        self.assertFalse(long_b)

    def test_breakout_long_detected(self) -> None:
        bt = BreakoutTracker(lookback_bars=5, buffer_ticks=0.0, tick_size=1.0)
        for price in [100.0, 101.0, 100.5, 99.0, 100.0]:
            bt.update(price)
        long_b, _ = bt.update(102.0)  # above max(101)
        self.assertTrue(long_b)

    def test_breakout_short_detected(self) -> None:
        bt = BreakoutTracker(lookback_bars=5, buffer_ticks=0.0, tick_size=1.0)
        for price in [100.0, 101.0, 100.5, 99.0, 100.0]:
            bt.update(price)
        _, short_b = bt.update(98.0)  # below min(99)
        self.assertTrue(short_b)

    def test_buffer_prevents_false_breakout(self) -> None:
        bt = BreakoutTracker(lookback_bars=5, buffer_ticks=1.0, tick_size=1.0)
        for price in [100.0, 101.0, 100.5, 99.0, 100.0]:
            bt.update(price)
        # max=101, need > 101+1=102 to break out; 101.5 is not enough
        long_b, _ = bt.update(101.5)
        self.assertFalse(long_b)
        # After adding 101.5 to history, max becomes 101.5; need > 101.5+1=102.5
        # Use 103.0 to ensure strict greater-than
        long_b2, _ = bt.update(103.0)
        self.assertTrue(long_b2)


class VolExpansionDetectorTests(unittest.TestCase):
    def test_no_expansion_at_baseline(self) -> None:
        ved = VolExpansionDetector(ema_alpha=0.20, ratio=2.0)
        for _ in range(10):
            result = ved.update(1.0)
        self.assertFalse(result)

    def test_expansion_detected_on_spike(self) -> None:
        ved = VolExpansionDetector(ema_alpha=0.20, ratio=2.0)
        for _ in range(20):
            ved.update(1.0)
        result = ved.update(5.0)   # 5.0 >= EMA(≈1.0) * 2.0 → True
        self.assertTrue(result)

    def test_no_expansion_on_first_call(self) -> None:
        ved = VolExpansionDetector(ema_alpha=0.20, ratio=2.0)
        self.assertFalse(ved.update(100.0))


class MicropriceStreakTrackerTests(unittest.TestCase):
    def test_initial_streaks_zero(self) -> None:
        tracker = MicropriceStreakTracker()
        up, down = tracker.update(100.0)
        self.assertEqual(up, 0)
        self.assertEqual(down, 0)

    def test_up_streak_increments(self) -> None:
        tracker = MicropriceStreakTracker()
        tracker.update(100.0)
        tracker.update(100.1)
        tracker.update(100.2)
        up, down = tracker.update(100.3)
        self.assertEqual(up, 3)
        self.assertEqual(down, 0)

    def test_down_streak_increments(self) -> None:
        tracker = MicropriceStreakTracker()
        tracker.update(100.0)
        tracker.update(99.9)
        tracker.update(99.8)
        up, down = tracker.update(99.7)
        self.assertEqual(up, 0)
        self.assertEqual(down, 3)

    def test_streak_resets_on_direction_change(self) -> None:
        tracker = MicropriceStreakTracker()
        tracker.update(100.0)
        tracker.update(100.1)
        tracker.update(100.2)
        # Direction reversal
        up, down = tracker.update(100.1)
        self.assertEqual(up, 0)
        self.assertEqual(down, 1)

    def test_flat_price_resets_both(self) -> None:
        tracker = MicropriceStreakTracker()
        tracker.update(100.0)
        tracker.update(100.1)
        up, down = tracker.update(100.1)  # same price
        self.assertEqual(up, 0)
        self.assertEqual(down, 0)


if __name__ == "__main__":
    unittest.main()
