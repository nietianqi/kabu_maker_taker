from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path

from .combined import CombinedMakerTakerStrategy
from .config import AppConfig
from .execution import KabuApiError, KabuRestExecutor, LiveExecutionResult
from .models import BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderState, OrderStatus
from .strategy import ORDER_ROLE_EXIT


def submit_to_live(
    strategy: CombinedMakerTakerStrategy,
    executor: KabuRestExecutor,
    intent: OrderIntent,
    role: str,
    now_ns: int,
    config: AppConfig,
) -> str:
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

    cleanup["flatten_attempts"] = int(cleanup["flatten_attempts"]) + 1
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
