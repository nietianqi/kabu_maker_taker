from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .broker import JsonBrokerSnapshotAdapter
from .combined import CombinedMakerTakerStrategy
from .config import AppConfig, load_config
from .execution import KabuApiError, KabuRestExecutor
from .live_runtime import (
    handle_live_execution as _handle_live_execution,
    live_halted as _live_halted,
    poll_live as _poll_live,
    sleep_before_live_poll as _sleep_before_live_poll,
    submit_to_live as _submit_to_live,
)
from .models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, OrderIntent, TradePrint
from .simulator import DryRunSimulator
from .strategy import ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run combined maker/taker strategy in dry-run intent mode.")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--events", help="JSONL file containing board/trade events.")
    parser.add_argument(
        "--broker-snapshot",
        help="JSON file containing read-only broker reconciliation state.",
    )
    parser.add_argument("--sample", action="store_true", help="Run an embedded sample event sequence.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send intents to kabu Station REST instead of dry-run simulation.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.live and args.sample:
        parser.error("--live cannot be used with --sample")
    if args.live and config.dry_run:
        parser.error("--live requires config.dry_run=false")
    if args.live and config.risk.api_error_limit <= 0:
        parser.error("--live requires risk.api_error_limit > 0")
    if args.live and args.broker_snapshot:
        parser.error("--broker-snapshot cannot be combined with --live")

    strategy = CombinedMakerTakerStrategy(config)
    simulator = DryRunSimulator(
        tick_size=config.tick_size,
        slippage_ticks=config.risk.slippage_ticks_default,
    )
    live_executor: KabuRestExecutor | None = None
    if args.live:
        live_executor = KabuRestExecutor(config)
        try:
            live_executor.start()
            broker_snapshot = live_executor.snapshot()
        except KabuApiError as exc:
            print(
                json.dumps(
                    {"status": "live_start_failed", "reason": str(exc)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return 2
        summary = strategy.reconcile_from_broker(
            broker_snapshot,
            now_ns=broker_snapshot.ts_ns or time.time_ns(),
        )
        print(
            json.dumps(
                {"status": "live_reconciled", "summary": summary},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    if args.broker_snapshot:
        broker_snapshot = JsonBrokerSnapshotAdapter(args.broker_snapshot).snapshot()
        summary = strategy.reconcile_from_broker(
            broker_snapshot,
            now_ns=broker_snapshot.ts_ns or time.time_ns(),
        )
        print(
            json.dumps(
                {"status": "reconciled", "summary": summary},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    if args.events:
        events = _read_jsonl(Path(args.events))
    elif args.sample:
        events = _sample_events(config)
    else:
        print(json.dumps({"status": "no_events", "hint": "use --sample or --events events.jsonl"}))
        return 0

    for event in events:
        event_type = str(event.get("type", "board"))
        if event_type == "trade":
            tp = TradePrint.from_dict(event)
            if live_executor is None:
                simulator.on_trade(tp, tp.ts_ns)
            strategy.on_trade(tp)
            continue
        snapshot = BoardSnapshot.from_dict(
            event,
            kabu_bidask_reversed=config.signals.kabu_bidask_reversed,
            auto_fix_negative_spread=config.signals.auto_fix_negative_spread,
        )
        if live_executor is None:
            for fill_event in simulator.on_board(snapshot, snapshot.ts_ns):
                strategy.on_broker_fill(fill_event)
        else:
            halt_reason = _poll_live(strategy, live_executor, now_ns=snapshot.ts_ns)
            if halt_reason:
                return _live_halted(strategy, halt_reason)
        result = strategy.on_board(snapshot, now_ns=snapshot.ts_ns)
        if result.entry_cancel_signal:
            for oid in strategy.working_entry_ids:
                order = strategy.request_cancel(oid, reason=result.entry_cancel_signal, now_ns=snapshot.ts_ns)
                if live_executor is None:
                    for cancel_event in simulator.cancel(oid, snapshot.ts_ns):
                        strategy.on_broker_order_event(cancel_event)
                elif order is not None:
                    halt_reason = _handle_live_execution(
                        strategy,
                        live_executor.cancel(order, now_ns=snapshot.ts_ns),
                        now_ns=snapshot.ts_ns,
                    )
                    if halt_reason:
                        return _live_halted(strategy, halt_reason)
                    _sleep_before_live_poll(config)
                    halt_reason = _poll_live(strategy, live_executor, now_ns=snapshot.ts_ns)
                    if halt_reason:
                        return _live_halted(strategy, halt_reason)
        if result.intent is not None:
            print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
            if live_executor is None:
                _submit_to_simulator(strategy, simulator, result.intent, snapshot, snapshot.ts_ns)
            else:
                halt_reason = _submit_to_live(
                    strategy,
                    live_executor,
                    result.intent,
                    ORDER_ROLE_ENTRY,
                    snapshot.ts_ns,
                    config,
                )
                if halt_reason:
                    return _live_halted(strategy, halt_reason)
        if result.exit_intent is not None:
            if live_executor is None:
                _submit_to_simulator(strategy, simulator, result.exit_intent, snapshot, snapshot.ts_ns)
            else:
                halt_reason = _submit_to_live(
                    strategy,
                    live_executor,
                    result.exit_intent,
                    ORDER_ROLE_EXIT,
                    snapshot.ts_ns,
                    config,
                )
                if halt_reason:
                    return _live_halted(strategy, halt_reason)

    final = strategy.last_result.to_dict() if strategy.last_result else {}
    print(
        json.dumps(
            {"status": "done", "last": final, "metrics": strategy.metrics.to_dict()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


def _submit_to_simulator(
    strategy: CombinedMakerTakerStrategy,
    simulator: DryRunSimulator,
    intent: OrderIntent,
    snapshot: BoardSnapshot,
    now_ns: int,
) -> None:
    for event in simulator.submit(intent, snapshot, now_ns):
        if isinstance(event, BrokerOrderEvent):
            strategy.on_broker_order_event(event)
        elif isinstance(event, BrokerFillEvent):
            strategy.on_broker_fill(event)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _sample_events(config: AppConfig) -> list[dict[str, Any]]:
    base = time.time_ns()
    symbol = config.symbol
    return [
        {"type": "trade", "symbol": symbol, "ts_ns": base, "price": 100.8, "size": 300, "side": 1},
        {
            "type": "board",
            "symbol": symbol,
            "exchange": config.exchange,
            "ts_ns": base + 100_000_000,
            "bid": 100.0,
            "ask": 101.0,
            "bid_size": 900,
            "ask_size": 250,
            "bids": [{"price": 100.0, "size": 900}, {"price": 99.0, "size": 500}],
            "asks": [{"price": 101.0, "size": 250}, {"price": 102.0, "size": 300}],
        },
        {
            "type": "trade",
            "symbol": symbol,
            "ts_ns": base + 150_000_000,
            "price": 101.0,
            "size": 500,
            "side": 1,
        },
        {
            "type": "board",
            "symbol": symbol,
            "exchange": config.exchange,
            "ts_ns": base + 200_000_000,
            "bid": 101.0,
            "ask": 102.0,
            "bid_size": 1200,
            "ask_size": 180,
            "bids": [{"price": 101.0, "size": 1200}, {"price": 100.0, "size": 700}],
            "asks": [{"price": 102.0, "size": 180}, {"price": 103.0, "size": 220}],
        },
    ]


if __name__ == "__main__":
    raise SystemExit(main())
