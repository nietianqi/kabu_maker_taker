from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..broker import BrokerPositionSnapshot, BrokerReconciliationSnapshot
from ..config import OrderProfile
from ..models import OrderIntent, OrderState, OrderStatus
from .models import KabuApiError, PositionLot, _extract_error_code
from .parsers import (
    _decode_payload,
    _kabu_side,
    _is_margin_mode,
    _is_sor_exchange,
    _normalize_base_url,
    _normalize_margin_equity_exchange,
    _resolve_margin_trade_type,
    order_snapshot,
    position_lot,
)

_ORDER_MUTATION_PATHS: frozenset[str] = frozenset(
    {
        "/kabusapi/sendorder",
        "/kabusapi/cancelorder",
    }
)
_POLLING_PATHS: frozenset[str] = frozenset(
    {
        "/kabusapi/orders",
        "/kabusapi/positions",
    }
)
_REQUEST_LANE_ORDER = "order"
_REQUEST_LANE_POLL = "poll"
_TSE_PLUS_RETRY_CODES: frozenset[int] = frozenset({100368, 100378})
SHARED_KABU_TOKEN_ENABLED_ENV = "KABU_MAKER_TAKER_USE_SHARED_KABU_TOKEN"
SHARED_KABU_TOKEN_ENV = "KABU_MAKER_TAKER_SHARED_KABU_TOKEN"


class _TokenBucket:
    def __init__(self, rate_per_sec: float) -> None:
        self._rate = max(float(rate_per_sec), 0.1)
        self._interval = 1.0 / self._rate
        self._next_allowed = 0.0

    def acquire(self) -> None:
        now = time.monotonic()
        wait = self._next_allowed - now
        if wait > 0:
            time.sleep(wait)
        self._next_allowed = max(self._next_allowed, time.monotonic()) + self._interval


class KabuRestClient:
    def __init__(
        self,
        base_url: str = "http://localhost:18080",
        *,
        order_rate_per_sec: float = 4.0,
        poll_rate_per_sec: float = 4.0,
        timeout_s: float = 5.0,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.timeout_s = max(float(timeout_s), 0.1)
        self._token: str | None = None
        self._password: str | None = None
        self._order_bucket = _TokenBucket(order_rate_per_sec)
        self._poll_bucket = _TokenBucket(poll_rate_per_sec)

    @property
    def token(self) -> str | None:
        return self._token

    def use_token(self, token: str, *, password: str | None = None) -> str:
        token = str(token or "").strip()
        if not token:
            raise KabuApiError("shared kabu token is empty")
        self._token = token
        if password is not None:
            self._password = password
        return token

    def get_token(self, password: str) -> str:
        data = self._request_json(
            "POST",
            "/kabusapi/token",
            json_body={"APIPassword": password},
            include_token=False,
        )
        token = str(data.get("Token") or "")
        if not token:
            raise KabuApiError("token response missing Token", payload=data)
        self._token = token
        self._password = password
        return token

    def get_orders(
        self,
        order_id: str | None = None,
        product: int = 0,
        *,
        lane: str = _REQUEST_LANE_POLL,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"product": product}
        if order_id:
            params["id"] = order_id
        data = self._request_json("GET", "/kabusapi/orders", params=params, lane=lane)
        return data if isinstance(data, list) else [data]

    def get_positions(
        self,
        symbol: str | None = None,
        product: int = 2,
        *,
        lane: str = _REQUEST_LANE_POLL,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"product": product}
        if symbol:
            params["symbol"] = symbol
        data = self._request_json("GET", "/kabusapi/positions", params=params, lane=lane)
        return data if isinstance(data, list) else [data]

    def register_symbol(self, symbol: str, exchange: int) -> dict[str, Any]:
        return self._request_json(
            "PUT",
            "/kabusapi/register",
            json_body={"Symbols": [{"Symbol": str(symbol), "Exchange": int(exchange)}]},
            lane=_REQUEST_LANE_POLL,
        )

    def unregister_symbol(self, symbol: str, exchange: int) -> dict[str, Any]:
        return self._request_json(
            "PUT",
            "/kabusapi/unregister",
            json_body={"Symbols": [{"Symbol": str(symbol), "Exchange": int(exchange)}]},
            lane=_REQUEST_LANE_POLL,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request_json(
            "PUT",
            "/kabusapi/cancelorder",
            json_body={"OrderId": order_id, "Password": self._password or ""},
            lane=_REQUEST_LANE_ORDER,
        )

    def send_entry_order(
        self,
        *,
        symbol: str,
        exchange: int,
        side: int,
        qty: int,
        price: float,
        is_market: bool,
        profile: OrderProfile,
        front_order_type: int | None = None,
    ) -> dict[str, Any]:
        margin_mode = _is_margin_mode(profile.mode)
        route_exchange = _normalize_margin_equity_exchange(exchange) if margin_mode else int(exchange)
        order_type = front_order_type
        if order_type is None:
            order_type = profile.front_order_type_market if is_market else profile.front_order_type_limit
        body: dict[str, Any] = {
            "Password": self._password or "",
            "Symbol": symbol,
            "Exchange": route_exchange,
            "SecurityType": 1,
            "Side": _kabu_side(side),
            "Qty": qty,
            "FrontOrderType": order_type,
            "Price": 0 if is_market else price,
            "ExpireDay": 0,
            "AccountType": profile.account_type,
        }
        if margin_mode:
            body.update(
                {
                    "CashMargin": 2,
                    "MarginTradeType": profile.margin_trade_type,
                    "DelivType": profile.margin_open_deliv_type,
                    "FundType": profile.margin_open_fund_type,
                }
            )
        else:
            if side < 0 and not profile.allow_short:
                raise ValueError("cash mode does not support opening short inventory")
            body.update(
                {
                    "CashMargin": 1,
                    "DelivType": profile.cash_buy_deliv_type if side > 0 else profile.cash_sell_deliv_type,
                    "FundType": profile.cash_buy_fund_type if side > 0 else profile.cash_sell_fund_type,
                }
            )
        return self._sendorder_with_exchange_retry(symbol=symbol, exchange=route_exchange, body=body)

    def send_exit_order(
        self,
        *,
        symbol: str,
        exchange: int,
        position_side: int,
        qty: int,
        price: float,
        is_market: bool,
        profile: OrderProfile,
        front_order_type: int | None = None,
    ) -> dict[str, Any]:
        margin_mode = _is_margin_mode(profile.mode)
        route_exchange = _normalize_margin_equity_exchange(exchange) if margin_mode else int(exchange)
        broker_side = -position_side
        order_type = front_order_type
        if order_type is None:
            order_type = profile.front_order_type_market if is_market else profile.front_order_type_limit
        body: dict[str, Any] = {
            "Password": self._password or "",
            "Symbol": symbol,
            "Exchange": route_exchange,
            "SecurityType": 1,
            "Side": _kabu_side(broker_side),
            "Qty": qty,
            "FrontOrderType": order_type,
            "Price": 0 if is_market else price,
            "ExpireDay": 0,
            "AccountType": profile.account_type,
        }
        if margin_mode:
            sor_route = _is_sor_exchange(route_exchange)
            close_positions, selected = self._build_close_positions(
                symbol=symbol,
                exchange=route_exchange,
                position_side=position_side,
                qty=qty,
                strict_exchange=not sor_route,
                allow_mixed_exchanges=sor_route,
            )
            body.update(
                {
                    "CashMargin": 3,
                    "MarginTradeType": _resolve_margin_trade_type(profile.margin_trade_type, selected),
                    "DelivType": profile.margin_close_deliv_type,
                    "ClosePositions": close_positions,
                }
            )
            try:
                return self._sendorder_with_exchange_retry(symbol=symbol, exchange=route_exchange, body=body)
            except KabuApiError as exc:
                if _extract_error_code(exc.payload) != 8:
                    raise
                retry_positions, retry_selected = self._build_close_positions(
                    symbol=symbol,
                    exchange=route_exchange,
                    position_side=position_side,
                    qty=qty,
                    strict_exchange=False,
                    allow_mixed_exchanges=sor_route,
                )
                retry_exchange = route_exchange
                exchanges = {position.exchange for position in retry_selected if position.exchange > 0}
                if not sor_route and len(exchanges) == 1:
                    retry_exchange = next(iter(exchanges))
                retry_body = dict(body)
                retry_body["Exchange"] = retry_exchange
                retry_body["MarginTradeType"] = _resolve_margin_trade_type(
                    profile.margin_trade_type,
                    retry_selected,
                )
                retry_body["ClosePositions"] = retry_positions
                return self._sendorder_with_exchange_retry(
                    symbol=symbol,
                    exchange=retry_exchange,
                    body=retry_body,
                )

        body.update(
            {
                "CashMargin": 1,
                "DelivType": profile.cash_buy_deliv_type if broker_side > 0 else profile.cash_sell_deliv_type,
                "FundType": profile.cash_buy_fund_type if broker_side > 0 else profile.cash_sell_fund_type,
            }
        )
        return self._sendorder_with_exchange_retry(symbol=symbol, exchange=route_exchange, body=body)

    def _build_close_positions(
        self,
        *,
        symbol: str,
        exchange: int,
        position_side: int,
        qty: int,
        strict_exchange: bool = True,
        allow_mixed_exchanges: bool = False,
    ) -> tuple[list[dict[str, Any]], list[PositionLot]]:
        positions = [position_lot(raw) for raw in self.get_positions(symbol, lane=_REQUEST_LANE_ORDER)]
        same_side = [
            position
            for position in positions
            if position is not None and position.side == position_side and position.closable_qty > 0
        ]
        exchange_matched = [position for position in same_side if position.exchange == exchange]
        if strict_exchange and exchange_matched:
            usable = exchange_matched
        elif strict_exchange:
            usable = []
        elif allow_mixed_exchanges:
            usable = same_side
        else:
            unique_exchanges = {position.exchange for position in same_side if position.exchange > 0}
            if len(unique_exchanges) > 1:
                raise KabuApiError(
                    f"ambiguous inventory exchange for close {symbol}: {sorted(unique_exchanges)}"
                )
            usable = same_side

        remaining = qty
        close_positions: list[dict[str, Any]] = []
        selected: list[PositionLot] = []
        for position in usable:
            take_qty = min(position.closable_qty, remaining)
            close_positions.append({"HoldID": position.hold_id, "Qty": take_qty})
            selected.append(position)
            remaining -= take_qty
            if remaining == 0:
                break
        if remaining > 0:
            raise KabuApiError(f"not enough inventory to close {symbol} exchange={exchange} qty={qty}")
        return close_positions, selected

    def _sendorder_with_exchange_retry(
        self,
        *,
        symbol: str,
        exchange: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._request_json("POST", "/kabusapi/sendorder", json_body=body, lane=_REQUEST_LANE_ORDER)
        except KabuApiError as exc:
            if exchange != 1 or _extract_error_code(exc.payload) not in _TSE_PLUS_RETRY_CODES:
                raise
            retry_body = dict(body)
            retry_body["Exchange"] = 27
            return self._request_json(
                "POST",
                "/kabusapi/sendorder",
                json_body=retry_body,
                lane=_REQUEST_LANE_ORDER,
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        include_token: bool = True,
        lane: str | None = None,
    ) -> Any:
        bucket = self._bucket_for_request(method, path, lane)
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Content-Type": "application/json"}
        if include_token:
            headers["X-API-KEY"] = self._token or ""
        data = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")

        attempts = 3 if method.upper() == "GET" and path in _POLLING_PATHS else 1
        for attempt in range(attempts):
            bucket.acquire()
            request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return _decode_payload(response.read())
            except urllib.error.HTTPError as exc:
                payload = _decode_payload(exc.read())
                should_retry = (
                    exc.code in {429, 500, 502, 503, 504}
                    and path not in _ORDER_MUTATION_PATHS
                    and attempt < attempts - 1
                )
                if should_retry:
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise KabuApiError(
                    f"{method.upper()} {path} failed with status {exc.code}",
                    status=exc.code,
                    payload=payload,
                ) from exc
            except urllib.error.URLError as exc:
                if method.upper() == "GET" and attempt < attempts - 1:
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise KabuApiError(f"{method.upper()} {path} failed: {exc.reason}") from exc
        raise KabuApiError(f"{method.upper()} {path} failed")

    @staticmethod
    def _resolve_request_lane(method: str, path: str, lane: str | None) -> str:
        if lane is not None:
            normalized = lane.strip().lower()
            if normalized in {_REQUEST_LANE_ORDER, _REQUEST_LANE_POLL}:
                return normalized
            raise ValueError(f"unsupported REST rate-limit lane={lane!r}")
        if path in _ORDER_MUTATION_PATHS:
            return _REQUEST_LANE_ORDER
        if method.upper() == "GET" and path in _POLLING_PATHS:
            return _REQUEST_LANE_POLL
        return _REQUEST_LANE_ORDER

    def _bucket_for_request(self, method: str, path: str, lane: str | None) -> _TokenBucket:
        request_lane = self._resolve_request_lane(method, path, lane)
        return self._poll_bucket if request_lane == _REQUEST_LANE_POLL else self._order_bucket
