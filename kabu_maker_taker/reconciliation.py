from __future__ import annotations

from typing import TYPE_CHECKING

from .broker import BrokerReconciliationSnapshot
from .models import OrderStatus, PositionState
from .orders import OrderLedger
from .strategy import ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT

if TYPE_CHECKING:
    from .combined import CombinedMakerTakerStrategy


def reconcile_strategy_from_broker(
    strategy: CombinedMakerTakerStrategy,
    snapshot: BrokerReconciliationSnapshot,
    *,
    now_ns: int = 0,
    manage_exit: bool = True,
) -> dict[str, int | float | bool]:
    ts = now_ns or snapshot.ts_ns
    strategy.orders = OrderLedger()
    strategy.position = PositionState()
    strategy.lollipop.reset()
    strategy.entry_order_active = False
    strategy._working_entry_side = 0
    strategy._working_entry_price = 0.0
    strategy._open_trade_realized_pnl = 0.0
    strategy._partial_loss_counted_for_position = False

    strategy.restore_daily_pnl(snapshot.daily_pnl, ts)

    restored_orders = 0
    restored_exit_orders = 0
    active_exit_order_price = 0.0
    for open_order in snapshot.open_orders:
        if open_order.symbol != strategy.config.symbol or open_order.exchange != strategy.config.exchange:
            continue
        strategy.orders.restore_order(
            open_order.to_intent(),
            role=open_order.role,
            status=open_order.status,
            broker_order_id=open_order.broker_order_id,
            submitted_ts_ns=open_order.submitted_ts_ns or ts,
            updated_ts_ns=open_order.updated_ts_ns or ts,
            cum_qty=open_order.cum_qty,
            avg_fill_price=open_order.avg_fill_price,
        )
        restored_orders += 1
        if open_order.role == ORDER_ROLE_EXIT and open_order.status not in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }:
            restored_exit_orders += 1
            active_exit_order_price = open_order.price

    restored_position = 0
    for position in snapshot.positions:
        if position.symbol != strategy.config.symbol or position.exchange != strategy.config.exchange:
            continue
        if position.qty <= 0 or position.side not in (-1, 1) or position.avg_price <= 0:
            continue
        strategy.position = PositionState(
            side=position.side,
            qty=position.qty,
            avg_price=position.avg_price,
            entry_mode=position.entry_mode,
            entry_ts_ns=position.entry_ts_ns or ts,
        )
        restored_position = 1
        break

    if strategy.position.qty > 0 and manage_exit:
        if restored_exit_orders > 0:
            strategy.lollipop.restore_active_exit(
                tp_price=active_exit_order_price,
                entry_mode=strategy.position.entry_mode or "maker",
                entry_side=strategy.position.side,
                entry_ts_ns=strategy.position.entry_ts_ns or ts,
                retry_count=1,
            )
        else:
            strategy.lollipop.on_entry_fill(
                strategy.position.avg_price,
                strategy.position.entry_mode or "maker",
                ts,
                entry_side=strategy.position.side,
            )

    strategy._refresh_working_entry_state()
    return {
        "positions_restored": restored_position,
        "orders_restored": restored_orders,
        "active_entries": len(strategy.orders.active_by_role(ORDER_ROLE_ENTRY)),
        "active_exits": len(strategy.orders.active_by_role(ORDER_ROLE_EXIT)),
        "daily_pnl": strategy.risk.daily_pnl,
        "managed_exit": bool(strategy.position.qty > 0 and manage_exit),
        "restored_active_exit": bool(restored_exit_orders > 0),
    }
