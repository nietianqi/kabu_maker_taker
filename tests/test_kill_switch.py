"""Tests for kill-switch functionality — file-based halt and soft kill in RiskManager."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.config import AppConfig, RiskConfig
from kabu_maker_taker.live_runtime import check_kill_switch
from kabu_maker_taker.models import BoardSnapshot, EntryDecision, Level, PositionState
from kabu_maker_taker.risk import RiskManager


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


if __name__ == "__main__":
    unittest.main()
