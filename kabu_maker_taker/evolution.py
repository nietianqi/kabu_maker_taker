"""Parameter evolution — grid search and walk-forward evaluation.

``grid_search()`` sweeps a ``param_grid`` over a single JSONL events file and
ranks parameter combinations by Sharpe ratio.  ``walk_forward()`` applies a
rolling train/test window over a sorted list of daily JSONL files.

Example usage::

    from kabu_maker_taker.config import load_config
    from kabu_maker_taker.evolution import grid_search

    base_config = load_config("config.example.json")
    param_grid = {
        "strategy.tape_imbalance_long": [0.10, 0.15, 0.20],
        "strategy.book_imbalance_long": [0.15, 0.20],
    }
    results = grid_search(base_config, "data/events.jsonl", param_grid)
    for r in results[:5]:
        print(r.params, r.result.sharpe, r.result.total_pnl)

Or via CLI::

    python -m kabu_maker_taker --evolve --events data/ --param-grid param_grid.json
"""
from __future__ import annotations

import dataclasses
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .replay import ReplayResult, ReplayRunner


@dataclass
class EvolutionResult:
    """One combination of parameters and its replay performance."""
    params: dict[str, Any]
    result: ReplayResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "trade_count": self.result.trade_count,
            "win_rate": self.result.win_rate,
            "avg_pnl_per_trade": self.result.avg_pnl_per_trade,
            "total_pnl": self.result.total_pnl,
            "max_drawdown": self.result.max_drawdown,
            "sharpe": self.result.sharpe,
            "fill_rate": self.result.fill_rate,
            "entry_count": self.result.entry_count,
        }


def grid_search(
    base_config: AppConfig,
    events_path: str | Path,
    param_grid: dict[str, list[Any]],
    *,
    sort_by: str = "sharpe",
) -> list[EvolutionResult]:
    """Run all combinations in *param_grid* on *events_path*.

    Args:
        base_config: Starting config; each combination overrides specific fields.
        events_path: Path to a single JSONL events file.
        param_grid: Mapping of dot-notation config key → list of values to try.
                    e.g. ``{"strategy.tape_imbalance_long": [0.10, 0.15, 0.20]}``
        sort_by: Sort results by this ``ReplayResult`` field (default: ``"sharpe"``).

    Returns:
        List of ``EvolutionResult`` sorted descending by *sort_by*.
    """
    keys = list(param_grid.keys())
    value_lists = [param_grid[k] for k in keys]
    results: list[EvolutionResult] = []

    for combo in itertools.product(*value_lists):
        params = dict(zip(keys, combo))
        config = base_config
        for key, value in params.items():
            config = _set_nested(config, key, value)
        replay = ReplayRunner(config)
        result = replay.run(events_path)
        results.append(EvolutionResult(params=params, result=result))

    results.sort(key=lambda r: getattr(r.result, sort_by, 0.0), reverse=True)
    return results


def walk_forward(
    base_config: AppConfig,
    events_files: list[str | Path],
    param_grid: dict[str, list[Any]],
    *,
    train_days: int = 5,
    test_days: int = 1,
    sort_by: str = "sharpe",
) -> list[dict[str, Any]]:
    """Rolling walk-forward: train on *train_days*, test on *test_days*.

    Args:
        base_config: Starting config.
        events_files: Daily JSONL files sorted oldest-first.
        param_grid: Same as ``grid_search``.
        train_days: Number of training files per window.
        test_days: Number of out-of-sample test files per window.
        sort_by: Metric used to pick best params on each training window.

    Returns:
        List of dicts with keys: ``window``, ``train_files``, ``test_files``,
        ``best_params``, ``train_result``, ``test_result``.
    """
    files = [Path(f) for f in events_files]
    step = test_days
    window_results: list[dict[str, Any]] = []

    for start in range(0, len(files) - train_days - test_days + 1, step):
        train_files = files[start: start + train_days]
        test_files = files[start + train_days: start + train_days + test_days]

        # Run grid_search independently on each training file; take the best
        # scoring params across all training files as the selected combination.
        best_params: dict[str, Any] = {}
        best_score = float("-inf")
        for train_file in train_files:
            for combo_result in grid_search(base_config, train_file, param_grid, sort_by=sort_by):
                score = getattr(combo_result.result, sort_by, 0.0)
                if score > best_score:
                    best_score = score
                    best_params = combo_result.params

        # Apply best params and test out-of-sample
        test_config = base_config
        for key, value in best_params.items():
            test_config = _set_nested(test_config, key, value)

        test_results: list[ReplayResult] = []
        for test_file in test_files:
            test_results.append(ReplayRunner(test_config).run(test_file))

        # Aggregate test results
        total_pnl = sum(r.total_pnl for r in test_results)
        total_trades = sum(r.trade_count for r in test_results)
        avg_sharpe = (
            sum(r.sharpe for r in test_results) / len(test_results) if test_results else 0.0
        )

        window_results.append({
            "window": start,
            "train_files": [str(f) for f in train_files],
            "test_files": [str(f) for f in test_files],
            "best_params": best_params,
            "test_total_pnl": round(total_pnl, 2),
            "test_trade_count": total_trades,
            "test_avg_sharpe": round(avg_sharpe, 4),
        })

    return window_results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _set_nested(config: Any, key: str, value: Any) -> Any:
    """Return a new config with *key* (dot-notation) replaced by *value*.

    Uses ``dataclasses.replace()`` at each level so frozen dataclasses are
    handled correctly.

    Example::
        new = _set_nested(cfg, "strategy.tape_imbalance_long", 0.20)
    """
    parts = key.split(".", 1)
    if len(parts) == 1:
        return dataclasses.replace(config, **{key: value})
    sub_name, rest = parts
    sub_config = getattr(config, sub_name)
    new_sub = _set_nested(sub_config, rest, value)
    return dataclasses.replace(config, **{sub_name: new_sub})


def run_cli(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m kabu_maker_taker --evolve`` subcommand."""
    import argparse
    parser = argparse.ArgumentParser(description="Grid-search strategy parameters on JSONL event data.")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--events", required=True, help="JSONL file (or glob pattern) to replay.")
    parser.add_argument(
        "--param-grid", dest="param_grid",
        help="JSON file mapping config keys to lists of values. "
             'Example: {"strategy.tape_imbalance_long": [0.10, 0.15, 0.20]}'
    )
    parser.add_argument("--sort-by", dest="sort_by", default="sharpe",
                        help="ReplayResult field to rank by (default: sharpe).")
    parser.add_argument("--top", type=int, default=10, help="Print top N results.")
    args = parser.parse_args(argv)

    from .config import load_config
    base_config = load_config(args.config)

    if args.param_grid:
        with open(args.param_grid, encoding="utf-8") as fh:
            param_grid = json.load(fh)
    else:
        # Default grid: sweep signal thresholds
        param_grid = {
            "strategy.tape_imbalance_long": [0.08, 0.10, 0.12, 0.15],
            "strategy.book_imbalance_long": [0.15, 0.18, 0.22],
        }

    # Support glob patterns for --events (e.g. "data/*.jsonl")
    events_path = Path(args.events)
    if events_path.is_dir():
        event_files = sorted(events_path.glob("*.jsonl"))
    else:
        event_files = [events_path]

    if not event_files:
        print(json.dumps({"status": "error", "reason": f"no JSONL files found at {args.events}"}))
        return 1

    all_results: list[EvolutionResult] = []
    for ef in event_files:
        all_results.extend(grid_search(base_config, ef, param_grid, sort_by=args.sort_by))

    # Re-sort combined results
    all_results.sort(key=lambda r: getattr(r.result, args.sort_by, 0.0), reverse=True)

    for r in all_results[: args.top]:
        print(json.dumps(r.to_dict(), ensure_ascii=False, separators=(",", ":")))

    print(json.dumps({"status": "done", "combinations_evaluated": len(all_results)}))
    return 0
