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
from .journal import TradeJournal
from .live_runtime import (
    check_kill_switch as _check_kill_switch,
    emergency_flatten as _emergency_flatten,
    handle_live_execution as _handle_live_execution,
    live_halted as _live_halted,
    poll_live as _poll_live,
    sleep_before_live_poll as _sleep_before_live_poll,
    submit_to_live as _submit_to_live,
)
from .models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, OrderIntent, TradePrint
from .simulator import DryRunSimulator
from .strategy import ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT
from .telemetry import DecisionTraceWriter


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
    parser.add_argument(
        "--evolve",
        action="store_true",
        help="Run parameter grid search instead of single-pass replay.",
    )
    parser.add_argument(
        "--param-grid",
        dest="param_grid",
        help="JSON file with parameter grid for --evolve (e.g. {\"strategy.tape_imbalance_long\": [0.10, 0.15]}).",
    )
    args = parser.parse_args(argv)

    # Delegate to evolution CLI when --evolve is requested
    if args.evolve:
        from .evolution import run_cli as _run_evolve
        evolve_argv = ["--config", args.config]
        if args.events:
            evolve_argv += ["--events", args.events]
        if args.param_grid:
            evolve_argv += ["--param-grid", args.param_grid]
        return _run_evolve(evolve_argv)

    config = load_config(args.config)
    if args.live and args.sample:
        parser.error("--live cannot be used with --sample")
    if args.live and config.dry_run:
        parser.error("--live requires config.dry_run=false")
    if args.live and args.broker_snapshot:
        parser.error("--broker-snapshot cannot be combined with --live")
    if args.live:
        live_config_errors = _validate_live_config(config)
        if live_config_errors:
            parser.error("--live safety config incomplete: " + ", ".join(live_config_errors))
        if args.events:
            event_error = _validate_live_events_file(Path(args.events), config, now_ns=time.time_ns())
            if event_error:
                parser.error("--live --events requires fresh events: " + event_error)

    strategy = CombinedMakerTakerStrategy(config)
    simulator = DryRunSimulator(
        tick_size=config.tick_size,
        slippage_ticks=config.risk.slippage_ticks_default,
    )

    # Optional journal (trades.csv + markouts.csv)
    if config.enable_journal:
        strategy.journal = TradeJournal(
            log_dir=config.log_dir,
            symbol=config.symbol,
            tick_size=config.tick_size,
        )

    # Optional decision trace (decisions.jsonl)
    tracer = DecisionTraceWriter(
        log_dir=config.log_dir,
        symbol=config.symbol,
        enabled=config.enable_decision_trace,
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
        live_now_ns = 0
        if live_executor is not None:
            live_now_ns = time.time_ns()
            live_event_error = _live_event_freshness_error(event, config, now_ns=live_now_ns)
            if live_event_error:
                print(
                    json.dumps(
                        {"status": "live_event_rejected", "reason": live_event_error},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return 2
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
        now_ns = live_now_ns if live_executor is not None else snapshot.ts_ns

        # Kill-switch check: works in both dry-run and live modes
        ks = _check_kill_switch(config)
        if ks == "hard":
            return _halt_live(strategy, live_executor, config, snapshot, "kill_switch_hard", now_ns, simulator=simulator)
        if live_executor is not None:
            strategy.risk.set_soft_kill(ks == "soft")

        if live_executor is None:
            for fill_event in simulator.on_board(snapshot, snapshot.ts_ns):
                strategy.on_broker_fill(fill_event)
        else:
            halt_reason = _poll_live(strategy, live_executor, now_ns=now_ns)
            if halt_reason:
                return _halt_live(strategy, live_executor, config, snapshot, halt_reason, now_ns)
        result = strategy.on_board(snapshot, now_ns=now_ns)
        tracer.record(result, strategy.position, now_ns)
        if result.exit_cancel_signal:
            halt_reason = _handle_exit_cancel_signal(
                strategy,
                simulator,
                live_executor,
                result.exit_cancel_signal,
                snapshot,
                now_ns,
                config,
            )
            if halt_reason:
                return _halt_live(strategy, live_executor, config, snapshot, halt_reason, now_ns)
        if result.entry_cancel_signal:
            for oid in list(strategy.working_entry_ids):
                order = strategy.request_cancel(oid, reason=result.entry_cancel_signal, now_ns=now_ns)
                if live_executor is None:
                    for cancel_event in simulator.cancel(oid, snapshot.ts_ns):
                        strategy.on_broker_order_event(cancel_event)
                elif order is not None:
                    halt_reason = _handle_live_execution(
                        strategy,
                        live_executor.cancel(order, now_ns=now_ns),
                        now_ns=now_ns,
                    )
                    if halt_reason:
                        return _halt_live(strategy, live_executor, config, snapshot, halt_reason, now_ns)
                    _sleep_before_live_poll(config)
                    halt_reason = _poll_live(strategy, live_executor, now_ns=now_ns)
                    if halt_reason:
                        return _halt_live(strategy, live_executor, config, snapshot, halt_reason, now_ns)
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
                    now_ns,
                    config,
                )
                if halt_reason:
                    return _halt_live(strategy, live_executor, config, snapshot, halt_reason, now_ns)
        if result.exit_intent is not None:
            if live_executor is None:
                _submit_to_simulator(strategy, simulator, result.exit_intent, snapshot, snapshot.ts_ns)
            else:
                halt_reason = _submit_to_live(
                    strategy,
                    live_executor,
                    result.exit_intent,
                    ORDER_ROLE_EXIT,
                    now_ns,
                    config,
                )
                if halt_reason:
                    return _halt_live(strategy, live_executor, config, snapshot, halt_reason, now_ns)

    # Flush journal markouts and close diagnostic files
    if strategy.journal is not None:
        strategy.journal.flush()
        strategy.journal.close()
    tracer.close()

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


def _handle_exit_cancel_signal(
    strategy: CombinedMakerTakerStrategy,
    simulator: DryRunSimulator,
    live_executor: KabuRestExecutor | None,
    reason: str,
    snapshot: BoardSnapshot,
    now_ns: int,
    config: AppConfig,
) -> str:
    if live_executor is None:
        for oid in list(strategy.working_exit_ids):
            order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
            if order is not None:
                for cancel_event in simulator.cancel(oid, now_ns):
                    strategy.on_broker_order_event(cancel_event)
        deferred = strategy.release_deferred_force_exit(snapshot, now_ns=now_ns)
        if deferred is None:
            return ""
        _submit_to_simulator(strategy, simulator, deferred, snapshot, now_ns)
        return ""

    for oid in list(strategy.working_exit_ids):
        order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
        if order is None:
            continue
        halt_reason = _handle_live_execution(strategy, live_executor.cancel(order, now_ns=now_ns), now_ns=now_ns)
        if halt_reason:
            return halt_reason
    _sleep_before_live_poll(config)
    halt_reason = _poll_live(strategy, live_executor, now_ns=now_ns)
    if halt_reason:
        return halt_reason
    deferred = strategy.release_deferred_force_exit(snapshot, now_ns=now_ns)
    if deferred is None:
        return ""
    halt_reason = _submit_to_live(
        strategy,
        live_executor,
        deferred,
        ORDER_ROLE_EXIT,
        now_ns,
        config,
    )
    return halt_reason


def _emergency_flatten_simulator(
    strategy: CombinedMakerTakerStrategy,
    simulator: DryRunSimulator,
    snapshot: BoardSnapshot,
    now_ns: int,
) -> None:
    """Cancel all working orders and force-close any open position in the simulator."""
    for oid in list(strategy.working_exit_ids) + list(strategy.working_entry_ids):
        order = strategy.request_cancel(oid, reason="emergency_flatten", now_ns=now_ns)
        if order is not None:
            for ev in simulator.cancel(oid, now_ns):
                strategy.on_broker_order_event(ev)
    if strategy.position.qty > 0:
        strategy.lollipop.force_exit_next_tick()
        action = strategy.lollipop.tick(
            snapshot,
            strategy.position,
            now_ns,
            symbol=strategy.config.symbol,
            exchange=strategy.config.exchange,
        )
        if action.intent is not None:
            tracked = strategy.orders.add_intent(action.intent, role=ORDER_ROLE_EXIT, now_ns=now_ns)
            _submit_to_simulator(strategy, simulator, tracked.intent, snapshot, now_ns)


def _halt_live(
    strategy: CombinedMakerTakerStrategy,
    live_executor: KabuRestExecutor | None,
    config: AppConfig,
    snapshot: BoardSnapshot,
    reason: str,
    now_ns: int,
    *,
    simulator: DryRunSimulator | None = None,
) -> int:
    cleanup = None
    if live_executor is not None:
        cleanup = _emergency_flatten(
            strategy,
            live_executor,
            config,
            snapshot,
            now_ns=now_ns,
            reason=reason,
        )
    elif simulator is not None:
        _emergency_flatten_simulator(strategy, simulator, snapshot, now_ns)
    return _live_halted(strategy, reason, cleanup=cleanup)


def _validate_live_config(config: AppConfig) -> list[str]:
    risk = config.risk
    missing: list[str] = []
    if not risk.enforce_session:
        missing.append("risk.enforce_session=true")
    if risk.daily_loss_limit <= 0:
        missing.append("risk.daily_loss_limit>0")
    if risk.max_entry_orders_per_minute <= 0:
        missing.append("risk.max_entry_orders_per_minute>0")
    if risk.max_cancel_requests_per_minute <= 0:
        missing.append("risk.max_cancel_requests_per_minute>0")
    if risk.stale_quote_ms <= 0:
        missing.append("risk.stale_quote_ms>0")
    if risk.stale_board_ms <= 0:
        missing.append("risk.stale_board_ms>0")
    if risk.api_error_limit <= 0:
        missing.append("risk.api_error_limit>0")
    if risk.max_inventory_qty <= 0:
        missing.append("risk.max_inventory_qty>0")
    if risk.max_notional <= 0:
        missing.append("risk.max_notional>0")
    if risk.max_spread_ticks <= 0:
        missing.append("risk.max_spread_ticks>0")
    if risk.latency_breach_limit <= 0:
        missing.append("risk.latency_breach_limit>0")
    if min(risk.order_latency_limit_ms, risk.cancel_latency_limit_ms, risk.poll_latency_limit_ms) <= 0:
        missing.append("risk REST latency limits>0")
    if not config.enable_journal:
        missing.append("enable_journal=true")
    if not config.enable_decision_trace:
        missing.append("enable_decision_trace=true")
    if not config.market_state.enabled:
        missing.append("market_state.enabled=true")
    return missing


def _validate_live_events_file(path: Path, config: AppConfig, *, now_ns: int) -> str:
    seen = False
    for line_no, event in _read_jsonl_with_line(path):
        seen = True
        error = _live_event_freshness_error(event, config, now_ns=now_ns)
        if error:
            return f"{path}:{line_no}: {error}"
    if not seen:
        return f"{path}: no events"
    return ""


def _live_event_freshness_error(event: dict[str, Any], config: AppConfig, *, now_ns: int) -> str:
    raw_ts = event.get("ts_ns", event.get("timestamp_ns", event.get("ExchangeTimeNs", 0)))
    try:
        ts_ns = int(raw_ts)
    except (TypeError, ValueError):
        ts_ns = 0
    if ts_ns <= 0:
        return "missing or invalid ts_ns"
    tolerance_ns = config.risk.stale_quote_ms * 1_000_000
    if tolerance_ns <= 0:
        return "risk.stale_quote_ms must be positive"
    diff_ns = ts_ns - now_ns
    if abs(diff_ns) > tolerance_ns:
        direction = "future" if diff_ns > 0 else "stale"
        diff_ms = abs(diff_ns) / 1_000_000
        return f"{direction} event ts_ns outside risk.stale_quote_ms ({diff_ms:.0f}ms)"
    return ""


def _read_jsonl_with_line(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            yield line_no, json.loads(line)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for _, event in _read_jsonl_with_line(path):
        yield event


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
