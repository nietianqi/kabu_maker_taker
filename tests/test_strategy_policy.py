from __future__ import annotations

import unittest

from kabu_maker_taker.config import StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, Level, SignalPacket
from kabu_maker_taker.strategy import MakerStrategy, TakerStrategy


def strong_long_signal(**overrides):
    values = {
        "ts_ns": 1,
        "obi_raw": 0.45,
        "lob_ofi_raw": 0.30,
        "tape_ofi_raw": 0.30,
        "micro_momentum_raw": 0.20,
        "microprice_tilt_raw": 0.60,
        "microprice": 100.8,
        "mid": 100.5,
        "obi_z": 0.0,
        "lob_ofi_z": 0.0,
        "tape_ofi_z": 0.0,
        "micro_momentum_z": 0.0,
        "microprice_tilt_z": 0.0,
        "composite": 0.0,
        "integrated_ofi": 0.30,
        "trade_burst_score": 0.25,
    }
    values.update(overrides)
    return SignalPacket(**values)


class StrategyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=200,
            bids=(Level(100.0, 1000), Level(99.0, 500)),
            asks=(Level(101.0, 200), Level(102.0, 250)),
        )

    def test_taker_requires_breakout_and_high_score(self) -> None:
        config = StrategyConfig(taker_score_threshold=9)
        decision = TakerStrategy(config).evaluate(self.snapshot, strong_long_signal())
        self.assertTrue(decision.allow)
        self.assertEqual(decision.entry_mode, "taker")
        self.assertGreaterEqual(decision.entry_score, 9)

    def test_maker_accepts_confirmed_edge_when_taker_is_not_ready(self) -> None:
        config = StrategyConfig(maker_score_threshold=6)
        weak_burst = strong_long_signal(tape_ofi_raw=0.11, trade_burst_score=0.0)
        taker = TakerStrategy(config).evaluate(self.snapshot, weak_burst)
        maker = MakerStrategy(config).evaluate(self.snapshot, weak_burst)
        self.assertFalse(taker.allow)
        self.assertTrue(maker.allow)
        self.assertEqual(maker.entry_mode, "maker")

    def test_price_breakout_path_requires_burst_or_strong_tape(self) -> None:
        config = StrategyConfig(taker_score_threshold=9, strong_signal_multiplier=3.0)
        balanced_depth = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=1000,
            bids=(Level(100.0, 1000), Level(99.0, 500)),
            asks=(Level(101.0, 1000), Level(102.0, 500)),
        )
        weak_breakout = strong_long_signal(
            breakout_long=True,
            tape_ofi_raw=0.11,
            trade_burst_score=0.0,
        )
        decision = TakerStrategy(config).evaluate(balanced_depth, weak_breakout, now_ns=1)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "taker_breakout")

    def test_price_breakout_path_allows_strong_tape_without_burst(self) -> None:
        config = StrategyConfig(taker_score_threshold=9, strong_signal_multiplier=3.0)
        balanced_depth = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=1000,
            bids=(Level(100.0, 1000), Level(99.0, 500)),
            asks=(Level(101.0, 1000), Level(102.0, 500)),
        )
        strong_breakout = strong_long_signal(
            breakout_long=True,
            tape_ofi_raw=0.31,
            trade_burst_score=0.0,
        )
        decision = TakerStrategy(config).evaluate(balanced_depth, strong_breakout, now_ns=1)
        self.assertTrue(decision.allow)
        self.assertEqual(decision.entry_mode, "taker")

    def test_depth_thin_trigger_is_classified(self) -> None:
        config = StrategyConfig(taker_score_threshold=9, strong_signal_multiplier=1.5)
        strategy = TakerStrategy(config)
        signal = strong_long_signal()

        decision = strategy.evaluate(self.snapshot, signal, now_ns=1)

        self.assertTrue(decision.allow)
        self.assertEqual(strategy.classify_entry_trigger(self.snapshot, signal, 1), "depth_thin")

    def test_depth_thin_switch_disables_depth_trigger(self) -> None:
        config = StrategyConfig(taker_score_threshold=9, use_depth_thin_taker=False)
        decision = TakerStrategy(config).evaluate(self.snapshot, strong_long_signal(), now_ns=1)

        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "taker_breakout")

    def test_wall_break_trigger_is_classified_and_switchable(self) -> None:
        balanced_depth = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=1000,
            bids=(Level(100.0, 1000),),
            asks=(Level(101.0, 1000),),
        )
        signal = strong_long_signal(wall_ask_consumed=True, wall_ask_consumed_ratio=0.75)

        enabled = TakerStrategy(StrategyConfig(taker_score_threshold=9))
        disabled = TakerStrategy(StrategyConfig(taker_score_threshold=9, use_wall_break_taker=False))

        self.assertTrue(enabled.evaluate(balanced_depth, signal, now_ns=1).allow)
        self.assertEqual(enabled.classify_entry_trigger(balanced_depth, signal, 1), "wall_break")
        self.assertFalse(disabled.evaluate(balanced_depth, signal, now_ns=1).allow)

    def test_cancel_imbalance_trigger_is_classified_and_switchable(self) -> None:
        balanced_depth = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=1000,
            bids=(Level(100.0, 1000),),
            asks=(Level(101.0, 1000),),
        )
        signal = strong_long_signal(ask_cancel_ratio=0.45)

        enabled = TakerStrategy(StrategyConfig(taker_score_threshold=9))
        disabled = TakerStrategy(StrategyConfig(taker_score_threshold=9, use_cancel_imbalance_taker=False))

        self.assertTrue(enabled.evaluate(balanced_depth, signal, now_ns=1).allow)
        self.assertEqual(enabled.classify_entry_trigger(balanced_depth, signal, 1), "cancel_imbalance")
        self.assertFalse(disabled.evaluate(balanced_depth, signal, now_ns=1).allow)

    def test_cancel_imbalance_extreme_blocks_chasing(self) -> None:
        signal = strong_long_signal(ask_cancel_ratio=0.80)
        decision = TakerStrategy(StrategyConfig(taker_score_threshold=9)).evaluate(
            self.snapshot,
            signal,
            now_ns=1,
        )

        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "taker_cancel_extreme")

    def test_price_breakout_trigger_is_classified(self) -> None:
        balanced_depth = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=1000,
            bids=(Level(100.0, 1000),),
            asks=(Level(101.0, 1000),),
        )
        signal = strong_long_signal(breakout_long=True, trade_burst_score=0.0)
        strategy = TakerStrategy(StrategyConfig(taker_score_threshold=9))

        self.assertTrue(strategy.evaluate(balanced_depth, signal, now_ns=1).allow)
        self.assertEqual(strategy.classify_entry_trigger(balanced_depth, signal, 1), "price_breakout")

    def test_vol_expansion_trigger_is_classified_by_default(self) -> None:
        balanced_depth = BoardSnapshot(
            symbol="9984",
            ts_ns=1,
            bid=100.0,
            ask=101.0,
            bid_size=1000,
            ask_size=1000,
            bids=(Level(100.0, 1000),),
            asks=(Level(101.0, 1000),),
        )
        signal = strong_long_signal(vol_expansion=True, trade_burst_score=0.0)
        strategy = TakerStrategy(StrategyConfig(taker_score_threshold=9))

        self.assertTrue(strategy.evaluate(balanced_depth, signal, now_ns=1).allow)
        self.assertEqual(strategy.classify_entry_trigger(balanced_depth, signal, 1), "vol_expansion")


if __name__ == "__main__":
    unittest.main()
