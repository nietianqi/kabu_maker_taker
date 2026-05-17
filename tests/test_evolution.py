"""Tests for evolution.py — _set_nested, grid_search, and walk_forward."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.evolution import (
    EvolutionResult,
    _set_nested,
    grid_search,
    walk_forward,
)


def _config() -> AppConfig:
    return AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=100,
        strategy=StrategyConfig(trade_qty=100),
        risk=RiskConfig(
            max_spread_ticks=5.0,
            fee_per_share=0.0,
            slippage_ticks_default=0.0,
        ),
        lollipop=LollipopConfig(tp_delay_ms=0, stop_loss_ticks=0.0),
    )


def _board_event(ts_ns: int, bid: float = 100.0, ask: float = 101.0) -> dict:
    return {
        "type": "board",
        "symbol": "9984",
        "exchange": 27,
        "ts_ns": ts_ns,
        "bid": bid,
        "ask": ask,
        "bid_size": 1000,
        "ask_size": 300,
        "bids": [{"price": bid, "size": 1000}],
        "asks": [{"price": ask, "size": 300}],
    }


def _write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


class SetNestedTests(unittest.TestCase):

    def test_flat_key_replaces_field(self) -> None:
        """_set_nested with a plain key replaces the top-level field."""
        cfg = _config()
        new_cfg = _set_nested(cfg, "symbol", "1234")
        self.assertEqual(new_cfg.symbol, "1234")
        # Original unchanged (frozen dataclass)
        self.assertEqual(cfg.symbol, "9984")

    def test_nested_key_replaces_subfield(self) -> None:
        """_set_nested with dot-notation updates a nested dataclass field."""
        cfg = _config()
        original_val = cfg.strategy.tape_imbalance_long
        new_cfg = _set_nested(cfg, "strategy.tape_imbalance_long", 0.25)
        self.assertAlmostEqual(new_cfg.strategy.tape_imbalance_long, 0.25, places=4)
        # Original strategy unchanged
        self.assertAlmostEqual(cfg.strategy.tape_imbalance_long, original_val, places=4)

    def test_deep_nested_key(self) -> None:
        """_set_nested handles two-level dot-notation (e.g. lollipop.tp_delay_ms)."""
        cfg = _config()
        new_cfg = _set_nested(cfg, "lollipop.tp_delay_ms", 999)
        self.assertEqual(new_cfg.lollipop.tp_delay_ms, 999)
        self.assertEqual(cfg.lollipop.tp_delay_ms, 0)

    def test_risk_subfield(self) -> None:
        """_set_nested updates risk sub-config."""
        cfg = _config()
        new_cfg = _set_nested(cfg, "risk.max_spread_ticks", 2.5)
        self.assertAlmostEqual(new_cfg.risk.max_spread_ticks, 2.5, places=4)
        self.assertAlmostEqual(cfg.risk.max_spread_ticks, 5.0, places=4)

    def test_original_config_is_unchanged(self) -> None:
        """Frozen dataclass: original is never mutated by _set_nested."""
        cfg = _config()
        _ = _set_nested(cfg, "lot_size", 200)
        self.assertEqual(cfg.lot_size, 100)


class GridSearchTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _events_path(self, n_boards: int = 5) -> Path:
        base = 1_700_000_000_000_000_000
        events = [_board_event(base + i * 100_000_000) for i in range(n_boards)]
        path = self._dir / "events.jsonl"
        _write_events(path, events)
        return path

    def test_grid_search_returns_all_combinations(self) -> None:
        """A 2×2 grid produces exactly 4 EvolutionResult entries."""
        path = self._events_path()
        results = grid_search(
            _config(),
            path,
            {
                "strategy.tape_imbalance_long": [0.10, 0.15],
                "strategy.book_imbalance_long": [0.15, 0.20],
            },
        )
        self.assertEqual(len(results), 4)
        self.assertTrue(all(isinstance(r, EvolutionResult) for r in results))

    def test_results_sorted_descending_by_sharpe(self) -> None:
        """grid_search returns results sorted highest sharpe first."""
        path = self._events_path()
        results = grid_search(
            _config(),
            path,
            {"strategy.tape_imbalance_long": [0.05, 0.10, 0.15, 0.20]},
        )
        self.assertEqual(len(results), 4)
        sharpes = [r.result.sharpe for r in results]
        self.assertEqual(sharpes, sorted(sharpes, reverse=True))

    def test_sort_by_total_pnl(self) -> None:
        """sort_by='total_pnl' sorts by total_pnl descending."""
        path = self._events_path()
        results = grid_search(
            _config(),
            path,
            {"strategy.tape_imbalance_long": [0.05, 0.10, 0.20]},
            sort_by="total_pnl",
        )
        pnls = [r.result.total_pnl for r in results]
        self.assertEqual(pnls, sorted(pnls, reverse=True))

    def test_empty_events_file_does_not_crash(self) -> None:
        """grid_search on an empty JSONL file returns results with trade_count=0."""
        path = self._dir / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        results = grid_search(
            _config(),
            path,
            {"strategy.tape_imbalance_long": [0.10, 0.15]},
        )
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.result.trade_count, 0)

    def test_empty_param_grid_runs_base_config(self) -> None:
        """An empty param_grid ({}) runs exactly one combination (base config)."""
        path = self._events_path()
        results = grid_search(_config(), path, {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].params, {})

    def test_params_dict_matches_combination(self) -> None:
        """result.params contains the exact values used for that run."""
        path = self._events_path()
        results = grid_search(
            _config(),
            path,
            {"strategy.tape_imbalance_long": [0.10, 0.20]},
        )
        used_vals = sorted(r.params["strategy.tape_imbalance_long"] for r in results)
        self.assertAlmostEqual(used_vals[0], 0.10, places=4)
        self.assertAlmostEqual(used_vals[1], 0.20, places=4)

    def test_evolution_result_to_dict(self) -> None:
        """EvolutionResult.to_dict() contains all expected metric keys."""
        path = self._events_path()
        results = grid_search(_config(), path, {})
        d = results[0].to_dict()
        for key in ("params", "trade_count", "win_rate", "avg_pnl_per_trade",
                    "total_pnl", "max_drawdown", "sharpe", "fill_rate", "entry_count"):
            self.assertIn(key, d)


class WalkForwardTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_files(self, n: int) -> list[Path]:
        """Create n minimal JSONL event files."""
        files = []
        base = 1_700_000_000_000_000_000
        for i in range(n):
            path = self._dir / f"day_{i:02d}.jsonl"
            events = [_board_event(base + i * 86_400_000_000_000 + j * 100_000_000)
                      for j in range(3)]
            _write_events(path, events)
            files.append(path)
        return files

    def test_insufficient_files_returns_empty(self) -> None:
        """When file count < train_days + test_days, walk_forward returns []."""
        files = self._make_files(3)
        result = walk_forward(_config(), files, {}, train_days=5, test_days=1)
        self.assertEqual(result, [])

    def test_basic_window_count(self) -> None:
        """6 files, train=3, test=1 (step=1) → 3 windows."""
        files = self._make_files(6)
        windows = walk_forward(_config(), files, {}, train_days=3, test_days=1)
        self.assertEqual(len(windows), 3)

    def test_window_contains_expected_keys(self) -> None:
        """Each walk-forward window dict has all required keys."""
        files = self._make_files(5)
        windows = walk_forward(_config(), files, {}, train_days=3, test_days=1)
        for w in windows:
            for key in ("window", "train_files", "test_files", "best_params",
                        "test_total_pnl", "test_trade_count", "test_avg_sharpe"):
                self.assertIn(key, w)

    def test_train_and_test_files_disjoint(self) -> None:
        """Train and test file sets never overlap in any window."""
        files = self._make_files(7)
        windows = walk_forward(_config(), files, {}, train_days=3, test_days=1)
        for w in windows:
            train_set = set(w["train_files"])
            test_set = set(w["test_files"])
            self.assertTrue(train_set.isdisjoint(test_set))

    def test_walk_forward_with_empty_param_grid(self) -> None:
        """Empty param_grid runs the base config for each window without error."""
        files = self._make_files(4)
        windows = walk_forward(_config(), files, {}, train_days=2, test_days=1)
        self.assertEqual(len(windows), 2)
        for w in windows:
            self.assertEqual(w["best_params"], {})

    def test_corrupt_training_file_is_skipped(self) -> None:
        """A corrupt (non-JSON) JSONL file in the training set is skipped silently;
        walk_forward completes without exception and returns results from valid windows."""
        valid_files = self._make_files(3)

        # Insert a corrupt file as the second training file
        corrupt_path = self._dir / "corrupt.jsonl"
        corrupt_path.write_text("this is not valid json\n", encoding="utf-8")

        # Sequence: [corrupt, valid0, valid1] with train=1, test=1 → 2 windows
        files = [corrupt_path] + valid_files[:2]
        # Should NOT raise even though the first training file is corrupt
        windows = walk_forward(_config(), files, {}, train_days=1, test_days=1)
        # Must complete and return windows from the valid training files
        self.assertIsInstance(windows, list)


if __name__ == "__main__":
    unittest.main()
