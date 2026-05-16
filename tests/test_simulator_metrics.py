from __future__ import annotations

import unittest

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, EntryDecision, Level, MarketState, SignalPacket
from kabu_maker_taker.simulator import DryRunSimulator


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


def _snapshot(ts_ns: int = 1_000_000_000, bid: float = 100.0, ask: float = 101.0, bid_size: int = 500) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=ts_ns,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=200,
        bids=(Level(bid, bid_size), Level(bid - 1.0, 300)),
        asks=(Level(ask, 200), Level(ask + 1.0, 250)),
    )


def _strategy(entry_mode: str) -> CombinedMakerTakerStrategy:
    config = AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=1,
        strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1),
        risk=RiskConfig(max_spread_ticks=5.0, slippage_ticks_default=0.0),
        lollipop=LollipopConfig(tp_delay_ms=0, stop_loss_ticks=0.0),
    )
    strategy = CombinedMakerTakerStrategy(config)
    strategy.signals.on_board = lambda snapshot: _signal(ts_ns=snapshot.ts_ns)
    strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
        True,
        "",
        entry_mode=entry_mode,
        side=1,
        entry_score=10,
        required_confirm=1,
    )
    return strategy


def _apply_events(strategy: CombinedMakerTakerStrategy, events: list[BrokerOrderEvent | BrokerFillEvent]) -> None:
    for event in events:
        if isinstance(event, BrokerOrderEvent):
            strategy.on_broker_order_event(event)
        else:
            strategy.on_broker_fill(event)


class DryRunSimulatorTests(unittest.TestCase):
    def test_taker_fill_uses_broker_events_and_slippage(self) -> None:
        strategy = _strategy("taker")
        simulator = DryRunSimulator(tick_size=1.0, slippage_ticks=1.0)
        snap = _snapshot()

        result = strategy.on_board(snap, now_ns=snap.ts_ns)
        assert result.intent is not None
        self.assertEqual(strategy.position.qty, 0)

        events = simulator.submit(result.intent, snap, snap.ts_ns)
        self.assertEqual(strategy.position.qty, 0)
        _apply_events(strategy, events)

        self.assertEqual(strategy.position.qty, 100)
        self.assertEqual(strategy.position.avg_price, 101.0)
        self.assertEqual(strategy.metrics.entry_intent_count, 1)
        self.assertEqual(strategy.metrics.taker_fill_count, 1)

    def test_maker_queue_partial_fill_uses_broker_fill_event(self) -> None:
        from kabu_maker_taker.models import TradePrint
        strategy = _strategy("maker")
        simulator = DryRunSimulator(tick_size=1.0)
        snap = _snapshot(bid_size=500)

        result = strategy.on_board(snap, now_ns=snap.ts_ns)
        assert result.intent is not None
        _apply_events(strategy, simulator.submit(result.intent, snap, snap.ts_ns))
        self.assertEqual(strategy.position.qty, 0)
        self.assertEqual(simulator.queue_ahead(result.intent.client_order_id), 500)

        # With the trade-print queue model, a size drop alone (cancel) no longer
        # triggers a fill.  We need actual sells at our bid price to consume the
        # queue.  Submit a trade that overshoots our queue_ahead (500) by a small
        # margin so exactly leaves_qty (100) shares fill.
        bid_price = result.intent.price
        simulator.on_trade(
            TradePrint(symbol="9984", ts_ns=1_000_000_050, price=bid_price, size=600, side=-1),
            1_000_000_050,
        )
        fill_events = simulator.on_board(_snapshot(ts_ns=1_000_000_100, bid_size=0), 1_000_000_100)
        self.assertEqual(len(fill_events), 1)
        _apply_events(strategy, fill_events)

        self.assertEqual(strategy.position.qty, 100)
        self.assertEqual(strategy.position.avg_price, result.intent.price)
        self.assertEqual(strategy.metrics.maker_fill_count, 1)

    def test_market_exit_fill_updates_exit_split_and_realized_pnl(self) -> None:
        strategy = _strategy("taker")
        simulator = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snapshot()
        entry = strategy.on_board(snap, now_ns=snap.ts_ns)
        assert entry.intent is not None
        _apply_events(strategy, simulator.submit(entry.intent, snap, snap.ts_ns))

        strategy.lollipop.force_exit_next_tick()
        exit_result = strategy.on_board(_snapshot(ts_ns=1_000_000_100, bid=103.0, ask=104.0), now_ns=1_000_000_100)
        assert exit_result.exit_intent is not None
        _apply_events(strategy, simulator.submit(exit_result.exit_intent, _snapshot(1_000_000_100, 103.0, 104.0), 1_000_000_100))

        self.assertEqual(strategy.position.qty, 0)
        summary = strategy.metrics.to_dict()
        self.assertEqual(summary["market_exit_count"], 1)
        self.assertEqual(summary["closed_trades"], 1)
        self.assertGreater(summary["realized_pnl"], 0)

    def test_cancel_signal_removes_pending_order(self) -> None:
        """Cancelling a pending maker order via simulator.cancel() must prevent future fills."""
        strategy = _strategy("maker")
        simulator = DryRunSimulator(tick_size=1.0)
        snap = _snapshot(bid_size=500)

        result = strategy.on_board(snap, now_ns=snap.ts_ns)
        assert result.intent is not None
        _apply_events(strategy, simulator.submit(result.intent, snap, snap.ts_ns))
        self.assertEqual(strategy.position.qty, 0)
        self.assertTrue(strategy.entry_order_active)

        # Simulate cancel: pop from simulator queue and feed CANCELED event back
        cancel_events = simulator.cancel(result.intent.client_order_id, snap.ts_ns + 1)
        self.assertEqual(len(cancel_events), 1)
        strategy.on_broker_order_event(cancel_events[0])

        # Order should no longer be active
        self.assertFalse(strategy.entry_order_active)

        # Board at a price that would have filled the order: no fills since it was removed
        fill_events = simulator.on_board(_snapshot(ts_ns=snap.ts_ns + 100, bid_size=400), snap.ts_ns + 100)
        self.assertEqual(len(fill_events), 0)
        self.assertEqual(strategy.position.qty, 0)

    def test_win_loss_rate_tracked(self) -> None:
        """win_count / loss_count / win_rate in metrics.to_dict() after a profitable exit."""
        strategy = _strategy("taker")
        simulator = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snapshot()

        # Entry fill
        entry = strategy.on_board(snap, now_ns=snap.ts_ns)
        assert entry.intent is not None
        _apply_events(strategy, simulator.submit(entry.intent, snap, snap.ts_ns))
        self.assertEqual(strategy.position.qty, 100)

        # Profitable exit: bid rose to 103
        strategy.lollipop.force_exit_next_tick()
        exit_snap = _snapshot(ts_ns=snap.ts_ns + 100, bid=103.0, ask=104.0)
        exit_result = strategy.on_board(exit_snap, now_ns=exit_snap.ts_ns)
        assert exit_result.exit_intent is not None
        _apply_events(strategy, simulator.submit(exit_result.exit_intent, exit_snap, exit_snap.ts_ns))

        self.assertEqual(strategy.position.qty, 0)
        summary = strategy.metrics.to_dict()
        self.assertEqual(summary["win_count"], 1)
        self.assertEqual(summary["loss_count"], 0)
        self.assertAlmostEqual(summary["win_rate"], 1.0)

    def test_markout_is_computed_after_future_boards(self) -> None:
        strategy = _strategy("taker")
        first = strategy.on_board(_snapshot(1_000_000_000, bid=100.0, ask=101.0), now_ns=1_000_000_000)
        self.assertIsNotNone(first.intent)
        strategy.on_broker_order_event(BrokerOrderEvent(order_id=first.intent.client_order_id, status="rejected"))

        strategy.on_board(_snapshot(1_000_000_100, bid=101.0, ask=102.0), now_ns=1_000_000_100)
        strategy.on_board(_snapshot(1_000_000_200, bid=102.0, ask=103.0), now_ns=1_000_000_200)
        strategy.on_board(_snapshot(1_000_000_300, bid=103.0, ask=104.0), now_ns=1_000_000_300)

        summary = strategy.metrics.to_dict()
        self.assertEqual(summary["markout_count"], 1)
        self.assertGreater(summary["average_markout_ticks"], 0)

    def test_timed_markout_buckets_are_computed_at_horizons(self) -> None:
        strategy = _strategy("taker")
        first = strategy.on_board(_snapshot(1_000_000_000, bid=100.0, ask=101.0), now_ns=1_000_000_000)
        self.assertIsNotNone(first.intent)
        strategy.on_broker_order_event(BrokerOrderEvent(order_id=first.intent.client_order_id, status="rejected"))
        strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            False,
            "test_block",
        )

        strategy.on_board(_snapshot(1_099_999_999, bid=101.0, ask=102.0), now_ns=1_099_999_999)
        early = strategy.metrics.to_dict()
        self.assertEqual(early["markout_100ms_count"], 0)

        strategy.on_board(_snapshot(1_100_000_000, bid=102.0, ask=103.0), now_ns=1_100_000_000)
        after_100ms = strategy.metrics.to_dict()
        self.assertEqual(after_100ms["markout_100ms_count"], 1)
        self.assertGreater(after_100ms["average_markout_100ms_ticks"], 0)
        self.assertEqual(after_100ms["markout_500ms_count"], 0)

        strategy.on_board(_snapshot(1_500_000_000, bid=103.0, ask=104.0), now_ns=1_500_000_000)
        after_500ms = strategy.metrics.to_dict()
        self.assertEqual(after_500ms["markout_500ms_count"], 1)
        self.assertGreater(after_500ms["average_markout_500ms_ticks"], after_100ms["average_markout_100ms_ticks"])

        strategy.on_board(_snapshot(2_000_000_000, bid=104.0, ask=105.0), now_ns=2_000_000_000)
        strategy.on_board(_snapshot(4_000_000_000, bid=105.0, ask=106.0), now_ns=4_000_000_000)
        final = strategy.metrics.to_dict()
        self.assertEqual(final["markout_1s_count"], 1)
        self.assertEqual(final["markout_3s_count"], 1)
        self.assertGreater(final["average_markout_3s_ticks"], final["average_markout_1s_ticks"])


if __name__ == "__main__":
    unittest.main()
