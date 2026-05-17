"""Combined maker/taker strategy coordinator.

``CombinedMakerTakerStrategy`` is the single public entry point for the
execution layer.  On every board tick ``on_board()`` runs the full pipeline:

  1. Signal engine  → ``SignalPacket``
  2. Lollipop TP   → optional exit intent
  3. Entry cancel  → optional cancel of working limit order
  4. Confirmation  → require N consecutive matching ticks
  5. Risk gates    → spread, position size, session, circuit breakers
  6. Entry policy  → taker (aggressive) first, maker (passive) fallback
  7. Return        → ``StrategyResult`` with at most one entry intent

The strategy never mutates broker state.  All fills and order events arrive
via ``on_broker_fill()`` / ``on_broker_order_event()``.
"""
from __future__ import annotations

from .broker import BrokerReconciliationSnapshot
from .config import AppConfig
from .journal import TradeJournal
from .lollipop import LollipopTPManager
from .metrics import MetricsCollector
from .models import (
    BoardSnapshot,
    BrokerFillEvent,
    BrokerOrderEvent,
    EntryDecision,
    LollipopPhase,
    MarketState,
    OrderIntent,
    OrderState,
    OrderStatus,
    PositionState,
    SignalPacket,
    StrategyResult,
    TradePrint,
)
from .orders import OrderLedger
from .risk import RiskManager
from .signals import MicrostructureSignalEngine
from .reconciliation import reconcile_strategy_from_broker
from .strategy import (
    ConfirmationTracker,
    ENTRY_MODE_TAKER,
    MakerStrategy,
    MarketStateDetector,
    ORDER_ROLE_ENTRY,
    ORDER_ROLE_EXIT,
    TakerStrategy,
)


class CombinedMakerTakerStrategy:
    def __init__(self, config: AppConfig):
        self.config = config
        self.position = PositionState()
        self.signals = MicrostructureSignalEngine(tick_size=config.tick_size, config=config.signals)
        self.maker = MakerStrategy(config.strategy, tick_size=config.tick_size)
        self.taker = TakerStrategy(config.strategy, tick_size=config.tick_size)
        self.risk = RiskManager(config=config.risk, tick_size=config.tick_size, lot_size=config.lot_size)
        self.confirmation = ConfirmationTracker()
        self.lollipop = LollipopTPManager(config.lollipop, config.tick_size, config.lot_size)
        self.market_state_detector = MarketStateDetector(config.market_state, config.tick_size)
        self.orders = OrderLedger()
        self.metrics = MetricsCollector(tick_size=config.tick_size)
        self.last_result: StrategyResult | None = None
        self.journal: TradeJournal | None = None   # set by app.py when enable_journal=True
        self._last_entry_signal: SignalPacket | None = None  # captured at entry fill
        self.entry_order_active = False
        self._working_entry_side: int = 0
        self._working_entry_price: float = 0.0
        self._open_trade_realized_pnl: float = 0.0
        self._partial_loss_counted_for_position = False

    def on_trade(self, trade: TradePrint) -> None:
        if trade.symbol != self.config.symbol:
            return
        self.signals.on_trade(trade)

    def on_board(self, snapshot: BoardSnapshot, *, now_ns: int | None = None) -> StrategyResult:
        ts = now_ns if now_ns is not None else snapshot.ts_ns

        if snapshot.symbol != self.config.symbol:
            result = StrategyResult(None, EntryDecision(False, "symbol_mismatch"), None, blocked_reason="symbol_mismatch")
            self.last_result = result
            return result
        if snapshot.duplicate or snapshot.out_of_order:
            result = StrategyResult(None, EntryDecision(False, "duplicate_or_out_of_order"), None)
            self.last_result = result
            return result

        board_stale = self.risk.is_stale_board(snapshot.ts_ns)
        self.risk.update_board_ts(snapshot.ts_ns)
        market_state = self.market_state_detector.update(snapshot, ts)
        signal = self.signals.on_board(snapshot)
        self.metrics.on_board(snapshot)
        if self.journal is not None:
            self.journal.on_board(snapshot)

        # Force-exit taker position when tape/LOB OFI flips to strong negative flow.
        if (
            self.position.qty > 0
            and self.position.entry_mode == ENTRY_MODE_TAKER
            and self.config.strategy.flow_flip_threshold > 0
            and self.lollipop.state.phase != LollipopPhase.TIMEOUT
            and (
                signal.tape_ofi_raw <= -self.config.strategy.flow_flip_threshold
                or signal.lob_ofi_raw <= -self.config.strategy.flow_flip_threshold
            )
        ):
            self.lollipop.force_exit_next_tick()

        lollipop_action = self.lollipop.tick(
            snapshot,
            self.position,
            ts,
            symbol=self.config.symbol,
            exchange=self.config.exchange,
        )
        exit_cancel_signal = ""
        exit_intent = None
        if lollipop_action.intent is not None:
            if lollipop_action.action == "force_exit" and self.orders.active_by_role(ORDER_ROLE_EXIT):
                exit_cancel_signal = "replace_active_exit_before_force_exit"
            else:
                exit_intent = self._track_intent(lollipop_action.intent, role=ORDER_ROLE_EXIT, now_ns=ts)
                self.metrics.record_exit_intent(exit_intent)

        if self.entry_order_active:
            entry_cancel_signal = ""
            entry_cancel_blocked_reason = ""
            if self._working_entry_side != 0:
                entry_orders = self.orders.active_by_role(ORDER_ROLE_ENTRY)
                order_age_ns = ts - entry_orders[-1].submitted_ts_ns if entry_orders else 0
                desired_price = self.maker.compute_quote_price(snapshot, signal, self._working_entry_side)
                raw_cancel_signal = self.maker.calc_cancel_reason(
                    signal,
                    self._working_entry_side,
                    self._working_entry_price,
                    market_state,
                    current_spread=snapshot.spread,
                    order_age_ns=order_age_ns,
                    desired_price=desired_price,
                    board_stale=board_stale,
                )
                if raw_cancel_signal:
                    allowed_cancel, blocked_reason = self.risk.can_send_cancel_signal(raw_cancel_signal, ts)
                    if allowed_cancel:
                        entry_cancel_signal = raw_cancel_signal
                        self.risk.record_cancel_request(raw_cancel_signal, ts)
                        self.metrics.record_cancel_signal()
                    else:
                        entry_cancel_blocked_reason = blocked_reason
                        self.metrics.record_cancel_signal(blocked_reason=blocked_reason)

            result = StrategyResult(
                None,
                EntryDecision(False, "working_entry"),
                signal,
                blocked_reason="working_entry",
                exit_intent=exit_intent,
                entry_cancel_signal=entry_cancel_signal,
                entry_cancel_blocked_reason=entry_cancel_blocked_reason,
                exit_cancel_signal=exit_cancel_signal,
                market_state=market_state,
            )
            self.last_result = result
            return result

        if self.position.qty > 0 and self.lollipop.is_busy:
            result = StrategyResult(
                None,
                EntryDecision(False, "lollipop_active"),
                signal,
                blocked_reason="lollipop_active",
                exit_intent=exit_intent,
                exit_cancel_signal=exit_cancel_signal,
                market_state=market_state,
            )
            self.last_result = result
            return result

        decision = self._choose_decision(snapshot, signal, ts, market_state)
        confirmed, progress = self.confirmation.observe(decision)
        if not confirmed:
            result = StrategyResult(
                None,
                decision,
                signal,
                blocked_reason=decision.reason or "confirming",
                confirm_progress=progress,
                exit_intent=exit_intent,
                exit_cancel_signal=exit_cancel_signal,
                market_state=market_state,
            )
            self.last_result = result
            return result

        expected_price = snapshot.ask if decision.side > 0 else snapshot.bid
        base_qty = self.config.strategy.trade_qty
        if self.config.strategy.vol_aware_sizing and signal.vol_expansion and base_qty > self.config.lot_size:
            base_qty = max(self.config.lot_size, (base_qty // 2 // self.config.lot_size) * self.config.lot_size)
        # Dynamic sizing: scale up taker qty at high conviction
        if (
            decision.entry_mode == ENTRY_MODE_TAKER
            and self.config.strategy.scale_qty_by_score
            and decision.entry_score >= self.config.strategy.scale_qty_score_threshold
        ):
            scaled = int(base_qty * self.config.strategy.scale_qty_multiplier // self.config.lot_size) * self.config.lot_size
            base_qty = scaled
        base_qty = self.risk.order_qty(
            base_qty=base_qty,
            position=self.position,
            expected_price=expected_price,
        )
        if base_qty <= 0:
            self.confirmation.reset()
            result = StrategyResult(
                None,
                decision,
                signal,
                blocked_reason="qty_zero",
                confirm_progress=progress,
                exit_intent=exit_intent,
                exit_cancel_signal=exit_cancel_signal,
                market_state=market_state,
            )
            self.last_result = result
            return result

        allowed, reason = self.risk.can_enter(
            snapshot=snapshot,
            decision=decision,
            position=self.position,
            now_ns=ts,
            expected_price=expected_price,
            order_qty=base_qty,
        )
        if not allowed:
            self.confirmation.reset()
            self.metrics.record_risk_block(reason)
            result = StrategyResult(
                None,
                decision,
                signal,
                blocked_reason=reason,
                confirm_progress=progress,
                exit_intent=exit_intent,
                exit_cancel_signal=exit_cancel_signal,
                market_state=market_state,
            )
            self.last_result = result
            return result

        if decision.entry_mode == ENTRY_MODE_TAKER:
            intent = self.taker.build_intent(
                symbol=self.config.symbol,
                exchange=self.config.exchange,
                lot_size=self.config.lot_size,
                qty=base_qty,
                snapshot=snapshot,
                decision=decision,
            )
        else:
            intent = self.maker.build_intent(
                symbol=self.config.symbol,
                exchange=self.config.exchange,
                tick_size=self.config.tick_size,
                lot_size=self.config.lot_size,
                qty=base_qty,
                snapshot=snapshot,
                decision=decision,
                signal=signal,
                position=self.position,
                max_inventory_qty=self.risk.config.max_inventory_qty,
            )

        intent = self._track_intent(intent, role=ORDER_ROLE_ENTRY, now_ns=ts)
        self.risk.record_entry_order(ts)
        self.metrics.record_entry_intent(intent, now_ns=ts)
        result = StrategyResult(
            intent,
            decision,
            signal,
            confirm_progress=progress,
            exit_intent=exit_intent,
            exit_cancel_signal=exit_cancel_signal,
            market_state=market_state,
        )
        self.entry_order_active = True
        self.last_result = result
        return result

    def apply_fill(self, *, side: int, qty: int, price: float, now_ns: int = 0, entry_mode: str = "") -> str:
        raise RuntimeError("manual apply_fill is disabled; use on_broker_fill() or on_broker_order_event()")

    def on_broker_order_event(self, event: BrokerOrderEvent) -> str:
        order, fill_qty, fill_price = self.orders.apply_order_event(event)
        if order is None:
            return "unknown_order"
        result = self._apply_broker_fill(order, fill_qty, fill_price, event.ts_ns) if fill_qty > 0 else order.status.value
        self._handle_final_order_state(order, event.ts_ns)
        self._refresh_working_entry_state()
        return result

    def on_broker_fill(self, event: BrokerFillEvent) -> str:
        order, fill_qty, fill_price = self.orders.apply_fill_event(event)
        if order is None:
            return "unknown_order"
        result = self._apply_broker_fill(order, fill_qty, fill_price, event.ts_ns) if fill_qty > 0 else order.status.value
        self._handle_final_order_state(order, event.ts_ns)
        self._refresh_working_entry_state()
        return result

    def request_cancel(self, order_id: str, reason: str = "", now_ns: int = 0) -> OrderState | None:
        order = self.orders.mark_cancel_pending(order_id, reason=reason, now_ns=now_ns)
        self._refresh_working_entry_state()
        return order

    def restore_position(
        self,
        *,
        side: int,
        qty: int,
        avg_price: float,
        entry_mode: str = "maker",
        now_ns: int = 0,
        manage_exit: bool = True,
    ) -> PositionState:
        if side not in (-1, 1):
            raise ValueError("side must be -1 or 1")
        if qty <= 0:
            raise ValueError("qty must be positive")
        if avg_price <= 0:
            raise ValueError("avg_price must be positive")
        self.position = PositionState(side=side, qty=qty, avg_price=avg_price, entry_mode=entry_mode, entry_ts_ns=now_ns)
        self.entry_order_active = False
        self._working_entry_side = 0
        self._working_entry_price = 0.0
        self._open_trade_realized_pnl = 0.0
        self._partial_loss_counted_for_position = False
        if manage_exit:
            self.lollipop.on_entry_fill(avg_price, entry_mode, now_ns, entry_side=side)
        else:
            self.lollipop.reset()
        return self.position

    def restore_daily_pnl(self, pnl: float, now_ns: int = 0) -> None:
        """Restore today's realized PnL from broker account summary at startup.

        Syncs both the risk manager (daily loss gate) and the metrics collector
        so ``metrics.to_dict()`` reflects the real session state from the start.

        Typical startup sequence::

            strategy.restore_position(side, qty, avg_price, entry_mode, now_ns)
            strategy.restore_daily_pnl(pnl=today_net_pnl, now_ns=now_ns)
            # Feed in-flight BrokerOrderEvent(status=WORKING) for open orders
        """
        self.risk.restore_daily_pnl(pnl, now_ns)
        self.metrics.realized_pnl = float(pnl)

    def reconcile_from_broker(
        self,
        snapshot: BrokerReconciliationSnapshot,
        *,
        now_ns: int = 0,
        manage_exit: bool = True,
    ) -> dict[str, int | float | bool]:
        return reconcile_strategy_from_broker(self, snapshot, now_ns=now_ns, manage_exit=manage_exit)

    def on_api_error(self, now_ns: int = 0) -> bool:
        opened = self.risk.record_api_error(now_ns)
        if opened:
            self.metrics.record_api_circuit_open()
        return opened

    def on_api_success(self) -> None:
        self.risk.record_api_success()

    def on_rest_latency(self, request_kind: str, latency_ms: float, now_ns: int = 0) -> bool:
        self.metrics.record_rest_latency(request_kind, latency_ms)
        opened = self.risk.record_latency(request_kind, latency_ms, now_ns)
        if opened:
            self.metrics.record_latency_circuit_open()
        return opened

    def _apply_broker_fill(self, order: OrderState, qty: int, price: float, now_ns: int = 0) -> str:
        if qty <= 0:
            return "none"
        outcome = self._apply_position_fill(
            side=order.intent.side,
            qty=qty,
            price=price,
            now_ns=now_ns,
            entry_mode=order.intent.strategy,
            order_reason=order.intent.reason,
        )
        self.metrics.record_fill(order, outcome)
        return outcome

    def _apply_position_fill(
        self,
        *,
        side: int,
        qty: int,
        price: float,
        now_ns: int = 0,
        entry_mode: str = "",
        order_reason: str = "",
    ) -> str:
        if qty <= 0:
            return "none"

        if self.position.qty == 0:
            self.position.side = side
            self.position.qty = qty
            self.position.avg_price = price
            self.position.entry_mode = entry_mode
            self.position.entry_ts_ns = now_ns
            self._open_trade_realized_pnl = 0.0
            self._partial_loss_counted_for_position = False
            # Capture the signal at entry time for the journal
            self._last_entry_signal = (
                self.last_result.signal if self.last_result is not None else None
            )
            self.lollipop.on_entry_fill(price, entry_mode, now_ns, entry_side=side)
            return "entry"

        if self.position.side == side:
            new_qty = self.position.qty + qty
            self.position.avg_price = (self.position.avg_price * self.position.qty + price * qty) / new_qty
            self.position.qty = new_qty
            self._partial_loss_counted_for_position = False
            self.lollipop.on_scale_in_fill(
                self.position.avg_price,
                self.position.entry_mode or entry_mode,
                entry_side=self.position.side,
            )
            return "entry"

        prev_avg = self.position.avg_price
        prev_side = self.position.side
        entry_ts_ns = self.position.entry_ts_ns
        self.position.qty = max(0, self.position.qty - qty)
        if self.position.qty == 0:
            gross_pnl = (price - prev_avg) * qty * prev_side
            final_net_pnl = gross_pnl - self.risk.estimate_round_trip_cost(qty)
            total_trade_pnl = self._open_trade_realized_pnl + final_net_pnl
            update_loss_streak = total_trade_pnl > 0 or not self._partial_loss_counted_for_position
            net_pnl = self.risk.record_trade_result(
                gross_pnl > 0,
                now_ns,
                pnl=gross_pnl,
                qty=qty,
                classification_pnl=total_trade_pnl,
                update_loss_streak=update_loss_streak,
            )
            self.metrics.record_trade_close(
                pnl=net_pnl,
                hold_ns=now_ns - entry_ts_ns if now_ns > 0 else 0,
                classification_pnl=total_trade_pnl,
            )
            entry_mode_for_log = self.position.entry_mode
            self.position = PositionState()
            self._open_trade_realized_pnl = 0.0
            self._partial_loss_counted_for_position = False
            self.lollipop.on_exit_fill()
            if self.journal is not None:
                self.journal.on_trade_closed(
                    entry_ts_ns=entry_ts_ns,
                    exit_ts_ns=now_ns,
                    side=prev_side,
                    qty=qty,
                    entry_price=prev_avg,
                    exit_price=price,
                    exit_reason=order_reason or (self.last_result.blocked_reason if self.last_result else ""),
                    entry_mode=entry_mode_for_log,
                    signal=self._last_entry_signal,
                    realized_pnl=net_pnl,
                )
            return "exit"
        # Partial exit: realize PnL for the exited portion immediately so that
        # daily loss limits and realized PnL metrics stay current.
        gross_pnl = (price - prev_avg) * qty * prev_side
        should_count_partial_loss = not self._partial_loss_counted_for_position
        net_pnl = self.risk.record_partial_pnl(
            pnl=gross_pnl,
            qty=qty,
            now_ns=now_ns,
            count_loss=should_count_partial_loss,
        )
        if net_pnl < 0:
            self._partial_loss_counted_for_position = True
        self._open_trade_realized_pnl += net_pnl
        self.metrics.record_partial_exit(pnl=net_pnl)
        return "partial_exit"

    @property
    def working_entry_ids(self) -> list[str]:
        """Client order IDs of all active (non-final) entry orders.

        The execution layer uses this to send cancel requests to the broker
        or simulator when entry_cancel_signal fires.
        """
        return [o.client_order_id for o in self.orders.active_by_role(ORDER_ROLE_ENTRY)]

    @property
    def working_exit_ids(self) -> list[str]:
        """Client order IDs of all active (non-final) exit orders."""
        return [o.client_order_id for o in self.orders.active_by_role(ORDER_ROLE_EXIT)]

    def release_deferred_force_exit(self, snapshot: BoardSnapshot, *, now_ns: int) -> OrderIntent | None:
        """Release a force-exit intent after active exit orders have been cleared."""
        if self.position.qty <= 0 or self.working_exit_ids:
            return None
        action = self.lollipop.tick(
            snapshot,
            self.position,
            now_ns,
            symbol=self.config.symbol,
            exchange=self.config.exchange,
        )
        if action.action != "force_exit" or action.intent is None:
            return None
        intent = self._track_intent(action.intent, role=ORDER_ROLE_EXIT, now_ns=now_ns)
        self.metrics.record_exit_intent(intent)
        return intent

    def clear_entry_order(self) -> None:
        for order in self.orders.active_by_role(ORDER_ROLE_ENTRY):
            self.orders.mark_cancel_pending(order.client_order_id, reason="clear_entry_order")
        self._refresh_working_entry_state()

    def _track_intent(self, intent: OrderIntent, *, role: str, now_ns: int) -> OrderIntent:
        order = self.orders.add_intent(intent, role=role, now_ns=now_ns)
        if role == ORDER_ROLE_ENTRY:
            self.entry_order_active = True
            self._working_entry_side = order.intent.side
            self._working_entry_price = order.intent.price
        return order.intent

    def _refresh_working_entry_state(self) -> None:
        active_entries = self.orders.active_by_role(ORDER_ROLE_ENTRY)
        self.entry_order_active = bool(active_entries)
        if not active_entries:
            self._working_entry_side = 0
            self._working_entry_price = 0.0
            return
        latest = active_entries[-1]
        self._working_entry_side = latest.intent.side
        self._working_entry_price = latest.intent.price

    def _handle_final_order_state(self, order: OrderState, now_ns: int = 0) -> None:
        if not order.is_final or order.role != ORDER_ROLE_EXIT or self.position.qty <= 0:
            return
        if order.status == OrderStatus.CANCELED:
            if self.lollipop.phase == LollipopPhase.TIMEOUT:
                # Force-exit was cancelled — allow re-emission on next tick
                self.lollipop.reset_force_exit()
            else:
                self.lollipop.reschedule(now_ns)
        elif order.status == OrderStatus.REJECTED:
            if self.lollipop.phase == LollipopPhase.TIMEOUT:
                # Already in TIMEOUT: reset flag so next tick re-emits force_exit.
                # force_exit_next_tick() is a no-op here and leaves force_exit_requested=True,
                # which would permanently block re-emission.
                self.lollipop.reset_force_exit()
            else:
                self.lollipop.force_exit_next_tick()

    def _choose_decision(
        self,
        snapshot: BoardSnapshot,
        signal,
        now_ns: int = 0,
        market_state: MarketState = MarketState.NORMAL,
    ) -> EntryDecision:
        taker_decision = self.taker.evaluate(snapshot, signal, now_ns=now_ns)
        if taker_decision.allow:
            return taker_decision
        maker_decision = self.maker.evaluate(snapshot, signal, market_state=market_state)
        if maker_decision.allow:
            return maker_decision
        return maker_decision if maker_decision.reason != "maker_no_direction" else taker_decision
