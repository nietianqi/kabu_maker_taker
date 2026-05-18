from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import BrokerFillEvent, BrokerOrderEvent, OrderStatus


class KabuApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload

    def __str__(self) -> str:
        base = super().__str__()
        code = _extract_error_code(self.payload)
        message = _extract_error_message(self.payload)
        suffix = []
        if code is not None:
            suffix.append(f"code={code}")
        if message:
            suffix.append(f"message={message}")
        return f"{base} ({', '.join(suffix)})" if suffix else base


@dataclass(slots=True)
class PositionLot:
    hold_id: str
    symbol: str
    exchange: int
    side: int
    qty: int
    closable_qty: int
    price: float
    margin_trade_type: int = 0


@dataclass(frozen=True, slots=True)
class KabuFillDetail:
    trade_id: str
    qty: int
    price: float
    ts_ns: int = 0


@dataclass(frozen=True, slots=True)
class KabuOrderSnapshot:
    order_id: str
    side: int
    order_qty: int
    cum_qty: int
    leaves_qty: int
    price: float
    avg_fill_price: float
    status: OrderStatus
    fill_ts_ns: int = 0
    reason: str = ""
    fills: tuple[KabuFillDetail, ...] = field(default_factory=tuple)
    symbol: str = ""
    exchange: int = 0


@dataclass(frozen=True, slots=True)
class LiveExecutionResult:
    events: tuple[BrokerOrderEvent | BrokerFillEvent, ...] = field(default_factory=tuple)
    api_success: bool = False
    api_error: bool = False
    halt_reason: str = ""
    request_kind: str = ""
    latency_ms: float = 0.0


def _extract_error_code(payload: Any) -> int | None:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend(
            [
                payload.get("Code"),
                payload.get("ResultCode"),
                payload.get("code"),
                payload.get("result_code"),
            ]
        )
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                candidates.extend(
                    [
                        item.get("Code"),
                        item.get("ResultCode"),
                        item.get("code"),
                        item.get("result_code"),
                    ]
                )
    for value in candidates:
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_error_message(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("Message", "Result", "message", "result"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            for key in ("Message", "Result", "message", "result"):
                value = item.get(key)
                if value not in (None, ""):
                    return str(value)
    return None
