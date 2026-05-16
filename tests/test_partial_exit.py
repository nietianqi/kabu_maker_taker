from __future__ import annotations

import unittest

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import (
    BoardSnapshot,
    BrokerFillEvent,
    EntryDecision,
    Level,
    MarketState,
    PositionState,
    SignalPacket,
)


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


def _signal(**overrides) -> SignalPacket:
    values = dict(
        ts_ns=1_000_000_000,
        obi_raw=0.35, lob_ofi_raw=0.20, tape_ofi_raw=0.20,
        micro_momentum_raw=0.10, microprice_tilt_raw=0.30,
        microprice=100.3, mid=100.0,
        obi_z=0.0, lob_ofi_z=0.0, tape_ofi_z=0.0,
        micro_momentum_z=0.0, microprice_tilt_z=0.0,
        composite=0.50, integrated_ofi=0.20, trade_burst_score=0.10,
    )
    values.update(overrides)
    return SignalPacket(**values)


def _strategy() -> CombinedMakerTakerStrategy:
    config = AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=1,
        strategy=StrategyConfig(trade_qty=100),
        risk=RiskConfig(max_spread_ticks=5.0, daily_loss_limit=5000.0, fee_per_share=0.0, slippage_ticks_default=0.0),
        lollipop=LollipopConfig(tp_delay_ms=0, stop_loss_ticks=0.0),
    )
    s = CombinedMakerTakerStrategy(config)
    s.signals.on_board = lambda snap: _signal(ts_ns=snap.ts_ns)
    s._choose_decision = lambda snap, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
        True, "", entry_mode="taker", side=1, entry_score=10, required_confirm=1,
    )
    return s


class PartialExitPnLTests(unittest.TestCase):
    def _open_position(self, strategy: CombinedMakerTakerStrategy, qty: int = 100, avg_price: float = 100.0) -> None:
        strategy.restore_position(side=1, qty=qty, avg_price=avg_price, entry_mode="taker", now_ns=0)

    # ------------------------------------------------------------------ #
    def test_partial_exit_updates_daily_pnl_in_risk(self) -> None:
        """Partial exit must move risk._daily_pnl so daily loss limit can fire."""
        strategy = _strategy()
        self._open_position(strategy, qty=100, avg_price=100.0)

        # Exit 30 shares at 98.0 (loss of 2 per share → −60 gross)
        fill = BrokerFillEvent(order_id="x", qty=30, price=98.0, ts_ns=2_000_000_000)
        strategy._apply_broker_fill(
            strategy.orders.add_intent(
                strategy.lollipop.tick(
                    _snapshot(ts_ns=2_000_000_000, bid=98.0, ask=99.0),
                    strategy.position, 2_000_000_000,
                    symbol="9984", exchange=27,
                ).intent,  # type: ignore[arg-type]
                role="exit", now_ns=2_000_000_000,
            ),
            30, 98.0, 2_000_000_000,
        )

        # The simpler test: just call _apply_position_fill directly
        strategy2 = _strategy()
        self._open_position(strategy2, qty=100, avg_price=100.0)
        outcome = strategy2._apply_position_fill(side=-1, qty=30, price=98.0, now_ns=2_000_000_000, entry_mode="")
        self.assertEqual(outcome, "partial_exit")
        self.assertLess(strategy2.risk.daily_pnl, 0.0)  # daily PnL must be negative
        self.assertEqual(strategy2.position.qty, 70)

    def test_partial_exit_adds_to_realized_pnl_metrics(self) -> None:
        """metrics.realized_pnl must include partial exit PnL; closed_trades must stay 0."""
        strategy = _strategy()
        self._open_position(strategy, qty=100, avg_price=100.0)

        outcome = strategy._apply_position_fill(side=-1, qty=40, price=103.0, now_ns=1_000_000_001, entry_mode="")
        self.assertEqual(outcome, "partial_exit")
        # 40 shares × +3.0 = +120 gross → net > 0
        self.assertGreater(strategy.metrics.realized_pnl, 0.0)
        self.assertGreater(strategy.metrics.partial_exit_pnl, 0.0)
        self.assertEqual(strategy.metrics.closed_trades, 0)

    def test_partial_exit_pnl_in_to_dict(self) -> None:
        """metrics.to_dict() must expose partial_exit_pnl key."""
        strategy = _strategy()
        self._open_position(strategy, qty=100, avg_price=100.0)
        strategy._apply_position_fill(side=-1, qty=10, price=101.0, now_ns=0, entry_mode="")
        summary = strategy.metrics.to_dict()
        self.assertIn("partial_exit_pnl", summary)
        self.assertGreater(summary["partial_exit_pnl"], 0.0)

    def test_daily_loss_limit_fires_after_large_partial_exit(self) -> None:
        """A losing partial exit must update daily PnL so can_enter() blocks new orders."""
        strategy = _strategy()
        # Set daily limit to 100 JPY loss
        strategy.risk.config = RiskConfig(
            max_spread_ticks=5.0,
            daily_loss_limit=100.0,
            fee_per_share=0.0,
            slippage_ticks_default=0.0,
        )
        self._open_position(strategy, qty=100, avg_price=200.0)

        # Exit 50 shares at 197 → −150 gross, exceeds 100 limit
        strategy._apply_position_fill(side=-1, qty=50, price=197.0, now_ns=2_000_000_000, entry_mode="")
        self.assertLess(strategy.risk.daily_pnl, -100.0)

        snap = _snapshot(ts_ns=2_000_000_001, bid=197.0, ask=198.0)
        from kabu_maker_taker.models import EntryDecision, PositionState
        ok, reason = strategy.risk.can_enter(
            snapshot=snap,
            decision=EntryDecision(True, "", entry_mode="taker", side=1),
            position=PositionState(),
            now_ns=2_000_000_001,
            expected_price=198.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_limit")

    def test_losing_partial_exit_enters_consecutive_loss_cooling_once(self) -> None:
        strategy = _strategy()
        strategy.risk.config = RiskConfig(
            max_spread_ticks=5.0,
            consecutive_loss_limit=1,
            cooling_seconds=120,
            fee_per_share=0.0,
            slippage_ticks_default=0.0,
        )
        self._open_position(strategy, qty=100, avg_price=100.0)

        strategy._apply_position_fill(side=-1, qty=20, price=99.0, now_ns=2_000_000_000, entry_mode="")
        first_cooling_until = strategy.risk._cooling_until_ns
        self.assertGreater(first_cooling_until, 2_000_000_000)

        strategy._apply_position_fill(side=-1, qty=20, price=98.0, now_ns=2_000_000_100, entry_mode="")
        self.assertEqual(strategy.risk._consecutive_losses, 1)
        self.assertEqual(strategy.risk._cooling_until_ns, first_cooling_until)

    def test_full_close_profit_after_partial_loss_resets_loss_cooling(self) -> None:
        strategy = _strategy()
        strategy.risk.config = RiskConfig(
            max_spread_ticks=5.0,
            consecutive_loss_limit=1,
            cooling_seconds=120,
            fee_per_share=0.0,
            slippage_ticks_default=0.0,
        )
        self._open_position(strategy, qty=100, avg_price=100.0)

        strategy._apply_position_fill(side=-1, qty=20, price=99.0, now_ns=2_000_000_000, entry_mode="")
        strategy._apply_position_fill(side=-1, qty=80, price=102.0, now_ns=2_000_000_100, entry_mode="")

        self.assertEqual(strategy.metrics.closed_trades, 1)
        self.assertEqual(strategy.metrics.win_count, 1)
        self.assertEqual(strategy.risk._consecutive_losses, 0)

    def test_full_close_after_partial_exit_counts_as_one_trade(self) -> None:
        """closed_trades increments exactly once when position is fully closed."""
        strategy = _strategy()
        self._open_position(strategy, qty=100, avg_price=100.0)

        strategy._apply_position_fill(side=-1, qty=30, price=102.0, now_ns=0, entry_mode="")  # partial
        self.assertEqual(strategy.metrics.closed_trades, 0)

        strategy._apply_position_fill(side=-1, qty=70, price=103.0, now_ns=0, entry_mode="")  # full close
        self.assertEqual(strategy.metrics.closed_trades, 1)
        self.assertEqual(strategy.position.qty, 0)


class RestoreDailyPnLTests(unittest.TestCase):
    def test_restore_daily_pnl_sets_risk_and_metrics(self) -> None:
        strategy = _strategy()
        strategy.restore_daily_pnl(pnl=-2500.0, now_ns=1_747_000_000_000_000_000)
        self.assertAlmostEqual(strategy.risk.daily_pnl, -2500.0)
        self.assertAlmostEqual(strategy.metrics.realized_pnl, -2500.0)

    def test_restore_daily_pnl_blocks_entry_when_exceeds_limit(self) -> None:
        strategy = _strategy()
        # daily_loss_limit is 5000 (set in _strategy())
        strategy.restore_daily_pnl(pnl=-5001.0, now_ns=1_747_000_000_000_000_000)
        snap = _snapshot(ts_ns=1_747_000_000_000_000_000)
        from kabu_maker_taker.models import EntryDecision, PositionState
        ok, reason = strategy.risk.can_enter(
            snapshot=snap,
            decision=EntryDecision(True, "", entry_mode="taker", side=1),
            position=PositionState(),
            now_ns=1_747_000_000_000_000_001,
            expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_limit")


if __name__ == "__main__":
    unittest.main()
