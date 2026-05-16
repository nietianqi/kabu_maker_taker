from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.broker import (
    BrokerOpenOrderSnapshot,
    BrokerPositionSnapshot,
    BrokerReconciliationSnapshot,
    JsonBrokerSnapshotAdapter,
)
from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, EntryDecision, Level, LollipopPhase, MarketState, OrderStatus, SignalPacket
from kabu_maker_taker.strategy import ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT


def _signal(**overrides) -> SignalPacket:
    values = dict(
        ts_ns=1_000_000_000,
        obi_raw=0.0,
        lob_ofi_raw=0.0,
        tape_ofi_raw=0.0,
        micro_momentum_raw=0.0,
        microprice_tilt_raw=0.0,
        microprice=100.5,
        mid=100.5,
        obi_z=0.0,
        lob_ofi_z=0.0,
        tape_ofi_z=0.0,
        micro_momentum_z=0.0,
        microprice_tilt_z=0.0,
        composite=0.0,
    )
    values.update(overrides)
    return SignalPacket(**values)


def _snapshot(ts_ns: int = 1_000_000_000) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=ts_ns,
        bid=100.0,
        ask=101.0,
        bid_size=500,
        ask_size=500,
        bids=(Level(100.0, 500),),
        asks=(Level(101.0, 500),),
    )


def _strategy() -> CombinedMakerTakerStrategy:
    config = AppConfig(
        symbol="9984",
        exchange=27,
        tick_size=1.0,
        lot_size=1,
        strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1),
        risk=RiskConfig(max_spread_ticks=5.0, daily_loss_limit=1000.0),
        lollipop=LollipopConfig(tp_delay_ms=0, maker_max_hold_seconds=600, taker_max_hold_seconds=600),
    )
    strategy = CombinedMakerTakerStrategy(config)
    strategy.signals.on_board = lambda snapshot: _signal(ts_ns=snapshot.ts_ns)
    strategy._choose_decision = lambda snapshot, signal, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
        False,
        "no_entry",
    )
    return strategy


class BrokerReconciliationTests(unittest.TestCase):
    def test_restore_position_daily_pnl_and_active_entry_order(self) -> None:
        strategy = _strategy()
        snap = BrokerReconciliationSnapshot(
            ts_ns=1_000_000_000,
            daily_pnl=-250.0,
            positions=(BrokerPositionSnapshot(symbol="9984", exchange=27, side=1, qty=100, avg_price=101.0),),
            open_orders=(
                BrokerOpenOrderSnapshot(
                    symbol="9984",
                    exchange=27,
                    side=1,
                    qty=100,
                    price=100.0,
                    role=ORDER_ROLE_ENTRY,
                    strategy="maker",
                    reason="maker_passive_edge",
                    reference_price=100.5,
                    client_order_id="entry-9",
                    broker_order_id="B-ENTRY",
                    status=OrderStatus.WORKING,
                    submitted_ts_ns=999_000_000,
                ),
            ),
        )

        summary = strategy.reconcile_from_broker(snap)

        self.assertEqual(summary["positions_restored"], 1)
        self.assertEqual(summary["active_entries"], 1)
        self.assertEqual(strategy.position.qty, 100)
        self.assertAlmostEqual(strategy.risk.daily_pnl, -250.0)
        self.assertTrue(strategy.entry_order_active)
        self.assertIsNotNone(strategy.orders.get("B-ENTRY"))

    def test_restore_active_exit_does_not_submit_duplicate_lollipop_tp(self) -> None:
        strategy = _strategy()
        snap = BrokerReconciliationSnapshot(
            ts_ns=1_000_000_000,
            positions=(BrokerPositionSnapshot(symbol="9984", exchange=27, side=1, qty=100, avg_price=101.0),),
            open_orders=(
                BrokerOpenOrderSnapshot(
                    symbol="9984",
                    exchange=27,
                    side=-1,
                    qty=100,
                    price=104.0,
                    role=ORDER_ROLE_EXIT,
                    strategy="lollipop_tp",
                    reason="limit_tp",
                    reference_price=101.0,
                    client_order_id="exit-5",
                    broker_order_id="B-EXIT",
                    status=OrderStatus.WORKING,
                    submitted_ts_ns=999_000_000,
                ),
            ),
        )

        summary = strategy.reconcile_from_broker(snap)
        result = strategy.on_board(_snapshot(1_000_000_100), now_ns=1_000_000_100)

        self.assertTrue(summary["restored_active_exit"])
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.ACTIVE)
        self.assertIsNone(result.exit_intent)
        self.assertEqual(len(strategy.orders.active_by_role(ORDER_ROLE_EXIT)), 1)

    def test_restore_position_without_exit_order_starts_lollipop_management(self) -> None:
        strategy = _strategy()
        snap = BrokerReconciliationSnapshot(
            ts_ns=1_000_000_000,
            positions=(BrokerPositionSnapshot(symbol="9984", exchange=27, side=1, qty=100, avg_price=101.0),),
        )

        strategy.reconcile_from_broker(snap)
        result = strategy.on_board(_snapshot(1_000_000_001), now_ns=1_000_000_001)

        self.assertIsNotNone(result.exit_intent)
        assert result.exit_intent is not None
        self.assertEqual(result.exit_intent.strategy, "lollipop_tp")

    def test_json_adapter_and_cli_snapshot_smoke(self) -> None:
        payload = {
            "ts_ns": 1_000_000_000,
            "daily_pnl": -10.0,
            "positions": [{"symbol": "9984", "exchange": 27, "side": 1, "qty": 100, "avg_price": 101.0}],
            "open_orders": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).joinpath("snapshot.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = JsonBrokerSnapshotAdapter(path).snapshot()
            self.assertEqual(loaded.daily_pnl, -10.0)
            completed = subprocess.run(
                [sys.executable, "-m", "kabu_maker_taker.app", "--sample", "--broker-snapshot", str(path)],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=True,
            )
        self.assertIn('"status":"reconciled"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
