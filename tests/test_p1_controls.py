from __future__ import annotations

import unittest

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import (
    BoardSnapshot,
    BrokerOrderEvent,
    EntryDecision,
    Level,
    MarketState,
    OrderStatus,
    PositionState,
    SignalPacket,
)
from kabu_maker_taker.risk import RiskManager


def _signal(**overrides) -> SignalPacket:
    values = {
        "ts_ns": 1_000_000_000,
        "obi_raw": 0.35,
        "lob_ofi_raw": 0.20,
        "tape_ofi_raw": 0.20,
        "micro_momentum_raw": 0.10,
        "microprice_tilt_raw": 0.30,
        "microprice": 100.3,
        "mid": 100.0,
        "obi_z": 0.0,
        "lob_ofi_z": 0.0,
        "tape_ofi_z": 0.0,
        "micro_momentum_z": 0.0,
        "microprice_tilt_z": 0.0,
        "composite": 0.50,
        "integrated_ofi": 0.20,
        "trade_burst_score": 0.10,
    }
    values.update(overrides)
    return SignalPacket(**values)


def _snapshot(ts_ns: int = 1_000_000_000, bid: float = 100.0, ask: float = 101.0) -> BoardSnapshot:
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


def _strategy(*, risk: RiskConfig | None = None, signal: SignalPacket | None = None) -> CombinedMakerTakerStrategy:
    config = AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=1,
        strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1, min_order_age_ms=0),
        risk=risk or RiskConfig(max_spread_ticks=5.0),
        lollipop=LollipopConfig(tp_delay_ms=0, stop_loss_ticks=0.0),
    )
    strategy = CombinedMakerTakerStrategy(config)
    strategy.signals.on_board = lambda snapshot: signal or _signal(ts_ns=snapshot.ts_ns)
    strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
        True,
        "",
        entry_mode="maker",
        side=1,
        entry_score=8,
        required_confirm=1,
    )
    return strategy


class P1RiskControlTests(unittest.TestCase):
    def test_order_rate_limit_blocks_new_entry(self) -> None:
        strategy = _strategy(risk=RiskConfig(max_spread_ticks=5.0, max_entry_orders_per_minute=1))
        first = strategy.on_board(_snapshot(1_000_000_000), now_ns=1_000_000_000)
        assert first.intent is not None
        strategy.on_broker_order_event(
            BrokerOrderEvent(order_id=first.intent.client_order_id, status=OrderStatus.REJECTED, ts_ns=1_000_000_100)
        )

        second = strategy.on_board(_snapshot(1_000_001_000), now_ns=1_000_001_000)
        self.assertIsNone(second.intent)
        self.assertEqual(second.blocked_reason, "order_rate_limit")
        self.assertEqual(strategy.metrics.order_rate_blocks, 1)

    def test_order_rate_limit_does_not_block_lollipop_emergency_exit(self) -> None:
        strategy = _strategy(risk=RiskConfig(max_spread_ticks=5.0, max_entry_orders_per_minute=1))
        strategy.risk.record_entry_order(1_000_000_000)
        strategy.restore_position(side=1, qty=100, avg_price=101.0, now_ns=1_000_000_000)
        strategy.lollipop.force_exit_next_tick()

        result = strategy.on_board(_snapshot(1_000_001_000, bid=100.0, ask=101.0), now_ns=1_000_001_000)
        self.assertIsNotNone(result.exit_intent)
        assert result.exit_intent is not None
        self.assertTrue(result.exit_intent.is_market)
        self.assertEqual(result.exit_intent.reason, "timeout_exit")

    def test_cancel_rate_limit_blocks_non_urgent_cancel_signal(self) -> None:
        bad_signal = _signal(composite=-1.0, tape_ofi_raw=0.20, obi_raw=0.20)
        strategy = _strategy(
            risk=RiskConfig(max_spread_ticks=5.0, max_cancel_requests_per_minute=1),
            signal=bad_signal,
        )
        first = strategy.on_board(_snapshot(1_000_000_000), now_ns=1_000_000_000)
        assert first.intent is not None

        allowed = strategy.on_board(_snapshot(1_000_001_000), now_ns=1_000_001_000)
        self.assertEqual(allowed.entry_cancel_signal, "alpha_flip")
        self.assertEqual(allowed.entry_cancel_blocked_reason, "")

        blocked = strategy.on_board(_snapshot(1_000_002_000), now_ns=1_000_002_000)
        self.assertEqual(blocked.entry_cancel_signal, "")
        self.assertEqual(blocked.entry_cancel_blocked_reason, "cancel_rate_limit")
        self.assertEqual(strategy.metrics.cancel_rate_blocks, 1)

    def test_urgent_cancel_bypasses_cancel_rate_limit(self) -> None:
        strategy = _strategy(risk=RiskConfig(max_spread_ticks=20.0, max_cancel_requests_per_minute=1))
        first = strategy.on_board(_snapshot(1_000_000_000), now_ns=1_000_000_000)
        assert first.intent is not None
        strategy.risk.record_cancel_request("alpha_flip", 1_000_000_100)

        result = strategy.on_board(_snapshot(1_000_001_000, bid=100.0, ask=106.0), now_ns=1_000_001_000)
        self.assertEqual(result.entry_cancel_signal, "spread_expanded")
        self.assertEqual(result.entry_cancel_blocked_reason, "")

    def test_api_circuit_blocks_and_recovers_after_cooling(self) -> None:
        base = 1_000_000_000
        strategy = _strategy(risk=RiskConfig(max_spread_ticks=5.0, api_error_limit=2, api_cooling_seconds=2))
        self.assertFalse(strategy.on_api_error(base))
        self.assertTrue(strategy.on_api_error(base + 1))

        blocked = strategy.on_board(_snapshot(base + 10), now_ns=base + 10)
        self.assertEqual(blocked.blocked_reason, "api_circuit_open")

        recovered = strategy.on_board(_snapshot(base + 3_000_000_000), now_ns=base + 3_000_000_000)
        self.assertIsNotNone(recovered.intent)

    def test_latency_circuit_opens_after_consecutive_submit_breaches(self) -> None:
        base = 1_000_000_000
        strategy = _strategy(
            risk=RiskConfig(
                max_spread_ticks=5.0,
                order_latency_limit_ms=10,
                latency_breach_limit=2,
                api_cooling_seconds=2,
            )
        )

        self.assertFalse(strategy.on_rest_latency("submit", 11.0, base))
        self.assertEqual(strategy.risk.latency_breach_count("submit"), 1)
        self.assertTrue(strategy.on_rest_latency("submit", 12.0, base + 1))
        self.assertEqual(strategy.metrics.latency_circuit_opens, 1)

        blocked = strategy.on_board(_snapshot(base + 10), now_ns=base + 10)
        self.assertIsNone(blocked.intent)
        self.assertEqual(blocked.blocked_reason, "latency_circuit_open")
        self.assertEqual(strategy.metrics.latency_blocks, 1)

        recovered = strategy.on_board(_snapshot(base + 3_000_000_000), now_ns=base + 3_000_000_000)
        self.assertIsNotNone(recovered.intent)
        self.assertEqual(strategy.risk.latency_breach_count("submit"), 0)

    def test_latency_circuit_tracks_cancel_and_poll_independently(self) -> None:
        base = 1_000_000_000
        strategy = _strategy(
            risk=RiskConfig(
                max_spread_ticks=5.0,
                cancel_latency_limit_ms=10,
                poll_latency_limit_ms=20,
                latency_breach_limit=2,
                api_cooling_seconds=2,
            )
        )

        self.assertFalse(strategy.on_rest_latency("cancel", 11.0, base))
        self.assertFalse(strategy.on_rest_latency("poll", 21.0, base + 1))
        self.assertEqual(strategy.risk.latency_breach_count("cancel"), 1)
        self.assertEqual(strategy.risk.latency_breach_count("poll"), 1)
        self.assertFalse(strategy.on_rest_latency("cancel", 5.0, base + 2))
        self.assertEqual(strategy.risk.latency_breach_count("cancel"), 0)

        self.assertTrue(strategy.on_rest_latency("poll", 22.0, base + 3))
        blocked = strategy.on_board(_snapshot(base + 10), now_ns=base + 10)
        self.assertEqual(blocked.blocked_reason, "latency_circuit_open")

        metrics = strategy.metrics.to_dict()
        self.assertEqual(metrics["cancel_latency_ms_count"], 2)
        self.assertEqual(metrics["poll_latency_ms_count"], 2)
        self.assertEqual(metrics["poll_latency_ms_max"], 22.0)

    def test_restore_position_long_and_short_start_lollipop_exit_management(self) -> None:
        strategy = _strategy()
        restored = strategy.restore_position(side=1, qty=100, avg_price=101.0, now_ns=1_000_000_000)
        self.assertEqual(restored.side, 1)
        long_result = strategy.on_board(_snapshot(1_000_000_100, bid=101.0, ask=102.0), now_ns=1_000_000_100)
        self.assertIsNotNone(long_result.exit_intent)
        assert long_result.exit_intent is not None
        self.assertEqual(long_result.exit_intent.side, -1)

        short_strategy = _strategy()
        short_strategy.restore_position(side=-1, qty=100, avg_price=101.0, now_ns=1_000_000_000)
        short_result = short_strategy.on_board(_snapshot(1_000_000_100, bid=100.0, ask=101.0), now_ns=1_000_000_100)
        self.assertIsNotNone(short_result.exit_intent)
        assert short_result.exit_intent is not None
        self.assertEqual(short_result.exit_intent.side, 1)

    def test_restore_position_validation(self) -> None:
        strategy = _strategy()
        with self.assertRaises(ValueError):
            strategy.restore_position(side=0, qty=100, avg_price=100.0)
        with self.assertRaises(ValueError):
            strategy.restore_position(side=1, qty=0, avg_price=100.0)
        with self.assertRaises(ValueError):
            strategy.restore_position(side=1, qty=100, avg_price=0.0)

    def test_daily_pnl_includes_fee_and_slippage_estimates(self) -> None:
        rm = RiskManager(
            config=RiskConfig(daily_loss_limit=5000.0, fee_per_share=1.0, slippage_ticks_default=1.0),
            tick_size=1.0,
            lot_size=100,
        )
        ns = 1_747_184_400_000_000_000
        net = rm.record_trade_result(False, ns, pnl=-4600.0, qty=100)
        self.assertAlmostEqual(net, -5000.0)
        ok, reason = rm.can_enter(
            snapshot=_snapshot(ts_ns=ns),
            decision=EntryDecision(True, "", entry_mode="maker", side=1),
            position=PositionState(),
            now_ns=ns + 1,
            expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_limit")


if __name__ == "__main__":
    unittest.main()
