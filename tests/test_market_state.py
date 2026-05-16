"""Tests for MarketStateDetector — state-machine classifying each board tick."""
from __future__ import annotations

import unittest

from kabu_maker_taker.config import MarketStateConfig
from kabu_maker_taker.models import BoardSnapshot, Level, MarketState
from kabu_maker_taker.strategy import MarketStateDetector


def _snap(
    bid: float = 100.0,
    ask: float = 101.0,
    ts_ns: int = 1_000_000_000,
) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=ts_ns,
        bid=bid,
        ask=ask,
        bid_size=500,
        ask_size=200,
        bids=(Level(bid, 500), Level(bid - 1.0, 300)),
        asks=(Level(ask, 200), Level(ask + 1.0, 250)),
    )


def _detector(**kwargs) -> MarketStateDetector:
    defaults: dict = dict(
        enabled=True,
        abnormal_spread_ticks=5.0,
        abnormal_event_rate_hz=100.0,
        event_rate_window_seconds=1,
        abnormal_price_jump_ticks=3.0,
    )
    defaults.update(kwargs)
    config = MarketStateConfig(**defaults)
    return MarketStateDetector(config, tick_size=1.0)


class MarketStateDetectorTests(unittest.TestCase):
    def test_disabled_always_returns_normal(self) -> None:
        """When enabled=False every snapshot returns NORMAL regardless of spread."""
        config = MarketStateConfig(enabled=False, abnormal_spread_ticks=1.0)
        det = MarketStateDetector(config, tick_size=1.0)
        # spread = 50 ticks — would be ABNORMAL if enabled
        result = det.update(_snap(bid=100.0, ask=150.0), now_ns=1_000_000_000)
        self.assertEqual(result, MarketState.NORMAL)

    def test_normal_state_on_healthy_board(self) -> None:
        """Spread of 2 ticks is healthy: spread > tick_size and < abnormal threshold."""
        det = _detector()
        # spread = 2 ticks (100..102) — above 1-tick locked threshold, below 5-tick abnormal limit
        result = det.update(_snap(bid=100.0, ask=102.0), now_ns=1_000_000_000)
        self.assertEqual(result, MarketState.NORMAL)

    def test_abnormal_on_wide_spread(self) -> None:
        """Spread >= abnormal_spread_ticks (5) triggers ABNORMAL."""
        det = _detector()
        # spread = 5 ticks (100..105)
        result = det.update(_snap(bid=100.0, ask=105.0), now_ns=1_000_000_000)
        self.assertEqual(result, MarketState.ABNORMAL)

    def test_queue_state_when_spread_is_one_tick(self) -> None:
        """spread <= tick_size (locked/touched market) → QUEUE state."""
        det = _detector()
        # spread = 1 tick = tick_size exactly → QUEUE
        result = det.update(_snap(bid=100.0, ask=101.0), now_ns=1_000_000_000)
        self.assertEqual(result, MarketState.QUEUE)

    def test_abnormal_on_large_price_jump(self) -> None:
        """Price jump >= abnormal_price_jump_ticks (3) triggers ABNORMAL."""
        det = _detector()
        det.update(_snap(bid=100.0, ask=101.0), now_ns=1_000_000_000)  # set prev_mid
        # mid jumps from 100.5 to 104.5 = 4 ticks
        result = det.update(_snap(bid=104.0, ask=105.0), now_ns=2_000_000_000)
        self.assertEqual(result, MarketState.ABNORMAL)

    def test_no_jump_on_first_tick(self) -> None:
        """First tick has no prior mid — price jump check is skipped."""
        det = _detector()
        # Even a very large move on the first tick is not flagged as a jump
        result = det.update(_snap(bid=200.0, ask=201.0), now_ns=1_000_000_000)
        # spread=1 → QUEUE (but NOT ABNORMAL due to jump)
        self.assertNotEqual(result, MarketState.ABNORMAL)

    def test_abnormal_on_high_event_rate(self) -> None:
        """More than abnormal_event_rate_hz events per second triggers ABNORMAL."""
        # rate = events / window_seconds; threshold = 5; need len(deque) / 1 >= 5
        det = _detector(abnormal_event_rate_hz=5.0, event_rate_window_seconds=1)
        snap = _snap(bid=100.0, ask=102.0)  # 2-tick spread to avoid early spread-based ABNORMAL
        base_ns = 1_000_000_000
        # Fire 5 events within 1 second — rate = 5 == threshold → triggers ABNORMAL (>=)
        state = MarketState.NORMAL
        for i in range(5):
            state = det.update(snap, now_ns=base_ns + i * 10_000_000)
        self.assertEqual(state, MarketState.ABNORMAL)

    def test_recovers_to_normal_after_spread_narrows(self) -> None:
        """After an ABNORMAL tick, a healthy board should return NORMAL again."""
        det = _detector()
        det.update(_snap(bid=100.0, ask=105.0), now_ns=1_000_000_000)  # ABNORMAL
        result = det.update(_snap(bid=100.0, ask=102.0), now_ns=2_000_000_000)
        self.assertEqual(result, MarketState.NORMAL)

    def test_state_property_reflects_last_update(self) -> None:
        det = _detector()
        det.update(_snap(bid=100.0, ask=105.0), now_ns=1_000_000_000)
        self.assertEqual(det.state, MarketState.ABNORMAL)
        det.update(_snap(bid=100.0, ask=102.0), now_ns=2_000_000_000)
        self.assertEqual(det.state, MarketState.NORMAL)


if __name__ == "__main__":
    unittest.main()
