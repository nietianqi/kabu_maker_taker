from __future__ import annotations

import json
import math
import time
from datetime import datetime
from typing import Any

from ..models import OrderStatus
from .models import KabuFillDetail, KabuOrderSnapshot, PositionLot, _extract_error_message


def order_snapshot(raw: dict[str, Any]) -> KabuOrderSnapshot | None:
    order_id = str(raw.get("ID") or raw.get("OrderId") or "")
    if not order_id:
        return None
    order_qty = _parse_int(raw.get("OrderQty", raw.get("Qty")))
    cum_qty = _parse_int(raw.get("CumQty"))
    price = _parse_float(raw.get("Price"))
    state_code = _parse_int(raw.get("State"))
    order_state_code = _parse_int(raw.get("OrderState"))
    is_final = state_code == 5 or order_state_code == 5

    fills: list[KabuFillDetail] = []
    fill_value = 0.0
    fill_qty = 0
    latest_fill_ts_ns = 0
    details = raw.get("Details") or []
    reject_detail = False
    for detail in details:
        if not isinstance(detail, dict):
            continue
        reject_detail = reject_detail or _is_reject_detail(detail)
        if not _is_fill_detail(detail):
            continue
        detail_qty = _parse_int(detail.get("Qty"))
        detail_price = _parse_float(detail.get("Price"))
        if detail_qty <= 0 or detail_price <= 0:
            continue
        detail_ts_ns = _to_ns(
            detail.get("ExecutionDay")
            or detail.get("TransactTime")
            or detail.get("RecvTime")
            or detail.get("Time")
        )
        trade_id = str(detail.get("ExecutionID") or detail.get("ID") or f"{order_id}-{len(fills) + 1}")
        fills.append(
            KabuFillDetail(
                trade_id=trade_id,
                qty=detail_qty,
                price=detail_price,
                ts_ns=detail_ts_ns,
            )
        )
        fill_value += detail_qty * detail_price
        fill_qty += detail_qty
        latest_fill_ts_ns = max(latest_fill_ts_ns, detail_ts_ns)

    avg_fill_price = 0.0
    if cum_qty > 0 and fill_qty > 0:
        avg_fill_price = fill_value / fill_qty
    elif cum_qty > 0 and price > 0:
        avg_fill_price = price
    leaves_qty = max(order_qty - cum_qty, 0)
    if order_qty > 0 and cum_qty >= order_qty:
        status = OrderStatus.FILLED
    elif is_final and cum_qty <= 0 and reject_detail:
        status = OrderStatus.REJECTED
    elif is_final:
        status = OrderStatus.CANCELED
    elif cum_qty > 0:
        status = OrderStatus.PARTIALLY_FILLED
    elif state_code <= 1 or order_state_code <= 1:
        status = OrderStatus.NEW_PENDING
    else:
        status = OrderStatus.WORKING
    return KabuOrderSnapshot(
        order_id=order_id,
        side=_internal_side(raw.get("Side")),
        order_qty=order_qty,
        cum_qty=cum_qty,
        leaves_qty=leaves_qty,
        price=price,
        avg_fill_price=avg_fill_price,
        status=status,
        fill_ts_ns=latest_fill_ts_ns,
        reason=_extract_error_message(raw) or "",
        fills=tuple(fills),
        symbol=str(raw.get("Symbol") or raw.get("symbol") or ""),
    )


def position_lot(raw: dict[str, Any]) -> PositionLot | None:
    hold_id = str(raw.get("HoldID") or raw.get("ExecutionID") or "")
    symbol = str(raw.get("Symbol") or "")
    qty = _parse_int(raw.get("LeavesQty", raw.get("Qty")))
    if not hold_id or not symbol or qty <= 0:
        return None
    closable = raw.get("ClosableQty")
    hold_qty = _parse_int(raw.get("HoldQty"))
    closable_qty = _parse_int(closable) if closable is not None else max(qty - hold_qty, 0)
    return PositionLot(
        hold_id=hold_id,
        symbol=symbol,
        exchange=_parse_int(raw.get("Exchange"), 1),
        side=_internal_side(raw.get("Side")),
        qty=qty,
        closable_qty=closable_qty,
        price=_parse_float(raw.get("Price", raw.get("ExecutionPrice"))),
        margin_trade_type=_parse_int(raw.get("MarginTradeType")),
    )


def _find_order_snapshot(raw_orders: list[dict[str, Any]], broker_order_id: str) -> KabuOrderSnapshot | None:
    snapshots = [snapshot for snapshot in (order_snapshot(raw) for raw in raw_orders) if snapshot is not None]
    for snapshot in snapshots:
        if snapshot.order_id == broker_order_id:
            return snapshot
    return snapshots[0] if len(snapshots) == 1 else None


def _normalize_base_url(base_url: str) -> str:
    normalized = str(base_url or "http://localhost:18080").rstrip("/")
    suffix = "/kabusapi"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return normalized


def _decode_payload(data: bytes) -> Any:
    if not data:
        return {}
    text = data.decode("utf-8")
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_ns(ts_str: Any) -> int:
    if not ts_str:
        return 0
    try:
        normalized = str(ts_str).replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def _kabu_side(internal_side: int) -> str:
    if internal_side > 0:
        return "2"
    if internal_side < 0:
        return "1"
    raise ValueError("internal side must be +1 or -1")


def _internal_side(raw_side: Any) -> int:
    side = str(raw_side)
    if side in {"2", "BUY", "Buy"}:
        return 1
    if side in {"1", "SELL", "Sell"}:
        return -1
    return 0


def _is_margin_mode(mode: str) -> bool:
    normalized = str(mode or "").strip().lower()
    if normalized in {"", "cash", "spot"}:
        return False
    if normalized in {"margin", "margin_daytrade", "margin_general", "credit", "shinyo"}:
        return True
    raise ValueError(f"unsupported order_profile.mode={mode!r}")


def _is_sor_exchange(exchange: int) -> bool:
    return int(exchange) == 9


def _is_tse_family_exchange(exchange: int) -> bool:
    return int(exchange) in {1, 9, 27}


def _normalize_margin_equity_exchange(exchange: int) -> int:
    return 9 if _is_tse_family_exchange(exchange) else int(exchange)


def _resolve_margin_trade_type(default_trade_type: int, selected_positions: list[PositionLot]) -> int:
    trade_types = {
        position.margin_trade_type
        for position in selected_positions
        if position.margin_trade_type > 0
    }
    return next(iter(trade_types)) if len(trade_types) == 1 else default_trade_type


def _is_fill_detail(detail: dict[str, Any]) -> bool:
    rec_type = _parse_int(detail.get("RecType"))
    if rec_type in {3, 7} or _parse_int(detail.get("State")) == 4:
        return False
    if rec_type == 8:
        return True
    execution_id = str(detail.get("ExecutionID") or "").strip()
    return execution_id.startswith("E")


def _is_reject_detail(detail: dict[str, Any]) -> bool:
    return _parse_int(detail.get("RecType")) == 7 or _parse_int(detail.get("State")) == 4


def _elapsed_ms(started_ns: int) -> float:
    return max((time.perf_counter_ns() - started_ns) / 1_000_000, 0.0)


def _aggressive_limit_price(
    *,
    side: int,
    reference_price: float,
    max_slip_ticks: float,
    tick_size: float,
) -> float:
    if side not in {-1, 1}:
        raise ValueError("aggressive IOC side must be +1 or -1")
    tick = max(tick_size, 1e-9)
    if reference_price <= 0:
        raise ValueError("aggressive IOC requires positive reference_price")
    slip_ticks = max(max_slip_ticks, 0.0)
    raw = reference_price + side * slip_ticks * tick
    if raw <= 0:
        raise ValueError("aggressive IOC limit price must be positive")
    steps = raw / tick
    snapped = math.ceil(steps - 1e-9) if side > 0 else math.floor(steps + 1e-9)
    price = round(snapped * tick, 10)
    if price <= 0:
        raise ValueError("aggressive IOC limit price must be positive")
    return price
