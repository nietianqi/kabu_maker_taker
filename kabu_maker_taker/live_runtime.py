from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from .broker import BrokerOpenOrderSnapshot, BrokerReconciliationSnapshot
from .combined import CombinedMakerTakerStrategy
from .config import AppConfig, effective_register_exchange, market_data_exchange_compatible
from .execution import KabuApiError, KabuRestExecutor, LiveExecutionResult
from .models import (
    BoardSnapshot,
    BrokerFillEvent,
    BrokerOrderEvent,
    MarketState,
    OrderIntent,
    OrderState,
    OrderStatus,
    PositionState,
    _to_ns_value,
)
from .strategy import MarketStateDetector, ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT

JST = timezone(timedelta(hours=9))
PREFLIGHT_STAMP_FILENAME = "live_preflight_stamp.json"


def submit_to_live(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    intent: OrderIntent,
    role: str,
    now_ns: int,
    config: AppConfig,
) -> str:
    if role == ORDER_ROLE_EXIT:
        block_reason = loss_exit_block_reason(strategy, intent, config)
        if block_reason:
            record_loss_exit_block(strategy, intent, role=role, now_ns=now_ns, reason=block_reason)
            return ""
    halt_reason = handle_live_execution(
        strategy,
        executor.submit(intent, role=role, now_ns=now_ns),
        now_ns=now_ns,
    )
    if halt_reason:
        return halt_reason
    sleep_before_live_poll(config)
    return poll_live(strategy, executor, now_ns=now_ns)


def poll_live(strategy: CombinedMakerTakerStrategy, executor: KabuRestExecutor, *, now_ns: int) -> str:
    return handle_live_execution(
        strategy,
        executor.poll_order_events(strategy.orders.active(), now_ns=now_ns),
        now_ns=now_ns,
    )


def loss_exit_block_reason(strategy: CombinedMakerTakerStrategy, intent: OrderIntent, config: AppConfig) -> str:
    allowed, reason = strategy.risk.can_exit_without_loss(
        intent=intent,
        position=strategy.position,
        max_slip_ticks=config.strategy.max_slip_ticks,
    )
    return "" if allowed else reason


def record_loss_exit_block(
    strategy: CombinedMakerTakerStrategy,
    intent: OrderIntent,
    *,
    role: str,
    now_ns: int,
    reason: str,
) -> None:
    print(
        json.dumps(
            {
                "status": "loss_exit_blocked",
                "role": role,
                "reason": reason,
                "intent": intent.to_dict(),
                "position": {
                    "side": strategy.position.side,
                    "qty": strategy.position.qty,
                    "avg_price": strategy.position.avg_price,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    if intent.client_order_id:
        strategy.on_broker_order_event(
            BrokerOrderEvent(
                order_id=intent.client_order_id,
                status=OrderStatus.REJECTED,
                ts_ns=now_ns,
                reason=reason,
            )
        )


def handle_live_execution(
    strategy: CombinedMakerTakerStrategy,
    result: LiveExecutionResult,
    *,
    now_ns: int,
) -> str:
    apply_broker_events(strategy, result.events)
    halt_reason = ""
    if result.api_success:
        strategy.on_api_success()
    if result.api_error and strategy.on_api_error(now_ns):
        halt_reason = "api_circuit_open"
    if result.request_kind:
        if strategy.on_rest_latency(result.request_kind, result.latency_ms, now_ns):
            halt_reason = "latency_circuit_open"
    return halt_reason or result.halt_reason


def apply_broker_events(
    strategy: CombinedMakerTakerStrategy,
    events: Iterable[BrokerOrderEvent | BrokerFillEvent],
) -> None:
    for event in events:
        if isinstance(event, BrokerOrderEvent):
            strategy.on_broker_order_event(event)
        elif isinstance(event, BrokerFillEvent):
            strategy.on_broker_fill(event)


def sleep_before_live_poll(config: AppConfig) -> None:
    poll_interval_ms = max(config.kabu.poll_interval_ms, 0)
    if poll_interval_ms:
        time.sleep(poll_interval_ms / 1000)


def check_kill_switch(config: AppConfig) -> str:
    """Check for kill-switch files and return the signal level.

    Returns:
        ``"hard"``  — halt_hard.txt exists: cancel all, force-exit, stop.
        ``"soft"``  — halt.txt exists: block new entries, keep exits running.
        ``""``      — no kill-switch file found, continue normally.
    """
    if Path(config.kill_switch_hard_path).exists():
        return "hard"
    if Path(config.kill_switch_path).exists():
        return "soft"
    return ""


def live_event_freshness_error(event: dict[str, Any], config: AppConfig, *, now_ns: int) -> str:
    ts_ns = _event_ts_ns(event)
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


def run_live_preflight(
    config: AppConfig,
    executor: KabuRestExecutor,
    *,
    websocket_factory: Callable[..., object] | None = None,
) -> int:
    ok, summary = perform_live_preflight(config, executor, websocket_factory=websocket_factory)
    if not ok:
        print(json.dumps({"status": "live_preflight_failed", **summary}, ensure_ascii=False, separators=(",", ":")))
        return 2
    write_live_preflight_stamp(config, time.time_ns(), summary)
    print(json.dumps({"status": "live_preflight_ok", **summary}, ensure_ascii=False, separators=(",", ":")))
    return 0


def perform_live_preflight(
    config: AppConfig,
    executor: KabuRestExecutor,
    *,
    websocket_factory: Callable[..., object] | None = None,
) -> tuple[bool, dict[str, object]]:
    """Validate broker snapshot, PUSH registration, and fresh board messages without placing orders."""
    try:
        broker_snapshot = executor.snapshot()
    except KabuApiError as exc:
        return False, {"reason": "broker_snapshot_failed", "detail": str(exc)}
    if broker_snapshot.positions:
        return False, {
            "reason": "broker_position_not_flat",
            "positions": [
                {
                    "symbol": p.symbol,
                    "exchange": p.exchange,
                    "side": p.side,
                    "qty": p.qty,
                    "avg_price": p.avg_price,
                    "entry_mode": p.entry_mode,
                }
                for p in broker_snapshot.positions
            ],
        }
    if broker_snapshot.open_orders:
        return False, {"reason": "broker_open_orders_present"}
    ignored_summary = ignored_broker_open_orders_summary(broker_snapshot)

    required = max(config.kabu.websocket_preflight_messages, 1)
    deadline = time.monotonic() + max(config.kabu.websocket_preflight_timeout_s, 0.1)
    detector = MarketStateDetector(
        config.market_state,
        config.tick_size,
        stale_quote_ms=config.risk.stale_quote_ms,
    )
    seen = 0
    ignored_boards = 0
    stale_boards = 0
    last_stale_ms = 0.0
    last_summary: dict[str, object] = {}
    ws = None
    registered = False
    try:
        executor.register_market_data()
        registered = True
        ws = _open_websocket(
            config,
            websocket_factory=websocket_factory,
            timeout_s=max(config.kabu.websocket_preflight_timeout_s, 0.1),
        )
        while seen < required and time.monotonic() <= deadline:
            raw = ws.recv()  # type: ignore[attr-defined]
            now_ns = time.time_ns()
            event = _decode_websocket_message(raw)
            snapshot = BoardSnapshot.from_dict(
                event,
                kabu_bidask_reversed=config.signals.kabu_bidask_reversed,
                auto_fix_negative_spread=config.signals.auto_fix_negative_spread,
            )
            target_error = _snapshot_target_error(snapshot, config)
            if target_error == "symbol_mismatch":
                ignored_boards += 1
                continue
            if target_error:
                return False, {
                    "reason": target_error,
                    "received_boards": seen,
                    "ignored_boards": ignored_boards,
                    "symbol": snapshot.symbol,
                    "exchange": snapshot.exchange,
                    "trade_exchange": config.exchange,
                    "register_exchange": effective_register_exchange(
                        config.exchange,
                        config.kabu.register_exchange,
                    ),
                }
            stale_detail, stale_ms = _websocket_snapshot_stale_warning(snapshot, config, now_ns=now_ns)
            if stale_detail:
                stale_boards += 1
                last_stale_ms = stale_ms
            error = _preflight_snapshot_error(snapshot, config, now_ns=now_ns)
            if error:
                return False, {
                    "reason": error,
                    "received_boards": seen,
                    "ignored_boards": ignored_boards,
                    "stale_boards": stale_boards,
                    "last_stale_ms": last_stale_ms,
                }
            market_state = detector.update(snapshot, now_ns)
            diagnostics = detector.last_diagnostics
            if market_state == MarketState.ABNORMAL and not (
                diagnostics.reason == "stale_quote" and bool(stale_detail)
            ):
                return False, {
                    "reason": "market_state_abnormal",
                    "market_state_reason": diagnostics.reason,
                    "received_boards": seen,
                    "ignored_boards": ignored_boards,
                    "stale_boards": stale_boards,
                    "last_stale_ms": last_stale_ms,
                }
            seen += 1
            last_summary = {
                "received_boards": seen,
                "ignored_boards": ignored_boards,
                "stale_boards": stale_boards,
                "last_stale_ms": last_stale_ms,
                "symbol": snapshot.symbol,
                "exchange": snapshot.exchange,
                "trade_exchange": config.exchange,
                "register_exchange": effective_register_exchange(config.exchange, config.kabu.register_exchange),
                "bid": snapshot.bid,
                "ask": snapshot.ask,
                "market_state": market_state.value,
                "market_state_reason": diagnostics.reason,
                "ts_ns": snapshot.ts_ns,
            }
        if seen < required:
            if seen > 0:
                return True, {
                    "required_boards": required,
                    "preflight_partial": True,
                    **ignored_summary,
                    **last_summary,
                }
            return False, {
                "reason": "websocket_preflight_timeout",
                "received_boards": seen,
                "ignored_boards": ignored_boards,
                "stale_boards": stale_boards,
                "last_stale_ms": last_stale_ms,
                "required_boards": required,
            }
        return True, {"required_boards": required, **ignored_summary, **last_summary}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return False, {
            "reason": "websocket_bad_message",
            "detail": str(exc),
            "received_boards": seen,
            "ignored_boards": ignored_boards,
            "stale_boards": stale_boards,
            "last_stale_ms": last_stale_ms,
        }
    except TimeoutError as exc:
        if seen > 0:
            return True, {
                "required_boards": required,
                "preflight_partial": True,
                "detail": str(exc),
                **ignored_summary,
                **last_summary,
            }
        return False, {
            "reason": "websocket_preflight_timeout",
            "detail": str(exc),
            "received_boards": seen,
            "ignored_boards": ignored_boards,
            "stale_boards": stale_boards,
            "last_stale_ms": last_stale_ms,
            "required_boards": required,
        }
    except Exception as exc:  # websocket-client transport classes are optional imports in tests.
        return False, {
            "reason": "websocket_preflight_failed",
            "detail": str(exc),
            "received_boards": seen,
            "ignored_boards": ignored_boards,
            "stale_boards": stale_boards,
            "last_stale_ms": last_stale_ms,
        }
    finally:
        if ws is not None:
            try:
                ws.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        if registered:
            try:
                executor.unregister_market_data()
            except KabuApiError:
                pass


def validate_live_preflight_stamp(config: AppConfig, *, now_ns: int | None = None) -> str:
    now = now_ns or time.time_ns()
    path = live_preflight_stamp_path(config)
    if not path.exists():
        return f"{path}: missing preflight stamp"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"{path}: unreadable preflight stamp: {exc}"
    if str(payload.get("symbol", "")) != config.symbol or int(payload.get("exchange", 0)) != config.exchange:
        return f"{path}: symbol/exchange mismatch"
    ts_ns = int(payload.get("ts_ns", 0))
    if ts_ns <= 0:
        return f"{path}: invalid preflight timestamp"
    if ts_ns - now > config.risk.stale_quote_ms * 1_000_000:
        return f"{path}: preflight stamp is from the future"
    if _jst_date(ts_ns) != _jst_date(now):
        return f"{path}: preflight stamp is not from today"
    max_age_ns = max(config.kabu.live_preflight_max_age_minutes, 1) * 60 * 1_000_000_000
    if now - ts_ns > max_age_ns:
        age_minutes = (now - ts_ns) / 60_000_000_000
        return f"{path}: preflight stamp too old ({age_minutes:.1f}m)"
    return ""


def write_live_preflight_stamp(config: AppConfig, ts_ns: int, summary: dict[str, object] | None = None) -> Path:
    path = live_preflight_stamp_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "live_preflight_ok",
        **(summary or {}),
        "symbol": config.symbol,
        "exchange": config.exchange,
        "ts_ns": ts_ns,
        "ts_jst": datetime.fromtimestamp(ts_ns / 1e9, tz=JST).strftime("%Y-%m-%dT%H:%M:%S.%f"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def live_preflight_stamp_path(config: AppConfig) -> Path:
    return Path(config.log_dir) / PREFLIGHT_STAMP_FILENAME


def ignored_broker_open_orders_summary(snapshot: BrokerReconciliationSnapshot) -> dict[str, object]:
    if not snapshot.ignored_open_orders:
        return {}
    return {
        "ignored_broker_open_orders": [
            _ignored_open_order_payload(order)
            for order in snapshot.ignored_open_orders
        ]
    }


def run_websocket_live(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    config: AppConfig,
    tracer,
    *,
    websocket_factory: Callable[..., object] | None = None,
    shadow: bool = False,
) -> int:
    attempts = max(config.kabu.websocket_reconnect_attempts, 0) + 1
    last_snapshot = _empty_snapshot(config, time.time_ns())
    last_error = ""
    ignored_boards = 0
    for attempt in range(attempts):
        ws = None
        registered = False
        try:
            executor.register_market_data()
            registered = True
            ws = _open_websocket(config, websocket_factory=websocket_factory)
            print(
                json.dumps(
                    {"status": "live_websocket_connected", "attempt": attempt + 1},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            while True:
                raw = ws.recv()  # type: ignore[attr-defined]
                now_ns = time.time_ns()
                event = _decode_websocket_message(raw)
                snapshot = BoardSnapshot.from_dict(
                    event,
                    kabu_bidask_reversed=config.signals.kabu_bidask_reversed,
                    auto_fix_negative_spread=config.signals.auto_fix_negative_spread,
                )
                target_error = _snapshot_target_error(snapshot, config)
                if target_error == "symbol_mismatch":
                    ignored_boards += 1
                    continue
                if target_error:
                    cleanup = _fault_cleanup(
                        strategy,
                        executor,
                        config,
                        snapshot,
                        now_ns=now_ns,
                        reason=f"websocket_{target_error}",
                        shadow=shadow,
                        detail=(
                            f"snapshot_exchange={snapshot.exchange} "
                            f"trade_exchange={config.exchange} "
                            f"register_exchange={effective_register_exchange(config.exchange, config.kabu.register_exchange)}"
                        ),
                    )
                    cleanup["ignored_boards"] = ignored_boards
                    return live_halted(strategy, f"websocket_{target_error}", cleanup=cleanup)
                last_snapshot = snapshot
                freshness_error = _websocket_snapshot_fatal_time_error(snapshot, config, now_ns=now_ns)
                if freshness_error:
                    reason = f"websocket_{_reason_token(freshness_error)}"
                    cleanup = _fault_cleanup(
                        strategy,
                        executor,
                        config,
                        snapshot,
                        now_ns=now_ns,
                        reason=reason,
                        shadow=shadow,
                        detail=freshness_error,
                    )
                    return live_halted(strategy, reason, cleanup=cleanup)
                halt_reason = process_live_board(strategy, executor, config, tracer, snapshot, now_ns, shadow=shadow)
                if halt_reason:
                    return live_halted(
                        strategy,
                        halt_reason,
                        cleanup=_fault_cleanup(
                            strategy,
                            executor,
                            config,
                            snapshot,
                            now_ns=now_ns,
                            reason=halt_reason,
                            shadow=shadow,
                        ),
                    )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            cleanup = _fault_cleanup(
                strategy,
                executor,
                config,
                last_snapshot,
                now_ns=time.time_ns(),
                reason="websocket_bad_message",
                shadow=shadow,
                detail=str(exc),
            )
            return live_halted(strategy, "websocket_bad_message", cleanup=cleanup)
        except Exception as exc:  # websocket-client raises several transport-specific exception classes.
            last_error = str(exc)
            if _has_live_exposure(strategy):
                return live_halted(
                    strategy,
                    "websocket_disconnected",
                    cleanup=_fault_cleanup(
                        strategy,
                        executor,
                        config,
                        last_snapshot,
                        now_ns=time.time_ns(),
                        reason="websocket_disconnected",
                        shadow=shadow,
                    ),
                )
            if attempt >= attempts - 1:
                return live_halted(
                    strategy,
                    "websocket_reconnect_exhausted",
                    cleanup={"last_error": last_error, "ignored_boards": ignored_boards},
                )
            reconnect_error = _reconcile_before_reconnect(strategy, executor)
            if reconnect_error:
                cleanup = _fault_cleanup(
                    strategy,
                    executor,
                    config,
                    last_snapshot,
                    now_ns=time.time_ns(),
                    reason=reconnect_error,
                    shadow=shadow,
                )
                cleanup["last_error"] = last_error
                return live_halted(strategy, reconnect_error, cleanup=cleanup)
            if _has_live_exposure(strategy):
                return live_halted(
                    strategy,
                    "websocket_disconnected_with_exposure",
                    cleanup=_fault_cleanup(
                        strategy,
                        executor,
                        config,
                        last_snapshot,
                        now_ns=time.time_ns(),
                        reason="websocket_disconnected_with_exposure",
                        shadow=shadow,
                    ),
                )
            print(
                json.dumps(
                    {"status": "live_websocket_reconnect", "attempt": attempt + 2, "reason": last_error},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        finally:
            if ws is not None:
                try:
                    ws.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
            if registered:
                try:
                    executor.unregister_market_data()
                except KabuApiError:
                    pass
    return live_halted(
        strategy,
        "websocket_reconnect_exhausted",
        cleanup={"last_error": last_error, "ignored_boards": ignored_boards},
    )


def process_live_board(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    config: AppConfig,
    tracer,
    snapshot: BoardSnapshot,
    now_ns: int,
    *,
    shadow: bool = False,
) -> str:
    ks = check_kill_switch(config)
    if ks == "hard":
        return "kill_switch_hard"
    strategy.risk.set_soft_kill(ks == "soft")

    halt_reason = poll_live(strategy, executor, now_ns=now_ns)
    if halt_reason:
        return halt_reason
    result = strategy.on_board(snapshot, now_ns=now_ns)
    tracer.record(result, strategy.position, now_ns)
    if result.exit_cancel_signal:
        if shadow:
            halt_reason = _handle_shadow_exit_cancel_signal(
                strategy,
                result.exit_cancel_signal,
                snapshot,
                now_ns,
            )
        else:
            halt_reason = _handle_live_exit_cancel_signal(
                strategy,
                executor,
                config,
                result.exit_cancel_signal,
                snapshot,
                now_ns,
            )
        if halt_reason:
            return halt_reason
    if result.entry_cancel_signal:
        halt_reason = (
            _handle_shadow_entry_cancel_signal(strategy, result.entry_cancel_signal, now_ns)
            if shadow
            else _handle_live_entry_cancel_signal(strategy, executor, config, result.entry_cancel_signal, now_ns)
        )
        if halt_reason:
            return halt_reason
    if result.intent is not None:
        print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
        halt_reason = (
            shadow_submit(strategy, result.intent, ORDER_ROLE_ENTRY, now_ns)
            if shadow
            else submit_to_live(strategy, executor, result.intent, ORDER_ROLE_ENTRY, now_ns, config)
        )
        if halt_reason:
            return halt_reason
    if result.exit_intent is not None:
        halt_reason = (
            shadow_submit(strategy, result.exit_intent, ORDER_ROLE_EXIT, now_ns)
            if shadow
            else submit_to_live(strategy, executor, result.exit_intent, ORDER_ROLE_EXIT, now_ns, config)
        )
        if halt_reason:
            return halt_reason
    return ""


def _handle_live_entry_cancel_signal(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    config: AppConfig,
    reason: str,
    now_ns: int,
) -> str:
    for oid in list(strategy.working_entry_ids):
        order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
        if order is None:
            continue
        halt_reason = handle_live_execution(strategy, executor.cancel(order, now_ns=now_ns), now_ns=now_ns)
        if halt_reason:
            return halt_reason
        sleep_before_live_poll(config)
        halt_reason = poll_live(strategy, executor, now_ns=now_ns)
        if halt_reason:
            return halt_reason
    return ""


def _handle_live_exit_cancel_signal(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    config: AppConfig,
    reason: str,
    snapshot: BoardSnapshot,
    now_ns: int,
) -> str:
    for oid in list(strategy.working_exit_ids):
        order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
        if order is None:
            continue
        halt_reason = handle_live_execution(strategy, executor.cancel(order, now_ns=now_ns), now_ns=now_ns)
        if halt_reason:
            return halt_reason
    sleep_before_live_poll(config)
    halt_reason = poll_live(strategy, executor, now_ns=now_ns)
    if halt_reason:
        return halt_reason
    deferred = strategy.release_deferred_force_exit(snapshot, now_ns=now_ns)
    if deferred is None:
        return ""
    return submit_to_live(strategy, executor, deferred, ORDER_ROLE_EXIT, now_ns, config)


def shadow_submit(strategy: CombinedMakerTakerStrategy, intent: OrderIntent, role: str, now_ns: int) -> str:
    """Record a would-submit event and finalize the local order without touching the broker."""
    if role == ORDER_ROLE_EXIT:
        block_reason = loss_exit_block_reason(strategy, intent, strategy.config)
        if block_reason:
            record_loss_exit_block(strategy, intent, role=role, now_ns=now_ns, reason=block_reason)
            return ""
    print(
        json.dumps(
            {"status": "shadow_would_submit", "role": role, "intent": intent.to_dict()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    strategy.on_broker_order_event(
        BrokerOrderEvent(
            order_id=intent.client_order_id,
            status=OrderStatus.REJECTED,
            ts_ns=now_ns,
            reason="shadow_not_sent",
        )
    )
    return ""


def _handle_shadow_entry_cancel_signal(
    strategy: CombinedMakerTakerStrategy,
    reason: str,
    now_ns: int,
) -> str:
    for oid in list(strategy.working_entry_ids):
        order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
        if order is None:
            continue
        _shadow_cancel_order(strategy, order, reason, now_ns)
    return ""


def _handle_shadow_exit_cancel_signal(
    strategy: CombinedMakerTakerStrategy,
    reason: str,
    snapshot: BoardSnapshot,
    now_ns: int,
) -> str:
    for oid in list(strategy.working_exit_ids):
        order = strategy.request_cancel(oid, reason=reason, now_ns=now_ns)
        if order is None:
            continue
        _shadow_cancel_order(strategy, order, reason, now_ns)
    deferred = strategy.release_deferred_force_exit(snapshot, now_ns=now_ns)
    if deferred is not None:
        return shadow_submit(strategy, deferred, ORDER_ROLE_EXIT, now_ns)
    return ""


def _shadow_cancel_order(
    strategy: CombinedMakerTakerStrategy,
    order: OrderState,
    reason: str,
    now_ns: int,
) -> None:
    print(
        json.dumps(
            {
                "status": "shadow_would_cancel",
                "order_id": order.client_order_id,
                "role": order.role,
                "reason": reason,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    strategy.on_broker_order_event(
        BrokerOrderEvent(
            order_id=order.client_order_id,
            status=OrderStatus.CANCELED,
            ts_ns=now_ns,
            reason=f"shadow_cancel:{reason}",
        )
    )


def _open_websocket(
    config: AppConfig,
    *,
    websocket_factory: Callable[..., object] | None = None,
    timeout_s: float | None = None,
) -> object:
    factory = websocket_factory
    if factory is None:
        import websocket

        factory = websocket.create_connection
    resolved_timeout_s = timeout_s if timeout_s is not None else _websocket_recv_timeout_s(config)
    return factory(_websocket_url(config), timeout=max(resolved_timeout_s, 0.1))


def _websocket_url(config: AppConfig) -> str:
    if config.kabu.websocket_url:
        return config.kabu.websocket_url
    parsed = urlparse(config.kabu.base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/kabusapi/websocket", "", "", ""))


def _decode_websocket_message(raw: object) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        payload = json.loads(raw)
    else:
        payload = raw
    if not isinstance(payload, dict):
        raise ValueError("websocket message must decode to an object")
    return payload


def _event_ts_ns(event: dict[str, Any]) -> int:
    values = [
        event.get("ts_ns"),
        event.get("timestamp_ns"),
        event.get("ExchangeTimeNs"),
        event.get("ExchangeTime"),
        event.get("BidTimeNs"),
        event.get("BidTime"),
        event.get("AskTimeNs"),
        event.get("AskTime"),
        event.get("CurrentPriceTimeNs"),
        event.get("CurrentPriceTime"),
    ]
    return max((_to_ns_value(value) for value in values), default=0)


def _reason_token(reason: str) -> str:
    token = reason.split("(", 1)[0].strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in token).strip("_") or "unknown"


def _live_snapshot_freshness_error(snapshot: BoardSnapshot, config: AppConfig, *, now_ns: int) -> str:
    return live_event_freshness_error({"ts_ns": snapshot.ts_ns}, config, now_ns=now_ns)


def _websocket_snapshot_fatal_time_error(snapshot: BoardSnapshot, config: AppConfig, *, now_ns: int) -> str:
    status, diff_ms = _websocket_snapshot_time_status(snapshot, config, now_ns=now_ns)
    if status == "missing":
        return "missing or invalid ts_ns"
    if status == "invalid_config":
        return "risk.stale_quote_ms must be positive"
    if status == "future":
        return f"future event ts_ns outside risk.stale_quote_ms ({diff_ms:.0f}ms)"
    return ""


def _websocket_snapshot_stale_warning(
    snapshot: BoardSnapshot,
    config: AppConfig,
    *,
    now_ns: int,
) -> tuple[str, float]:
    status, diff_ms = _websocket_snapshot_time_status(snapshot, config, now_ns=now_ns)
    if status == "stale":
        return f"stale event ts_ns outside risk.stale_quote_ms ({diff_ms:.0f}ms)", diff_ms
    return "", 0.0


def _websocket_snapshot_time_status(
    snapshot: BoardSnapshot,
    config: AppConfig,
    *,
    now_ns: int,
) -> tuple[str, float]:
    if snapshot.ts_ns <= 0:
        return "missing", 0.0
    tolerance_ns = config.risk.stale_quote_ms * 1_000_000
    if tolerance_ns <= 0:
        return "invalid_config", 0.0
    diff_ns = snapshot.ts_ns - now_ns
    if diff_ns > tolerance_ns:
        return "future", abs(diff_ns) / 1_000_000
    if -diff_ns > tolerance_ns:
        return "stale", abs(diff_ns) / 1_000_000
    return "fresh", abs(diff_ns) / 1_000_000


def _websocket_recv_timeout_s(config: AppConfig) -> float:
    board_timeout_ms = config.risk.stale_board_ms if config.risk.stale_board_ms > 0 else 0
    quote_timeout_ms = config.risk.stale_quote_ms if config.risk.stale_quote_ms > 0 else 0
    return max(board_timeout_ms, quote_timeout_ms, 1000) / 1000.0


def _empty_snapshot(config: AppConfig, ts_ns: int) -> BoardSnapshot:
    return BoardSnapshot(
        symbol=config.symbol,
        exchange=config.exchange,
        ts_ns=ts_ns,
        bid=0.0,
        ask=0.0,
        bid_size=0,
        ask_size=0,
    )


def _preflight_snapshot_error(snapshot: BoardSnapshot, config: AppConfig, *, now_ns: int) -> str:
    freshness_error = _websocket_snapshot_fatal_time_error(snapshot, config, now_ns=now_ns)
    if freshness_error:
        return f"websocket_{_reason_token(freshness_error)}"
    if not snapshot.valid:
        return "invalid_quote"
    if snapshot.spread > config.risk.max_spread_ticks * config.tick_size:
        return "spread_too_wide"
    return ""


def _snapshot_target_error(snapshot: BoardSnapshot, config: AppConfig) -> str:
    if snapshot.symbol != config.symbol:
        return "symbol_mismatch"
    if not market_data_exchange_compatible(config.exchange, snapshot.exchange):
        return "exchange_mismatch"
    return ""


def _fault_cleanup(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    config: AppConfig,
    snapshot: BoardSnapshot,
    *,
    now_ns: int,
    reason: str,
    shadow: bool,
    detail: str = "",
) -> dict[str, object]:
    if shadow:
        return _shadow_cleanup(strategy, reason, now_ns, detail=detail)
    if _has_live_exposure(strategy):
        return emergency_flatten(strategy, executor, config, snapshot, now_ns=now_ns, reason=reason)
    return {"detail": detail} if detail else {"cleanup_status": "not_required"}


def _shadow_cleanup(
    strategy: CombinedMakerTakerStrategy,
    reason: str,
    now_ns: int,
    *,
    detail: str = "",
) -> dict[str, object]:
    for order in list(strategy.orders.active()):
        _shadow_cancel_order(strategy, order, reason, now_ns)
    cleanup: dict[str, object] = {
        "cleanup_status": "shadow_noop",
        "reason": reason,
        "live_orders_sent": 0,
        "active_orders_after_cleanup": len(strategy.orders.active()),
        "position_qty": strategy.position.qty,
    }
    if detail:
        cleanup["detail"] = detail
    return cleanup


def _jst_date(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=JST).strftime("%Y-%m-%d")


def _ignored_open_order_payload(order: BrokerOpenOrderSnapshot) -> dict[str, object]:
    return {
        "order_id": order.broker_order_id,
        "symbol": order.symbol,
        "exchange": order.exchange,
        "side": order.side,
        "qty": order.qty,
        "price": order.price,
        "status": order.status.value if isinstance(order.status, OrderStatus) else str(order.status),
    }


def _has_live_exposure(strategy: CombinedMakerTakerStrategy) -> bool:
    return strategy.position.qty > 0 or bool(strategy.orders.active())


def _reconcile_before_reconnect(strategy: CombinedMakerTakerStrategy, executor: KabuRestExecutor) -> str:
    try:
        snapshot = executor.snapshot()
    except KabuApiError as exc:
        return f"websocket_reconnect_snapshot_failed:{exc}"
    strategy.reconcile_from_broker(snapshot, now_ns=snapshot.ts_ns or time.time_ns())
    return ""


def emergency_flatten(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    config: AppConfig,
    snapshot,
    *,
    now_ns: int,
    reason: str,
) -> dict[str, object]:
    cleanup: dict[str, object] = {
        "cleanup_status": "completed",
        "reason": reason,
        "cancel_attempts": 0,
        "flatten_attempts": 0,
        "unresolved_order_ids": [],
        "local_unknown_order_ids": [],
        "unresolved_position": None,
        "flatten_blocked_reason": "",
        "errors": [],
    }
    active_orders = list(strategy.orders.active())
    local_unresolved: list[str] = []
    for order in active_orders:
        cleanup["cancel_attempts"] = int(cleanup["cancel_attempts"]) + 1
        strategy.request_cancel(order.client_order_id, reason=reason, now_ns=now_ns)
        if not order.broker_order_id:
            local_unresolved.append(order.client_order_id)
            continue
        try:
            result = executor.cancel(order, now_ns=now_ns)
            halt_reason = handle_live_execution(strategy, result, now_ns=now_ns)
            if halt_reason:
                cleanup["errors"].append(halt_reason)
        except KabuApiError as exc:
            cleanup["errors"].append(str(exc))

    known_broker_ids = {order.broker_order_id for order in active_orders if order.broker_order_id}
    attempted_unknown_cancels: set[str] = set()
    try:
        open_orders = ()
        for _ in range(3):
            open_orders = executor.open_order_snapshots()
            for broker_order in open_orders:
                if broker_order.order_id in known_broker_ids or broker_order.order_id in attempted_unknown_cancels:
                    continue
                attempted_unknown_cancels.add(broker_order.order_id)
                cleanup["cancel_attempts"] = int(cleanup["cancel_attempts"]) + 1
                result = executor.cancel(_order_state_from_broker_snapshot(broker_order, config, reason), now_ns=now_ns)
                halt_reason = handle_live_execution(strategy, result, now_ns=now_ns)
                if halt_reason:
                    cleanup["errors"].append(halt_reason)
            if not open_orders:
                break
            sleep_before_live_poll(config)
            poll_halt = poll_live(strategy, executor, now_ns=now_ns)
            if poll_halt:
                cleanup["errors"].append(poll_halt)
        open_orders = executor.open_order_snapshots()
    except KabuApiError as exc:
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append(str(exc))
        cleanup["local_unknown_order_ids"] = sorted(set(local_unresolved))
        return cleanup

    cleanup["local_unknown_order_ids"] = sorted(set(local_unresolved))
    unresolved_order_ids = sorted(order.order_id for order in open_orders)
    cleanup["unresolved_order_ids"] = unresolved_order_ids
    if open_orders:
        cleanup["cleanup_status"] = "unresolved"
        return cleanup

    try:
        positions = executor.position_snapshot()
    except KabuApiError as exc:
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append(str(exc))
        return cleanup

    cleanup["unresolved_position"] = [
        {
            "symbol": position.symbol,
            "side": position.side,
            "qty": position.qty,
            "avg_price": position.avg_price,
            "exchange": position.exchange,
        }
        for position in positions
        if position.qty > 0
    ]
    if not cleanup["unresolved_position"]:
        return cleanup

    if len(cleanup["unresolved_position"]) != 1:
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append("ambiguous broker position snapshot")
        return cleanup

    position = positions[0]
    if position.qty <= 0 or position.side not in (-1, 1):
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append("invalid broker position snapshot")
        return cleanup

    reference_price = snapshot.bid if position.side > 0 else snapshot.ask
    if reference_price <= 0:
        reference_price = position.avg_price
    if reference_price <= 0:
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append("missing reference price for emergency flatten")
        return cleanup

    intent = OrderIntent(
        symbol=position.symbol,
        exchange=position.exchange,
        side=-position.side,
        qty=position.qty,
        price=0.0,
        is_market=True,
        strategy="emergency_flatten",
        reason="emergency_flatten",
        score=0,
        reference_price=reference_price,
    )
    exit_allowed, block_reason = strategy.risk.can_exit_without_loss(
        intent=intent,
        position=PositionState(
            side=position.side,
            qty=position.qty,
            avg_price=position.avg_price,
            entry_mode=position.entry_mode,
        ),
        max_slip_ticks=config.strategy.max_slip_ticks,
        snapshot=snapshot,
    )
    if not exit_allowed:
        cleanup["cleanup_status"] = "unresolved"
        cleanup["flatten_blocked_reason"] = block_reason
        cleanup["errors"].append(block_reason)
        return cleanup

    cleanup["flatten_attempts"] = int(cleanup["flatten_attempts"]) + 1
    try:
        tracked_order = strategy.orders.add_intent(intent, role=ORDER_ROLE_EXIT, now_ns=now_ns)
        strategy.metrics.record_exit_intent(tracked_order.intent)
        result = executor.submit(tracked_order.intent, role=ORDER_ROLE_EXIT, now_ns=now_ns)
        halt_reason = handle_live_execution(strategy, result, now_ns=now_ns)
        if halt_reason:
            cleanup["errors"].append(halt_reason)
        sleep_before_live_poll(config)
        poll_halt = poll_live(strategy, executor, now_ns=now_ns)
        if poll_halt:
            cleanup["errors"].append(poll_halt)
    except KabuApiError as exc:
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append(str(exc))
        return cleanup

    try:
        final_positions = executor.position_snapshot()
    except KabuApiError as exc:
        cleanup["cleanup_status"] = "failed"
        cleanup["errors"].append(str(exc))
        return cleanup

    cleanup["unresolved_position"] = [
        {
            "symbol": position.symbol,
            "side": position.side,
            "qty": position.qty,
            "avg_price": position.avg_price,
            "exchange": position.exchange,
        }
        for position in final_positions
        if position.qty > 0
    ]
    if cleanup["unresolved_position"]:
        cleanup["cleanup_status"] = "unresolved"
        try:
            final_open_orders = executor.open_order_snapshots()
            cleanup["unresolved_order_ids"] = sorted(
                {*(cleanup["unresolved_order_ids"]), *(order.order_id for order in final_open_orders)}
            )
        except KabuApiError as exc:
            cleanup["cleanup_status"] = "failed"
            cleanup["errors"].append(str(exc))
    return cleanup


def _order_state_from_broker_snapshot(broker_order, config: AppConfig, reason: str) -> OrderState:
    side = broker_order.side or 1
    qty = broker_order.leaves_qty or broker_order.order_qty
    intent = OrderIntent(
        symbol=broker_order.symbol or config.symbol,
        exchange=config.exchange,
        side=side,
        qty=qty,
        price=broker_order.price,
        is_market=False,
        strategy="emergency_cancel",
        reason=reason,
        score=0,
        reference_price=broker_order.price,
        client_order_id=f"broker-{broker_order.order_id}",
    )
    return OrderState(
        client_order_id=f"broker-{broker_order.order_id}",
        intent=intent,
        role=ORDER_ROLE_EXIT,
        status=OrderStatus.WORKING,
        broker_order_id=broker_order.order_id,
    )


def live_halted(strategy: CombinedMakerTakerStrategy, reason: str, *, cleanup: dict[str, object] | None = None) -> int:
    print(
        json.dumps(
            {
                "status": "live_halted",
                "reason": reason,
                "orders": strategy.orders.snapshot(),
                "metrics": strategy.metrics.to_dict(),
                **({"cleanup": cleanup} if cleanup is not None else {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 3
