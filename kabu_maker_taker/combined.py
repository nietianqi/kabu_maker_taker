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

from dataclasses import dataclass

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
    MakerQuoteDiagnostics,
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
    ENTRY_MODE_MAKER,
    ENTRY_MODE_TAKER,
    MakerStrategy,
    MarketStateDetector,
    ORDER_ROLE_ENTRY,
    ORDER_ROLE_EXIT,
    TakerStrategy,
)


@dataclass(frozen=True, slots=True)
class EntrySelection:
    decision: EntryDecision
    setup_type: str = ""
    selection_reason: str = ""
    maker_decision: EntryDecision | None = None
    taker_decision: EntryDecision | None = None
    maker_trigger: str = ""
    taker_trigger: str = ""
    maker_edge_ticks: float = 0.0
    taker_exec_quality: int = 0
    maker_diagnostics: MakerQuoteDiagnostics | None = None


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
        self.market_state_detector = MarketStateDetector(
            config.market_state,
            config.tick_size,
            stale_quote_ms=config.risk.stale_quote_ms,
        )
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

    def _market_result_fields(self) -> dict[str, object]:
        diagnostics = self.market_state_detector.last_diagnostics
        return {
            "market_state": diagnostics.state,
            "market_state_reason": diagnostics.reason,
            "market_state_spread_ticks": diagnostics.spread_ticks,
            "market_state_event_rate_hz": diagnostics.event_rate_hz,
            "market_state_stale_ms": diagnostics.stale_ms,
            "market_state_jump_ticks": diagnostics.jump_ticks,
            "market_state_trade_lag_ms": diagnostics.trade_lag_ms,
        }

    def _maker_result_fields(self, diagnostics: MakerQuoteDiagnostics | None) -> dict[str, object]:
        if diagnostics is None:
            return {}
        return {
            "maker_quote_mode": diagnostics.quote_mode,
            "maker_fair_price": diagnostics.fair_price,
            "maker_reservation_price": diagnostics.reservation_price,
            "maker_edge_ticks": diagnostics.edge_ticks,
            "maker_half_spread_ticks": diagnostics.half_spread_ticks,
            "maker_queue_threshold": diagnostics.queue_threshold,
            "maker_top_queue_qty": diagnostics.top_queue_qty,
            "maker_working_age_ms": diagnostics.working_age_ms,
        }

    def _selection_result_fields(self, selection: EntrySelection | None) -> dict[str, object]:
        if selection is None:
            return {}
        maker_decision = selection.maker_decision
        taker_decision = selection.taker_decision
        return {
            "setup_type": selection.setup_type,
            "selection_reason": selection.selection_reason,
            "maker_candidate_allow": maker_decision.allow if maker_decision else False,
            "maker_candidate_reason": maker_decision.reason if maker_decision else "",
            "maker_candidate_score": maker_decision.entry_score if maker_decision else 0,
            "maker_candidate_trigger": selection.maker_trigger,
            "maker_candidate_edge_ticks": selection.maker_edge_ticks,
            "taker_candidate_allow": taker_decision.allow if taker_decision else False,
            "taker_candidate_reason": taker_decision.reason if taker_decision else "",
            "taker_candidate_score": taker_decision.entry_score if taker_decision else 0,
            "taker_candidate_trigger": selection.taker_trigger,
            "taker_candidate_exec_quality": selection.taker_exec_quality,
        }

    def on_board(self, snapshot: BoardSnapshot, *, now_ns: int | None = None) -> StrategyResult:
        ts = now_ns if now_ns is not None else snapshot.ts_ns

        if snapshot.symbol != self.config.symbol:
            result = StrategyResult(
                None,
                EntryDecision(False, "symbol_mismatch"),
                None,
                blocked_reason="symbol_mismatch",
                market_state_reason="symbol_mismatch",
            )
            self.last_result = result
            return result
        if snapshot.duplicate or snapshot.out_of_order:
            result = StrategyResult(
                None,
                EntryDecision(False, "duplicate_or_out_of_order"),
                None,
                blocked_reason="duplicate_or_out_of_order",
                market_state_reason="duplicate_or_out_of_order",
            )
            self.last_result = result
            return result

        board_stale = self.risk.is_stale_board(snapshot.ts_ns)
        self.risk.update_board_ts(snapshot.ts_ns)
        market_state = self.market_state_detector.update(snapshot, ts)
        market_fields = self._market_result_fields()
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
        exit_blocked_reason = ""
        if lollipop_action.intent is not None:
            exit_allowed, maybe_exit_blocked_reason = self.risk.can_exit_without_loss(
                intent=lollipop_action.intent,
                position=self.position,
                max_slip_ticks=self.config.strategy.max_slip_ticks,
                snapshot=snapshot,
            )
            if not exit_allowed:
                exit_blocked_reason = maybe_exit_blocked_reason
                self.metrics.record_risk_block("loss_exit_blocked")
                if lollipop_action.action == "force_exit":
                    self.lollipop.reset_force_exit()
            elif lollipop_action.action == "force_exit" and self.orders.active_by_role(ORDER_ROLE_EXIT):
                exit_cancel_signal = "replace_active_exit_before_force_exit"
            else:
                exit_intent = self._track_intent(lollipop_action.intent, role=ORDER_ROLE_EXIT, now_ns=ts)
                self.metrics.record_exit_intent(exit_intent)

        if self.entry_order_active:
            entry_cancel_signal = ""
            entry_cancel_blocked_reason = ""
            maker_diag: MakerQuoteDiagnostics | None = None
            if self._working_entry_side != 0:
                entry_orders = self.orders.active_by_role(ORDER_ROLE_ENTRY)
                order_age_ns = ts - entry_orders[-1].submitted_ts_ns if entry_orders else 0
                working_age_ms = max(order_age_ns / 1_000_000, 0.0)
                desired_intent, maker_diag = self.maker.preview_quote(
                    symbol=self.config.symbol,
                    exchange=self.config.exchange,
                    tick_size=self.config.tick_size,
                    lot_size=self.config.lot_size,
                    qty=self.config.strategy.trade_qty,
                    snapshot=snapshot,
                    decision=EntryDecision(
                        True,
                        "",
                        entry_mode=ENTRY_MODE_MAKER,
                        side=self._working_entry_side,
                    ),
                    signal=signal,
                    position=self.position,
                    max_inventory_qty=self.risk.config.max_inventory_qty,
                    market_state=market_state,
                    working_age_ms=working_age_ms,
                )
                desired_price = desired_intent.price
                raw_cancel_signal = self.maker.calc_cancel_reason(
                    signal,
                    self._working_entry_side,
                    self._working_entry_price,
                    market_state,
                    current_spread=snapshot.spread,
                    order_age_ns=order_age_ns,
                    desired_price=desired_price,
                    board_stale=board_stale,
                    same_side_top_qty=maker_diag.top_queue_qty if maker_diag is not None else 0,
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
                **market_fields,
                **self._maker_result_fields(maker_diag),
            )
            self.last_result = result
            return result

        if self.position.qty > 0 and self.lollipop.is_busy:
            result = StrategyResult(
                None,
                EntryDecision(False, exit_blocked_reason or "lollipop_active"),
                signal,
                blocked_reason=exit_blocked_reason or "lollipop_active",
                exit_intent=exit_intent,
                exit_cancel_signal=exit_cancel_signal,
                **market_fields,
            )
            self.last_result = result
            return result

        selection = self._select_entry(snapshot, signal, ts, market_state)
        decision = selection.decision
        selection_fields = self._selection_result_fields(selection)
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
                **market_fields,
                **selection_fields,
                **self._maker_result_fields(selection.maker_diagnostics),
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
                **market_fields,
                **selection_fields,
                **self._maker_result_fields(selection.maker_diagnostics),
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
            market_state=market_state,
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
                **market_fields,
                **selection_fields,
                **self._maker_result_fields(selection.maker_diagnostics),
            )
            self.last_result = result
            return result

        maker_diag: MakerQuoteDiagnostics | None = None
        if decision.entry_mode == ENTRY_MODE_TAKER:
            intent = self.taker.build_intent(
                symbol=self.config.symbol,
                exchange=self.config.exchange,
                lot_size=self.config.lot_size,
                qty=base_qty,
                snapshot=snapshot,
                decision=decision,
                setup_type=selection.setup_type,
                selection_reason=selection.selection_reason,
            )
        else:
            intent, maker_diag = self.maker.preview_quote(
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
                market_state=market_state,
                setup_type=selection.setup_type,
                selection_reason=selection.selection_reason,
            )
            min_edge = self.config.strategy.maker_min_edge_ticks
            if min_edge > 0 and maker_diag.edge_ticks < min_edge:
                self.confirmation.reset()
                result = StrategyResult(
                    None,
                    decision,
                    signal,
                    blocked_reason="maker_edge_too_low",
                    confirm_progress=progress,
                    exit_intent=exit_intent,
                    exit_cancel_signal=exit_cancel_signal,
                    **market_fields,
                    **selection_fields,
                    **self._maker_result_fields(maker_diag),
                )
                self.last_result = result
                return result

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
            **market_fields,
            **selection_fields,
            **self._maker_result_fields(maker_diag),
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

    def consistency_issues(self) -> list[dict[str, object]]:
        issues: list[dict[str, object]] = []

        def add(code: str, severity: str, message: str, *, order_id: str = "") -> None:
            issue: dict[str, object] = {"code": code, "severity": severity, "message": message}
            if order_id:
                issue["order_id"] = order_id
            issues.append(issue)

        if self.position.qty == 0 and self.position.side != 0:
            add("flat_position_side", "high", "position side must be zero when quantity is flat")
        if self.position.qty > 0 and self.position.side not in {-1, 1}:
            add("open_position_side", "high", "position side must be +/-1 when quantity is open")
        if self.position.qty > self.config.risk.max_inventory_qty:
            add("position_inventory_limit", "high", "position quantity exceeds risk.max_inventory_qty")

        for order_id, order in self.orders.snapshot().items():
            intent = order.get("intent", {})
            intent_qty = int(intent.get("qty", 0) or 0)
            cum_qty = int(order.get("cum_qty", 0) or 0)
            role = str(order.get("role", ""))
            side = int(intent.get("side", 0) or 0)
            qty = int(intent.get("qty", 0) or 0)
            if cum_qty > intent_qty:
                add("order_cum_qty_exceeds_intent", "high", "order cumulative quantity exceeds intent quantity", order_id=order_id)
            if role not in {ORDER_ROLE_ENTRY, ORDER_ROLE_EXIT}:
                add("order_unknown_role", "medium", "order has an unknown role", order_id=order_id)
            if order.get("status") == OrderStatus.UNKNOWN.value:
                add("order_unknown_status", "medium", "order status is unknown", order_id=order_id)
            if order.get("status") in {OrderStatus.FILLED.value, OrderStatus.CANCELED.value, OrderStatus.REJECTED.value}:
                continue
            if role == ORDER_ROLE_EXIT:
                if self.position.qty <= 0:
                    add("exit_without_position", "high", "active exit order exists while local position is flat", order_id=order_id)
                elif side != -self.position.side:
                    add("exit_not_reducing_position", "high", "active exit order side does not reduce local position", order_id=order_id)
                elif qty > self.position.qty:
                    add("exit_qty_exceeds_position", "high", "active exit order quantity exceeds local position", order_id=order_id)
            elif role == ORDER_ROLE_ENTRY and self.position.qty > 0 and side not in {0, self.position.side}:
                add("entry_opposes_position", "high", "active entry order side opposes local position", order_id=order_id)
        return issues

    def status_snapshot(self) -> dict[str, object]:
        issues = self.consistency_issues()
        return {
            "symbol": self.config.symbol,
            "exchange": self.config.exchange,
            "position": {
                "side": self.position.side,
                "qty": self.position.qty,
                "avg_price": self.position.avg_price,
                "entry_mode": self.position.entry_mode,
                "entry_ts_ns": self.position.entry_ts_ns,
            },
            "active_orders": [order.to_dict() for order in self.orders.active()],
            "orders": self.orders.snapshot(),
            "metrics": self.metrics.to_dict(),
            "risk": {
                "daily_pnl": self.risk.daily_pnl,
                "api_cooling_until_ns": self.risk.api_cooling_until_ns,
                "latency_circuit_open_until_ns": self.risk.latency_circuit_open_until_ns,
                "submit_latency_last_ms": self.risk.last_latency_ms("submit"),
                "cancel_latency_last_ms": self.risk.last_latency_ms("cancel"),
                "poll_latency_last_ms": self.risk.last_latency_ms("poll"),
                "submit_latency_breach_count": self.risk.latency_breach_count("submit"),
                "cancel_latency_breach_count": self.risk.latency_breach_count("cancel"),
                "poll_latency_breach_count": self.risk.latency_breach_count("poll"),
            },
            "consistency": {
                "ok": not any(issue.get("severity") == "high" for issue in issues),
                "issue_count": len(issues),
                "high_issue_count": sum(1 for issue in issues if issue.get("severity") == "high"),
                "issues": issues,
            },
        }

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
        exit_allowed, _reason = self.risk.can_exit_without_loss(
            intent=action.intent,
            position=self.position,
            max_slip_ticks=self.config.strategy.max_slip_ticks,
            snapshot=snapshot,
        )
        if not exit_allowed:
            self.metrics.record_risk_block("loss_exit_blocked")
            self.lollipop.reset_force_exit()
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
        return self._select_entry(snapshot, signal, now_ns, market_state).decision

    def _select_entry(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        now_ns: int = 0,
        market_state: MarketState = MarketState.NORMAL,
    ) -> EntrySelection:
        override = self.__dict__.get("_choose_decision")
        if override is not None:
            decision = override(snapshot, signal, now_ns=now_ns, market_state=market_state)
            return EntrySelection(
                decision=decision,
                setup_type=self._setup_type_for_decision(decision, ""),
                selection_reason="legacy_override",
            )

        taker_decision = self.taker.evaluate(snapshot, signal, now_ns=now_ns)
        maker_decision = self.maker.evaluate(snapshot, signal, market_state=market_state)
        maker_diag = self._preview_maker_candidate(snapshot, signal, maker_decision, market_state)
        maker_edge = maker_diag.edge_ticks if maker_diag is not None else 0.0
        maker_trigger = self._maker_setup_type(maker_diag) if maker_diag is not None else ""
        taker_trigger = (
            self.taker.classify_entry_trigger(snapshot, signal, taker_decision.side)
            if taker_decision.side != 0
            else ""
        )
        taker_exec_quality = (
            self.taker.exec_quality_score(snapshot, signal, taker_decision.side)
            if taker_decision.side != 0
            else 0
        )
        policy = self.config.strategy.entry_selection_policy.strip().lower()
        if policy not in {"adaptive", "taker_priority", "maker_priority"}:
            policy = "adaptive"

        if policy == "taker_priority":
            if taker_decision.allow:
                return self._entry_selection(
                    taker_decision,
                    "taker_priority",
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )
            if maker_decision.allow:
                return self._entry_selection(
                    maker_decision,
                    "only_maker_allowed",
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )

        elif policy == "maker_priority":
            if maker_decision.allow:
                return self._entry_selection(
                    maker_decision,
                    "maker_priority",
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )
            if taker_decision.allow:
                return self._entry_selection(
                    taker_decision,
                    "only_taker_allowed",
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )

        else:
            maker_floor = max(
                self.config.strategy.maker_min_edge_ticks,
                self.config.strategy.adaptive_maker_min_edge_ticks,
            )
            maker_viable = maker_decision.allow and maker_edge >= maker_floor
            taker_urgency = self._taker_urgency_score(taker_decision, taker_trigger, taker_exec_quality)
            taker_urgent = (
                taker_decision.allow
                and taker_urgency >= max(self.config.strategy.adaptive_taker_urgency_score, 1)
            )
            if maker_viable and taker_urgent:
                return self._entry_selection(
                    taker_decision,
                    "taker_urgent",
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )
            if maker_viable:
                reason = "only_maker_allowed" if not taker_decision.allow else "maker_edge_better"
                return self._entry_selection(
                    maker_decision,
                    reason,
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )
            if taker_decision.allow:
                reason = "only_taker_allowed" if not maker_decision.allow else "maker_edge_too_low"
                return self._entry_selection(
                    taker_decision,
                    reason,
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )
            if maker_decision.allow:
                blocked = EntryDecision(
                    False,
                    "maker_edge_too_low",
                    entry_mode=ENTRY_MODE_MAKER,
                    side=maker_decision.side,
                    entry_score=maker_decision.entry_score,
                    required_confirm=maker_decision.required_confirm,
                )
                return self._entry_selection(
                    blocked,
                    "maker_edge_too_low",
                    taker_decision=taker_decision,
                    maker_decision=maker_decision,
                    maker_diag=maker_diag,
                    maker_trigger=maker_trigger,
                    taker_trigger=taker_trigger,
                    taker_exec_quality=taker_exec_quality,
                )

        blocked_decision = maker_decision if maker_decision.reason != "maker_no_direction" else taker_decision
        return self._entry_selection(
            blocked_decision,
            "both_blocked",
            taker_decision=taker_decision,
            maker_decision=maker_decision,
            maker_diag=maker_diag,
            maker_trigger=maker_trigger,
            taker_trigger=taker_trigger,
            taker_exec_quality=taker_exec_quality,
        )

    def _preview_maker_candidate(
        self,
        snapshot: BoardSnapshot,
        signal: SignalPacket,
        decision: EntryDecision,
        market_state: MarketState,
    ) -> MakerQuoteDiagnostics | None:
        if not decision.allow:
            return None
        _, diagnostics = self.maker.preview_quote(
            symbol=self.config.symbol,
            exchange=self.config.exchange,
            tick_size=self.config.tick_size,
            lot_size=self.config.lot_size,
            qty=self.config.strategy.trade_qty,
            snapshot=snapshot,
            decision=decision,
            signal=signal,
            position=self.position,
            max_inventory_qty=self.risk.config.max_inventory_qty,
            market_state=market_state,
        )
        return diagnostics

    def _entry_selection(
        self,
        decision: EntryDecision,
        selection_reason: str,
        *,
        taker_decision: EntryDecision,
        maker_decision: EntryDecision,
        maker_diag: MakerQuoteDiagnostics | None,
        maker_trigger: str,
        taker_trigger: str,
        taker_exec_quality: int,
    ) -> EntrySelection:
        setup_type = self._setup_type_for_decision(decision, taker_trigger, maker_diag=maker_diag)
        if not decision.allow:
            setup_type = self._blocked_setup_type(decision.reason)
        return EntrySelection(
            decision=decision,
            setup_type=setup_type,
            selection_reason=selection_reason,
            maker_decision=maker_decision,
            taker_decision=taker_decision,
            maker_trigger=maker_trigger,
            taker_trigger=taker_trigger,
            maker_edge_ticks=maker_diag.edge_ticks if maker_diag is not None else 0.0,
            taker_exec_quality=taker_exec_quality,
            maker_diagnostics=maker_diag,
        )

    def _setup_type_for_decision(
        self,
        decision: EntryDecision,
        taker_trigger: str,
        *,
        maker_diag: MakerQuoteDiagnostics | None = None,
    ) -> str:
        if decision.entry_mode == ENTRY_MODE_MAKER:
            return self._maker_setup_type(maker_diag)
        if decision.entry_mode == ENTRY_MODE_TAKER:
            return self._taker_setup_type(taker_trigger, decision)
        return self._blocked_setup_type(decision.reason)

    def _maker_setup_type(self, diagnostics: MakerQuoteDiagnostics | None) -> str:
        if diagnostics is not None and diagnostics.quote_mode == "QUEUE_DEFENSE":
            return "maker_queue_defense"
        return "maker_passive_fair"

    def _taker_setup_type(self, trigger: str, decision: EntryDecision) -> str:
        mapping = {
            "depth_breakout": "taker_depth_thin",
            "depth_thin": "taker_depth_thin",
            "wall_break": "taker_wall_break",
            "cancel_imbalance": "taker_cancel_imbalance",
            "price_breakout": "taker_price_breakout",
            "vol_expansion": "taker_vol_expansion",
        }
        if trigger in mapping:
            return mapping[trigger]
        return "taker_quality_urgent" if decision.allow else self._blocked_setup_type(decision.reason)

    def _blocked_setup_type(self, reason: str) -> str:
        token = (reason or "unknown").split(":", 1)[0].strip().lower()
        token = "".join(ch if ch.isalnum() else "_" for ch in token).strip("_") or "unknown"
        return f"blocked_{token}"

    def _taker_urgency_score(self, decision: EntryDecision, trigger: str, exec_quality: int) -> int:
        if not decision.allow:
            return 0
        score = 0
        if trigger in {"depth_breakout", "depth_thin", "wall_break", "cancel_imbalance", "price_breakout", "vol_expansion"}:
            score += 2
        if exec_quality >= max(self.config.strategy.exec_quality_min_score, 8):
            score += 1
        aggressive_threshold = self.config.strategy.aggressive_taker_entry_score
        if aggressive_threshold > 0:
            if decision.entry_score >= aggressive_threshold:
                score += 1
        elif decision.entry_score >= self.config.strategy.taker_score_threshold + 2:
            score += 1
        return score
