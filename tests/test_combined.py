from __future__ import annotations

import unittest

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, RiskConfig, SignalConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, Level, TradePrint


class CombinedStrategyTests(unittest.TestCase):
    def test_taker_has_priority_over_maker(self) -> None:
        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            strategy=StrategyConfig(taker_confirm_ticks=1, maker_confirm_ticks=1),
            risk=RiskConfig(max_spread_ticks=3.0),
            signals=SignalConfig(zscore_window=2),
        )
        strategy = CombinedMakerTakerStrategy(config)
        base = 1_770_000_000_000_000_000
        strategy.on_trade(TradePrint("9984", base, 100.8, 500, 1))
        first = BoardSnapshot(
            symbol="9984",
            ts_ns=base + 100_000_000,
            bid=100.0,
            ask=101.0,
            bid_size=900,
            ask_size=200,
            bids=(Level(100.0, 900), Level(99.0, 500)),
            asks=(Level(101.0, 200), Level(102.0, 250)),
        )
        result = strategy.on_board(first, now_ns=first.ts_ns)
        strategy.on_trade(TradePrint("9984", base + 150_000_000, 101.0, 800, 1))
        second = BoardSnapshot(
            symbol="9984",
            ts_ns=base + 200_000_000,
            bid=101.0,
            ask=102.0,
            bid_size=1200,
            ask_size=180,
            bids=(Level(101.0, 1200), Level(100.0, 700)),
            asks=(Level(102.0, 180), Level(103.0, 220)),
        )
        blocked = strategy.on_board(second, now_ns=second.ts_ns)
        self.assertIsNotNone(result.intent)
        self.assertEqual(result.intent.strategy, "taker")
        self.assertTrue(result.intent.is_market)
        self.assertIsNone(blocked.intent)
        self.assertEqual(blocked.blocked_reason, "working_entry")

    def test_risk_blocks_wide_spread(self) -> None:
        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            strategy=StrategyConfig(taker_confirm_ticks=1, maker_confirm_ticks=1),
            risk=RiskConfig(max_spread_ticks=1.0),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.last_signal = None
        base = 1_770_000_000_000_000_000
        strategy.on_trade(TradePrint("9984", base, 100.0, 500, 1))
        snapshot = BoardSnapshot(
            symbol="9984",
            ts_ns=base + 100_000_000,
            bid=100.0,
            ask=105.0,
            bid_size=1200,
            ask_size=100,
            bids=(Level(100.0, 1200),),
            asks=(Level(105.0, 100),),
        )
        result = strategy.on_board(snapshot, now_ns=snapshot.ts_ns)
        self.assertIsNone(result.intent)
        self.assertIn(result.blocked_reason, {"spread_too_wide", "confirming", "maker_primary", "taker_primary"})


if __name__ == "__main__":
    unittest.main()
