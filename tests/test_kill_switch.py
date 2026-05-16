"""Tests for kill-switch functionality — file-based halt and soft kill in RiskManager."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.app import _emergency_flatten_simulator
from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig
from kabu_maker_taker.live_runtime import check_kill_switch
from kabu_maker_taker.models import BoardSnapshot, BrokerOrderEvent, EntryDecision, Level, OrderIntent, PositionState
from kabu_maker_taker.risk import RiskManager
from kabu_maker_taker.simulator import DryRunSimulator
from kabu_maker_taker.strategy import ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT


def _snap(bid: float = 100.0, ask: float = 102.0) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984", ts_ns=1_000_000_000,
        bid=bid, ask=ask, bid_size=500, ask_size=200,
        bids=(Level(bid, 500),), asks=(Level(ask, 200),),
    )


def _risk() -> RiskManager:
    return RiskManager(
        config=RiskConfig(max_spread_ticks=5.0),
        tick_size=1.0,
        lot_size=100,
    )


class SoftKillRiskTests(unittest.TestCase):

    def test_soft_kill_blocks_new_entries(self) -> None:
        """set_soft_kill(True) causes can_enter() to return kill_switch_soft."""
        risk = _risk()
        decision = EntryDecision(allow=True, reason="ok", entry_mode="maker", side=1)
        pos = PositionState()

        # Without kill switch: should pass all gates
        allowed, reason = risk.can_enter(
            snapshot=_snap(), decision=decision, position=pos,
            now_ns=1_000_000_000, expected_price=102.0,
        )
        self.assertTrue(allowed)

        # Activate soft kill
        risk.set_soft_kill(True)
        allowed, reason = risk.can_enter(
            snapshot=_snap(), decision=decision, position=pos,
            now_ns=1_000_000_000, expected_price=102.0,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "kill_switch_soft")

    def test_soft_kill_deactivates_on_set_false(self) -> None:
        """set_soft_kill(False) re-enables entries after soft kill was active."""
        risk = _risk()
        decision = EntryDecision(allow=True, reason="ok", entry_mode="maker", side=1)
        pos = PositionState()

        risk.set_soft_kill(True)
        allowed, _ = risk.can_enter(
            snapshot=_snap(), decision=decision, position=pos,
            now_ns=1_000_000_000, expected_price=102.0,
        )
        self.assertFalse(allowed)

        risk.set_soft_kill(False)
        allowed, _ = risk.can_enter(
            snapshot=_snap(), decision=decision, position=pos,
            now_ns=1_000_000_000, expected_price=102.0,
        )
        self.assertTrue(allowed)

    def test_soft_kill_does_not_affect_decision_allow_false(self) -> None:
        """When decision.allow is already False, soft kill reason is not returned."""
        risk = _risk()
        risk.set_soft_kill(True)
        decision = EntryDecision(allow=False, reason="no_direction", side=0)
        pos = PositionState()

        allowed, reason = risk.can_enter(
            snapshot=_snap(), decision=decision, position=pos,
            now_ns=1_000_000_000, expected_price=102.0,
        )
        self.assertFalse(allowed)
        # reason comes from decision.reason, checked before soft-kill
        self.assertEqual(reason, "no_direction")


class CheckKillSwitchTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _config(self) -> AppConfig:
        return AppConfig(
            kill_switch_path=str(self._dir / "halt.txt"),
            kill_switch_hard_path=str(self._dir / "halt_hard.txt"),
        )

    def test_no_file_returns_empty_string(self) -> None:
        """No kill-switch files → check_kill_switch returns ''."""
        config = self._config()
        self.assertEqual(check_kill_switch(config), "")

    def test_soft_file_returns_soft(self) -> None:
        """Creating halt.txt → check_kill_switch returns 'soft'."""
        config = self._config()
        (self._dir / "halt.txt").touch()
        self.assertEqual(check_kill_switch(config), "soft")

    def test_hard_file_returns_hard(self) -> None:
        """Creating halt_hard.txt → check_kill_switch returns 'hard'."""
        config = self._config()
        (self._dir / "halt_hard.txt").touch()
        self.assertEqual(check_kill_switch(config), "hard")

    def test_hard_takes_priority_over_soft(self) -> None:
        """When both files exist, 'hard' is returned."""
        config = self._config()
        (self._dir / "halt.txt").touch()
        (self._dir / "halt_hard.txt").touch()
        self.assertEqual(check_kill_switch(config), "hard")

    def test_deleting_soft_file_clears_kill_switch(self) -> None:
        """After the halt.txt is removed, returns '' again."""
        config = self._config()
        halt = self._dir / "halt.txt"
        halt.touch()
        self.assertEqual(check_kill_switch(config), "soft")
        halt.unlink()
        self.assertEqual(check_kill_switch(config), "")


def _make_snap(bid: float = 100.0, ask: float = 102.0, ts_ns: int = 1_000_000_000) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984", ts_ns=ts_ns,
        bid=bid, ask=ask, bid_size=500, ask_size=200,
        bids=(Level(bid, 500),), asks=(Level(ask, 200),),
    )


def _make_config() -> AppConfig:
    return AppConfig(
        symbol="9984",
        exchange=27,
        tick_size=1.0,
        lot_size=100,
        lollipop=LollipopConfig(
            maker_tp_ticks=2.0,
            taker_tp_ticks=3.0,
            tp_delay_ms=0,
            maker_max_hold_seconds=300,
            taker_max_hold_seconds=300,
            max_retries=3,
        ),
    )


class DryRunHaltTests(unittest.TestCase):
    """_emergency_flatten_simulator() cancels orders and closes open positions."""

    def test_dry_run_halt_cancels_working_entry_order(self) -> None:
        """After _emergency_flatten_simulator(), working entry orders are cancelled."""
        config = _make_config()
        strategy = CombinedMakerTakerStrategy(config)
        simulator = DryRunSimulator(tick_size=1.0, slippage_ticks=0)
        snapshot = _make_snap()
        now_ns = snapshot.ts_ns

        # Register a working limit entry order in both strategy ledger AND simulator
        intent = OrderIntent(
            symbol="9984", exchange=27, side=1, qty=100,
            price=100.0, is_market=False, strategy="maker",
            reason="test", score=0, reference_price=100.0,
        )
        tracked = strategy.orders.add_intent(intent, role=ORDER_ROLE_ENTRY, now_ns=now_ns)
        # Submit to simulator so it registers the order in its internal state
        for ev in simulator.submit(tracked.intent, snapshot, now_ns):
            if isinstance(ev, BrokerOrderEvent):
                strategy.on_broker_order_event(ev)

        self.assertTrue(len(list(strategy.working_entry_ids)) > 0)

        _emergency_flatten_simulator(strategy, simulator, snapshot, now_ns)

        self.assertEqual(list(strategy.working_entry_ids), [])

    def test_dry_run_halt_closes_open_position(self) -> None:
        """After _emergency_flatten_simulator(), an open position receives a market exit."""
        config = _make_config()
        strategy = CombinedMakerTakerStrategy(config)
        simulator = DryRunSimulator(tick_size=1.0, slippage_ticks=0)
        snapshot = _make_snap()
        now_ns = snapshot.ts_ns

        # Set up an open long position directly
        strategy.position.side = 1
        strategy.position.qty = 100
        strategy.position.avg_price = 100.0
        strategy.lollipop.on_entry_fill(100.0, "maker", now_ns=now_ns, entry_side=1)

        _emergency_flatten_simulator(strategy, simulator, snapshot, now_ns)

        # The simulator should have received a market sell — position should be flat
        self.assertEqual(strategy.position.qty, 0)


if __name__ == "__main__":
    unittest.main()
