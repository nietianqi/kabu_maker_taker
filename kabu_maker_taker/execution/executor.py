from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Callable

from ..broker import BrokerOpenOrderSnapshot, BrokerPositionSnapshot, BrokerReconciliationSnapshot
from ..config import AppConfig, effective_register_exchange
from ..models import BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderState, OrderStatus
from ..strategy import ORDER_ROLE_ENTRY
from .client import KabuRestClient, SHARED_KABU_TOKEN_ENABLED_ENV, SHARED_KABU_TOKEN_ENV, _REQUEST_LANE_POLL
from .models import KabuApiError, KabuOrderSnapshot, LiveExecutionResult
from .parsers import _aggressive_limit_price, _elapsed_ms, _find_order_snapshot, order_snapshot, position_lot


def _token_fingerprint(token: str) -> str:
    token = str(token or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8] if token else ""


class KabuRestExecutor:
    def __init__(self, config: AppConfig, client: KabuRestClient | None = None) -> None:
        self.config = config
        self.client = client or KabuRestClient(
            config.kabu.base_url,
            order_rate_per_sec=config.kabu.order_rate_per_sec,
            poll_rate_per_sec=config.kabu.poll_rate_per_sec,
        )
        self._using_shared_token = False
        self._token_fingerprint = ""
        self._token_source = ""
        self._token_refresh_count = 0

    def start(self) -> str:
        if not self.config.kabu.api_password:
            raise KabuApiError("kabu.api_password is required for --live")
        if os.environ.get(SHARED_KABU_TOKEN_ENABLED_ENV, "").strip() == "1":
            shared_token = os.environ.get(SHARED_KABU_TOKEN_ENV, "").strip()
            token = self.client.use_token(shared_token, password=self.config.kabu.api_password)
            self._using_shared_token = True
            self._token_source = "shared"
            self._token_fingerprint = _token_fingerprint(token)
            self._token_refresh_count = 0
            return token
        token = self.client.get_token(self.config.kabu.api_password)
        self._using_shared_token = False
        self._token_source = "worker"
        self._token_fingerprint = _token_fingerprint(token)
        self._token_refresh_count = 0
        return token

    def auth_context(self) -> dict[str, object]:
        return {
            "shared_token": self._using_shared_token,
            "token_source": self._token_source,
            "token_sha256_8": self._token_fingerprint,
            "token_refresh_count": self._token_refresh_count,
        }

    def submit(self, intent: OrderIntent, *, role: str, now_ns: int = 0) -> LiveExecutionResult:
        started_ns = time.perf_counter_ns()
        try:
            submit_intent, front_order_type = self._prepare_live_intent(intent)
            if role == "exit":
                payload = self._with_auth_retry(
                    lambda: self.client.send_exit_order(
                        symbol=submit_intent.symbol,
                        exchange=submit_intent.exchange,
                        position_side=-submit_intent.side,
                        qty=submit_intent.qty,
                        price=submit_intent.price,
                        is_market=submit_intent.is_market,
                        profile=self.config.kabu.order_profile,
                        front_order_type=front_order_type,
                    )
                )
            else:
                payload = self._with_auth_retry(
                    lambda: self.client.send_entry_order(
                        symbol=submit_intent.symbol,
                        exchange=submit_intent.exchange,
                        side=submit_intent.side,
                        qty=submit_intent.qty,
                        price=submit_intent.price,
                        is_market=submit_intent.is_market,
                        profile=self.config.kabu.order_profile,
                        front_order_type=front_order_type,
                    )
                )
            broker_order_id = _extract_order_id(payload)
            if not broker_order_id:
                event = BrokerOrderEvent(
                    order_id=intent.client_order_id,
                    status=OrderStatus.UNKNOWN,
                    ts_ns=now_ns,
                    reason="sendorder response missing OrderId",
                )
                return LiveExecutionResult(
                    events=(event,),
                    api_error=True,
                    halt_reason="submit_unknown",
                    request_kind="submit",
                    latency_ms=_elapsed_ms(started_ns),
                )
            event = BrokerOrderEvent(
                order_id=intent.client_order_id,
                broker_order_id=broker_order_id,
                status=OrderStatus.WORKING,
                ts_ns=now_ns,
            )
            return LiveExecutionResult(
                events=(event,),
                api_success=True,
                request_kind="submit",
                latency_ms=_elapsed_ms(started_ns),
            )
        except ValueError as exc:
            event = BrokerOrderEvent(
                order_id=intent.client_order_id,
                status=OrderStatus.REJECTED,
                ts_ns=now_ns,
                reason=str(exc),
            )
            return LiveExecutionResult(
                events=(event,),
                halt_reason="local_reject",
                request_kind="submit",
                latency_ms=_elapsed_ms(started_ns),
            )
        except KabuApiError as exc:
            event = BrokerOrderEvent(
                order_id=intent.client_order_id,
                status=OrderStatus.UNKNOWN,
                ts_ns=now_ns,
                reason=str(exc),
            )
            return LiveExecutionResult(
                events=(event,),
                api_error=True,
                halt_reason="submit_unknown",
                request_kind="submit",
                latency_ms=_elapsed_ms(started_ns),
            )

    def _prepare_live_intent(self, intent: OrderIntent) -> tuple[OrderIntent, int | None]:
        if not intent.is_market:
            return intent, None
        price = _aggressive_limit_price(
            side=intent.side,
            reference_price=intent.reference_price,
            max_slip_ticks=intent.max_slip_ticks or self.config.strategy.max_slip_ticks,
            tick_size=self.config.tick_size,
        )
        return (
            OrderIntent(
                symbol=intent.symbol,
                exchange=intent.exchange,
                side=intent.side,
                qty=intent.qty,
                price=price,
                is_market=False,
                strategy=intent.strategy,
                reason=intent.reason,
                score=intent.score,
                reference_price=intent.reference_price,
                max_slip_ticks=intent.max_slip_ticks,
                client_order_id=intent.client_order_id,
                setup_type=intent.setup_type,
                selection_reason=intent.selection_reason,
            ),
            self.config.kabu.order_profile.front_order_type_ioc_limit,
        )

    def cancel(self, order: OrderState, *, now_ns: int = 0) -> LiveExecutionResult:
        if not order.broker_order_id:
            event = BrokerOrderEvent(
                order_id=order.client_order_id,
                status=OrderStatus.UNKNOWN,
                ts_ns=now_ns,
                reason="missing_broker_order_id",
            )
            return LiveExecutionResult(events=(event,), halt_reason="missing_broker_order_id")
        started_ns = time.perf_counter_ns()
        try:
            self._with_auth_retry(lambda: self.client.cancel_order(order.broker_order_id))
            event = BrokerOrderEvent(
                order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                status=OrderStatus.CANCEL_PENDING,
                ts_ns=now_ns,
            )
            return LiveExecutionResult(
                events=(event,),
                api_success=True,
                request_kind="cancel",
                latency_ms=_elapsed_ms(started_ns),
            )
        except KabuApiError as exc:
            event = BrokerOrderEvent(
                order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                status=OrderStatus.UNKNOWN,
                ts_ns=now_ns,
                reason=str(exc),
            )
            return LiveExecutionResult(
                events=(event,),
                api_error=True,
                halt_reason="cancel_unknown",
                request_kind="cancel",
                latency_ms=_elapsed_ms(started_ns),
            )

    def poll_order_events(
        self,
        active_orders: list[OrderState],
        *,
        now_ns: int = 0,
    ) -> LiveExecutionResult:
        events: list[BrokerOrderEvent | BrokerFillEvent] = []
        active = list(active_orders)
        if not active:
            return LiveExecutionResult()
        for order in active:
            if not order.broker_order_id:
                events.append(
                    BrokerOrderEvent(
                        order_id=order.client_order_id,
                        status=OrderStatus.UNKNOWN,
                        ts_ns=now_ns,
                        reason="missing_broker_order_id",
                    )
                )
                return LiveExecutionResult(
                    events=tuple(events),
                    halt_reason="missing_broker_order_id",
                )
        started_ns = time.perf_counter_ns()
        try:
            if len(active) > 1:
                raw_orders = self._with_auth_retry(
                    lambda: self.client.get_orders(product=0, lane=_REQUEST_LANE_POLL)
                )
                snapshots = [
                    snapshot
                    for snapshot in (order_snapshot(raw) for raw in raw_orders)
                    if snapshot is not None
                ]
                snapshot_by_id = {snapshot.order_id: snapshot for snapshot in snapshots}
                for order in active:
                    snapshot = snapshot_by_id.get(order.broker_order_id)
                    if snapshot is not None:
                        events.extend(_events_from_order_snapshot(order, snapshot, now_ns))
            else:
                order = active[0]
                raw_orders = self._with_auth_retry(
                    lambda: self.client.get_orders(order.broker_order_id, lane=_REQUEST_LANE_POLL)
                )
                snapshot = _find_order_snapshot(raw_orders, order.broker_order_id)
                if snapshot is not None:
                    events.extend(_events_from_order_snapshot(order, snapshot, now_ns))
        except KabuApiError as exc:
            for order in active:
                events.append(
                    BrokerOrderEvent(
                        order_id=order.client_order_id,
                        broker_order_id=order.broker_order_id,
                        status=OrderStatus.UNKNOWN,
                        ts_ns=now_ns,
                        reason=str(exc),
                    )
                )
            return LiveExecutionResult(
                events=tuple(events),
                api_error=True,
                request_kind="poll",
                latency_ms=_elapsed_ms(started_ns),
            )
        return LiveExecutionResult(
            events=tuple(events),
            api_success=True,
            request_kind="poll",
            latency_ms=_elapsed_ms(started_ns),
        )

    def snapshot(self) -> BrokerReconciliationSnapshot:
        return self._with_auth_retry(self._snapshot)

    def _snapshot(self) -> BrokerReconciliationSnapshot:
        positions = self._position_snapshot()
        raw_orders = self.client.get_orders(product=0, lane=_REQUEST_LANE_POLL)
        active_orders = [
            order
            for order in (order_snapshot(raw) for raw in raw_orders)
            if order is not None and not _kabu_order_final(order)
        ]
        current_active_orders = [order for order in active_orders if self._is_current_order(order)]
        ignored_orders = tuple(self._ignored_open_order_snapshot(order) for order in active_orders)
        policy = self.config.kabu.startup_open_order_policy.strip().lower()
        if policy not in {"reject", "ignore"}:
            raise KabuApiError(f"invalid kabu.startup_open_order_policy={self.config.kabu.startup_open_order_policy}")
        if policy == "reject" and current_active_orders:
            raise KabuApiError(
                "unsafe active kabu orders at startup; cancel or reconcile them before --live",
                payload={"order_ids": [order.order_id for order in current_active_orders]},
            )
        return BrokerReconciliationSnapshot(
            ts_ns=time.time_ns(),
            positions=positions,
            ignored_open_orders=ignored_orders,
        )

    def _with_auth_retry(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except KabuApiError as exc:
            if not self._refresh_token_after_auth_failure(exc):
                raise
            return operation()

    def _refresh_token_after_auth_failure(self, exc: KabuApiError) -> bool:
        if exc.status not in {401, 403} or not self.config.kabu.api_password:
            return False
        was_shared = self._using_shared_token
        token = self.client.get_token(self.config.kabu.api_password)
        self._using_shared_token = False
        self._token_source = (
            "worker_retry_after_shared_401"
            if was_shared and exc.status == 401
            else f"worker_retry_after_auth_{exc.status}"
        )
        self._token_fingerprint = _token_fingerprint(token)
        self._token_refresh_count += 1
        return True

    def register_market_data(self) -> None:
        exchange = self.market_data_exchange
        try:
            self._with_auth_retry(lambda: self.client.register_symbol(self.config.symbol, exchange))
        except KabuApiError as exc:
            raise KabuApiError(
                (
                    "market data register failed "
                    f"symbol={self.config.symbol} "
                    f"trade_exchange={self.config.exchange} "
                    f"register_exchange={exchange}"
                ),
                status=exc.status,
                payload=exc.payload,
            ) from exc

    def unregister_market_data(self) -> None:
        exchange = self.market_data_exchange
        try:
            self._with_auth_retry(lambda: self.client.unregister_symbol(self.config.symbol, exchange))
        except KabuApiError as exc:
            raise KabuApiError(
                (
                    "market data unregister failed "
                    f"symbol={self.config.symbol} "
                    f"trade_exchange={self.config.exchange} "
                    f"register_exchange={exchange}"
                ),
                status=exc.status,
                payload=exc.payload,
            ) from exc

    @property
    def market_data_exchange(self) -> int:
        return effective_register_exchange(self.config.exchange, self.config.kabu.register_exchange)

    def open_order_snapshots(self) -> tuple[KabuOrderSnapshot, ...]:
        raw_orders = self._with_auth_retry(lambda: self.client.get_orders(product=0, lane=_REQUEST_LANE_POLL))
        return self._open_order_snapshots(raw_orders)

    def position_snapshot(self) -> tuple[BrokerPositionSnapshot, ...]:
        return self._with_auth_retry(self._position_snapshot)

    def _position_snapshot(self) -> tuple[BrokerPositionSnapshot, ...]:
        lots = [
            lot
            for lot in (
                position_lot(raw)
                for raw in self.client.get_positions(self.config.symbol, lane=_REQUEST_LANE_POLL)
            )
            if lot is not None and lot.symbol == self.config.symbol and lot.qty > 0 and lot.side in {-1, 1}
        ]
        sides = {lot.side for lot in lots}
        if len(sides) > 1:
            raise KabuApiError(f"ambiguous mixed-side inventory for {self.config.symbol}")
        if not lots:
            return ()
        total_qty = sum(lot.qty for lot in lots)
        avg_price = sum(lot.qty * lot.price for lot in lots) / max(total_qty, 1)
        return (
            BrokerPositionSnapshot(
                symbol=self.config.symbol,
                exchange=self.config.exchange,
                side=lots[0].side,
                qty=total_qty,
                avg_price=avg_price,
                entry_mode="broker_unknown",
            ),
        )

    def _open_order_snapshots(self, raw_orders: list[dict[str, Any]]) -> tuple[KabuOrderSnapshot, ...]:
        snapshots = []
        for raw in raw_orders:
            snapshot = order_snapshot(raw)
            if snapshot is None:
                continue
            if snapshot.symbol and snapshot.symbol != self.config.symbol:
                continue
            if snapshot.status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            }:
                continue
            snapshots.append(snapshot)
        return tuple(snapshots)

    def _is_current_order(self, order: KabuOrderSnapshot) -> bool:
        symbol_matches = not order.symbol or order.symbol == self.config.symbol
        exchange_matches = order.exchange in {0, self.config.exchange}
        return symbol_matches and exchange_matches

    def _ignored_open_order_snapshot(self, order: KabuOrderSnapshot) -> BrokerOpenOrderSnapshot:
        qty = order.leaves_qty or order.order_qty
        return BrokerOpenOrderSnapshot(
            symbol=order.symbol,
            exchange=order.exchange or self.config.exchange,
            side=order.side,
            qty=qty,
            price=order.price,
            role=ORDER_ROLE_ENTRY,
            strategy="broker_ignored",
            reason="startup_open_order_ignored",
            reference_price=order.price,
            client_order_id=f"broker-{order.order_id}",
            broker_order_id=order.order_id,
            status=order.status,
            cum_qty=order.cum_qty,
            avg_fill_price=order.avg_fill_price,
        )


def _extract_order_id(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("OrderId") or payload.get("ID") or "")
    return ""


def _events_from_order_snapshot(
    order: OrderState,
    snapshot: KabuOrderSnapshot,
    now_ns: int,
) -> list[BrokerOrderEvent | BrokerFillEvent]:
    events: list[BrokerOrderEvent | BrokerFillEvent] = []
    for fill in snapshot.fills:
        events.append(
            BrokerFillEvent(
                order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                qty=fill.qty,
                price=fill.price,
                ts_ns=fill.ts_ns or now_ns,
                trade_id=fill.trade_id,
            )
        )
    events.append(
        BrokerOrderEvent(
            order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=snapshot.status,
            ts_ns=now_ns or snapshot.fill_ts_ns,
            cum_qty=snapshot.cum_qty,
            avg_fill_price=snapshot.avg_fill_price,
            reason=snapshot.reason,
        )
    )
    return events


def _kabu_order_final(order: KabuOrderSnapshot) -> bool:
    return order.status in {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    }
