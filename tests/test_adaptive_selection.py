from __future__ import annotations

import unittest

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, EntryDecision, Level, SignalPacket
from kabu_maker_taker.strategy import ENTRY_MODE_MAKER, ENTRY_MODE_TAKER


def _snapshot(**overrides) -> BoardSnapshot:
    defaults = dict(
        symbol="9984",
        ts_ns=1_000_000_000,
        bid=100.0,
        ask=101.0,
        bid_size=500,
        ask_size=200,
        bids=(Level(100.0, 500), Level(99.0, 300)),
        asks=(Level(101.0, 200), Level(102.0, 250)),
    )
    defaults.update(overrides)
    return BoardSnapshot(**defaults)


def _signal(**overrides) -> SignalPacket:
    defaults = dict(
        ts_ns=1_000_000_000,
        obi_raw=0.35,
        lob_ofi_raw=0.20,
        tape_ofi_raw=0.20,
        micro_momentum_raw=0.10,
        microprice_tilt_raw=0.30,
        microprice=100.3,
        mid=100.5,
        obi_z=0.5,
        lob_ofi_z=0.4,
        tape_ofi_z=0.3,
        micro_momentum_z=0.2,
        microprice_tilt_z=0.1,
        composite=0.45,
        integrated_ofi=0.20,
        trade_burst_score=0.10,
    )
    defaults.update(overrides)
    return SignalPacket(**defaults)


def _strategy(**strategy_overrides) -> CombinedMakerTakerStrategy:
    config = AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=100,
        strategy=StrategyConfig(
            maker_confirm_ticks=1,
            taker_confirm_ticks=1,
            **strategy_overrides,
        ),
        risk=RiskConfig(max_spread_ticks=5.0),
    )
    return CombinedMakerTakerStrategy(config)


def _patch_candidates(
    strategy: CombinedMakerTakerStrategy,
    *,
    maker_allow: bool = True,
    taker_allow: bool = True,
    taker_trigger: str = "",
    taker_exec_quality: int = 5,
) -> None:
    strategy.maker.evaluate = lambda snapshot, signal, market_state=None: EntryDecision(
        maker_allow,
        "" if maker_allow else "maker_primary",
        entry_mode=ENTRY_MODE_MAKER if maker_allow else "",
        side=1 if maker_allow else 0,
        entry_score=8 if maker_allow else 0,
        required_confirm=1,
    )
    strategy.taker.evaluate = lambda snapshot, signal, now_ns=0: EntryDecision(
        taker_allow,
        "" if taker_allow else "taker_breakout",
        entry_mode=ENTRY_MODE_TAKER if taker_allow else "",
        side=1 if taker_allow else 0,
        entry_score=10 if taker_allow else 0,
        required_confirm=1,
    )
    strategy.taker.classify_entry_trigger = lambda snapshot, signal, direction: taker_trigger
    strategy.taker.exec_quality_score = lambda snapshot, signal, direction: taker_exec_quality


class AdaptiveEntrySelectionTests(unittest.TestCase):
    def test_adaptive_chooses_maker_when_taker_not_urgent_and_edge_is_good(self) -> None:
        strategy = _strategy()
        _patch_candidates(strategy, taker_trigger="", taker_exec_quality=5)

        selection = strategy._select_entry(_snapshot(), _signal(), 1_000_000_000)

        self.assertEqual(selection.decision.entry_mode, ENTRY_MODE_MAKER)
        self.assertEqual(selection.selection_reason, "maker_edge_better")
        self.assertEqual(selection.setup_type, "maker_passive_fair")
        self.assertTrue(selection.maker_decision.allow)
        self.assertTrue(selection.taker_decision.allow)
        self.assertGreaterEqual(selection.maker_edge_ticks, 0.25)

    def test_adaptive_chooses_taker_when_depth_breakout_is_urgent(self) -> None:
        strategy = _strategy()
        _patch_candidates(strategy, taker_trigger="depth_breakout", taker_exec_quality=9)

        selection = strategy._select_entry(_snapshot(), _signal(), 1_000_000_000)

        self.assertEqual(selection.decision.entry_mode, ENTRY_MODE_TAKER)
        self.assertEqual(selection.selection_reason, "taker_urgent")
        self.assertEqual(selection.setup_type, "taker_depth_breakout")

    def test_adaptive_chooses_taker_when_maker_edge_is_too_low(self) -> None:
        strategy = _strategy()
        _patch_candidates(strategy, taker_trigger="", taker_exec_quality=5)

        selection = strategy._select_entry(_snapshot(), _signal(composite=-10.0), 1_000_000_000)

        self.assertEqual(selection.decision.entry_mode, ENTRY_MODE_TAKER)
        self.assertEqual(selection.selection_reason, "maker_edge_too_low")
        self.assertLess(selection.maker_edge_ticks, 0.25)

    def test_taker_priority_policy_preserves_old_priority(self) -> None:
        strategy = _strategy(entry_selection_policy="taker_priority")
        _patch_candidates(strategy, taker_trigger="", taker_exec_quality=5)

        selection = strategy._select_entry(_snapshot(), _signal(), 1_000_000_000)

        self.assertEqual(selection.decision.entry_mode, ENTRY_MODE_TAKER)
        self.assertEqual(selection.selection_reason, "taker_priority")

    def test_maker_priority_policy_prefers_maker_even_when_taker_is_urgent(self) -> None:
        strategy = _strategy(entry_selection_policy="maker_priority")
        _patch_candidates(strategy, taker_trigger="depth_breakout", taker_exec_quality=9)

        selection = strategy._select_entry(_snapshot(), _signal(), 1_000_000_000)

        self.assertEqual(selection.decision.entry_mode, ENTRY_MODE_MAKER)
        self.assertEqual(selection.selection_reason, "maker_priority")

    def test_result_to_dict_contains_selection_fields(self) -> None:
        strategy = _strategy()
        _patch_candidates(strategy, taker_trigger="", taker_exec_quality=5)
        strategy.signals.on_board = lambda snapshot: _signal()

        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        payload = result.to_dict()

        self.assertEqual(result.intent.setup_type, "maker_passive_fair")
        self.assertEqual(payload["setup_type"], "maker_passive_fair")
        self.assertEqual(payload["selection_reason"], "maker_edge_better")
        self.assertTrue(payload["maker_candidate_allow"])
        self.assertTrue(payload["taker_candidate_allow"])


if __name__ == "__main__":
    unittest.main()
