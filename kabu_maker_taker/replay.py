"""Offline replay engine — drives strategy on JSONL event files without a broker.

``ReplayRunner.run()`` replays historical board/trade events through
``CombinedMakerTakerStrategy`` + ``DryRunSimulator`` and returns a
``ReplayResult`` with standard backtesting metrics:

* trade_count, win_rate, avg_pnl_per_trade, total_pnl
* max_drawdown — maximum peak-to-trough equity drawdown (JPY)
* sharpe — mean / std of per-trade PnL (0 when fewer than 2 trades)
* fill_rate — fraction of taker-entry orders that received at least one fill

Used standalone (``--events`` flag) and as the inner loop for ``evolution.py``
parameter grid search.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .combined import CombinedMakerTakerStrategy
from .config import AppConfig
from .models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, OrderIntent, TradePrint
from .simulator import DryRunSimulator


@dataclass
class ReplayResult:
    """Summary statistics from one replay run."""
    trade_count: int = 0
    win_rate: float = 0.0           # fraction of profitable full closes
    avg_pnl_per_trade: float = 0.0  # mean net PnL per round-trip (JPY)
    total_pnl: float = 0.0          # total realized net PnL (JPY)
    max_drawdown: float = 0.0       # max peak-to-trough equity swing (JPY)
    sharpe: float = 0.0             # mean(PnL) / std(PnL) per trade
    fill_rate: float = 0.0          # taker fills / taker orders submitted
    entry_count: int = 0            # total entry orders generated
    taker_entry_count: int = 0      # of which were taker (market) orders


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield parsed dicts from a JSONL file, skipping blank lines."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


class ReplayRunner:
    """Runs a fresh strategy + simulator pair on a JSONL event file.

    Each ``run()`` call is independent — a new ``CombinedMakerTakerStrategy``
    and ``DryRunSimulator`` are created, so multiple runs never share state.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(self, events_path: str | Path) -> ReplayResult:
        """Replay *events_path* and return aggregated performance statistics."""
        config = self.config
        strategy = CombinedMakerTakerStrategy(config)
        simulator = DryRunSimulator(
            tick_size=config.tick_size,
            slippage_ticks=config.risk.slippage_ticks_default,
        )

        # Track per-trade PnL by detecting increases in closed_trades counter.
        # realized_pnl accumulates all partial + full exits, so the delta
        # between consecutive full-close events gives each round-trip's PnL.
        pnl_at_close: list[float] = []   # cumulative realized_pnl after each close
        entry_count = 0
        taker_entry_count = 0

        prev_closed = 0
        running_pnl = 0.0
        peak_pnl = 0.0
        max_dd = 0.0

        for event in read_jsonl(Path(events_path)):
            event_type = str(event.get("type", "board"))

            if event_type == "trade":
                tp = TradePrint.from_dict(event)
                simulator.on_trade(tp, tp.ts_ns)
                strategy.on_trade(tp)
                continue

            snapshot = BoardSnapshot.from_dict(
                event,
                kabu_bidask_reversed=config.signals.kabu_bidask_reversed,
                auto_fix_negative_spread=config.signals.auto_fix_negative_spread,
            )

            # Simulator fills from previous board
            for fill_event in simulator.on_board(snapshot, snapshot.ts_ns):
                strategy.on_broker_fill(fill_event)

            result = strategy.on_board(snapshot, now_ns=snapshot.ts_ns)

            # Cancel active exit orders before releasing deferred force-exit, matching app.py dry-run flow.
            if result.exit_cancel_signal:
                _handle_exit_cancel_signal_sim(strategy, simulator, result.exit_cancel_signal, snapshot, snapshot.ts_ns)

            # Cancel signal
            if result.entry_cancel_signal:
                for oid in strategy.working_entry_ids:
                    order = strategy.request_cancel(oid, now_ns=snapshot.ts_ns)
                    if order is not None:
                        for ev in simulator.cancel(oid, snapshot.ts_ns):
                            strategy.on_broker_order_event(ev)

            # Submit new entry
            if result.intent is not None:
                entry_count += 1
                if result.intent.is_market:
                    taker_entry_count += 1
                _submit_to_sim(strategy, simulator, result.intent, snapshot, snapshot.ts_ns)

            # Submit exit
            if result.exit_intent is not None:
                _submit_to_sim(strategy, simulator, result.exit_intent, snapshot, snapshot.ts_ns)

            # Detect full position closes via metrics.closed_trades counter
            curr_closed = strategy.metrics.closed_trades
            if curr_closed > prev_closed:
                curr_pnl = strategy.metrics.realized_pnl
                pnl_at_close.append(curr_pnl)
                running_pnl = curr_pnl
                peak_pnl = max(peak_pnl, running_pnl)
                max_dd = max(max_dd, peak_pnl - running_pnl)
                prev_closed = curr_closed

        # --- compute per-trade PnL deltas ---
        per_trade_pnl: list[float] = []
        prev_cum = 0.0
        for cum in pnl_at_close:
            per_trade_pnl.append(cum - prev_cum)
            prev_cum = cum

        trade_count = len(per_trade_pnl)
        taker_fills = strategy.metrics.taker_fill_count
        fill_rate = taker_fills / taker_entry_count if taker_entry_count > 0 else 0.0

        if trade_count == 0:
            return ReplayResult(
                entry_count=entry_count,
                taker_entry_count=taker_entry_count,
                fill_rate=round(fill_rate, 4),
            )

        total_pnl = sum(per_trade_pnl)
        avg_pnl = total_pnl / trade_count
        win_count = sum(1 for p in per_trade_pnl if p > 0)

        if trade_count >= 2:
            mean = avg_pnl
            variance = sum((p - mean) ** 2 for p in per_trade_pnl) / (trade_count - 1)
            std = math.sqrt(variance) if variance > 0.0 else 0.0
            sharpe = mean / std if std > 0.0 else 0.0
        else:
            sharpe = 0.0

        return ReplayResult(
            trade_count=trade_count,
            win_rate=round(win_count / trade_count, 4),
            avg_pnl_per_trade=round(avg_pnl, 2),
            total_pnl=round(total_pnl, 2),
            max_drawdown=round(max_dd, 2),
            sharpe=round(sharpe, 4),
            fill_rate=round(fill_rate, 4),
            entry_count=entry_count,
            taker_entry_count=taker_entry_count,
        )


def _submit_to_sim(
    strategy: CombinedMakerTakerStrategy,
    simulator: DryRunSimulator,
    intent: OrderIntent,
    snapshot: BoardSnapshot,
    now_ns: int,
) -> None:
    for ev in simulator.submit(intent, snapshot, now_ns):
        if isinstance(ev, BrokerOrderEvent):
            strategy.on_broker_order_event(ev)
        elif isinstance(ev, BrokerFillEvent):
            strategy.on_broker_fill(ev)


def _handle_exit_cancel_signal_sim(
    strategy: CombinedMakerTakerStrategy,
    simulator: DryRunSimulator,
    reason: str,
    snapshot: BoardSnapshot,
    now_ns: int,
) -> None:
    for oid in list(strategy.working_exit_ids):
        order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
        if order is None:
            continue
        for ev in simulator.cancel(oid, now_ns):
            strategy.on_broker_order_event(ev)
    deferred = strategy.release_deferred_force_exit(snapshot, now_ns=now_ns)
    if deferred is not None:
        _submit_to_sim(strategy, simulator, deferred, snapshot, now_ns)
