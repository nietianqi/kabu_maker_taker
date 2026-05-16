from __future__ import annotations

import json
import time
from collections.abc import Iterable

from .combined import CombinedMakerTakerStrategy
from .config import AppConfig
from .execution import KabuRestExecutor, LiveExecutionResult
from .models import BrokerFillEvent, BrokerOrderEvent, OrderIntent


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


def live_halted(strategy: CombinedMakerTakerStrategy, reason: str) -> int:
    print(
        json.dumps(
            {
                "status": "live_halted",
                "reason": reason,
                "orders": strategy.orders.snapshot(),
                "metrics": strategy.metrics.to_dict(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 3

