from __future__ import annotations

from .config import AppConfig
from .lollipop import LollipopTPManager
from .models import (
    BoardSnapshot,
    BrokerFillEvent,
    BrokerOrderEvent,
    EntryDecision,
    MarketState,
    OrderIntent,
    OrderState,
    OrderStatus,
    PositionState,
    StrategyResult,
    TradePrint,
)
from .orders import OrderLedger
from .risk import RiskManager
from .signals import MicrostructureSignalEngine
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
        self.taker = TakerStrategy(config.strategy)
        self.risk = RiskManager(config=config.risk, tick_size=config.tick_size, lot_size=config.lot_size)
        self.confirmation = ConfirmationTracker()
        self.lollipop = LollipopTPManager(config.lollipop, config.tick_size, config.lot_size)
        self.market_state_detector = MarketStateDetector(config.market_state, config.tick_size)
        self.orders = OrderLedger()
        self.last_result: StrategyResult | None = None
        self.entry_order_active = False
        self._working_entry_side: int = 0
        self._working_entry_price: float = 0.0

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

        market_state = self.market_state_detector.update(snapshot, ts)
        signal = self.signals.on_board(snapshot)

        # Always tick the lollipop TP manager first — it manages exit for open positions
        lollipop_action = self.lollipop.tick(
            snapshot,
            self.position,
            ts,
            symbol=self.config.symbol,
            exchange=self.config.exchange,
        )
        exit_intent = lollipop_action.intent if lollipop_action.action != "none" else None
        if exit_intent is not None:
            exit_intent = self._track_intent(exit_intent, role=ORDER_ROLE_EXIT, now_ns=ts)

        if self.entry_order_active:
            entry_cancel_signal = self.maker.calc_cancel_reason(
                signal,
                self._working_entry_side,
                self._working_entry_price,
                market_state,
            ) if self._working_entry_side != 0 else ""
            result = StrategyResult(
                None,
                EntryDecision(False, "working_entry"),
                signal,
                blocked_reason="working_entry",
                exit_intent=exit_intent,
                entry_cancel_signal=entry_cancel_signal,
                market_state=market_state,
            )
            self.last_result = result
            return result

        # Block new entry while a position is open and lollipop is managing the exit
        if self.position.qty > 0 and self.lollipop.is_busy:
            result = StrategyResult(
                None,
                EntryDecision(False, "lollipop_active"),
                signal,
                blocked_reason="lollipop_active",
                exit_intent=exit_intent,
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
                market_state=market_state,
            )
            self.last_result = result
            return result

        base_qty = self.risk.order_qty(base_qty=self.config.strategy.trade_qty, position=self.position)
        # ATR-aware sizing: halve order qty when volatility is expanding
        if self.config.strategy.vol_aware_sizing and signal.vol_expansion and base_qty > self.config.lot_size:
            base_qty = max(self.config.lot_size, (base_qty // 2 // self.config.lot_size) * self.config.lot_size)
        if base_qty <= 0:
            self.confirmation.reset()
            result = StrategyResult(
                None,
                decision,
                signal,
                blocked_reason="qty_zero",
                confirm_progress=progress,
                exit_intent=exit_intent,
                market_state=market_state,
            )
            self.last_result = result
            return result

        expected_price = snapshot.ask if decision.side > 0 else snapshot.bid
        allowed, reason = self.risk.can_enter(
            snapshot=snapshot,
            decision=decision,
            position=self.position,
            now_ns=ts,
            expected_price=expected_price,
        )
        if not allowed:
            self.confirmation.reset()
            result = StrategyResult(
                None,
                decision,
                signal,
                blocked_reason=reason,
                confirm_progress=progress,
                exit_intent=exit_intent,
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
        # _track_intent sets entry_order_active, _working_entry_side, _working_entry_price
        # for both taker and maker paths — no redundant setter needed here.
        intent = self._track_intent(intent, role=ORDER_ROLE_ENTRY, now_ns=ts)
        result = StrategyResult(
            intent,
            decision,
            signal,
            confirm_progress=progress,
            exit_intent=exit_intent,
            market_state=market_state,
        )
        self.entry_order_active = True
        self.last_result = result
        return result

    def apply_fill(self, *, side: int, qty: int, price: float, now_ns: int = 0, entry_mode: str = "") -> str:
        raise RuntimeError("manual apply_fill is disabled; use on_broker_fill() or on_broker_order_event()")

    def on_broker_order_event(self, event: BrokerOrderEvent) -> str:
        """Apply a broker order snapshot/ack/reject/cancel event.

        Position changes happen only from fill deltas reported by this event.
        The event may use either the local client order id or the broker order id.
        """
        order, fill_qty, fill_price = self.orders.apply_order_event(event)
        if order is None:
            return "unknown_order"
        result = self._apply_broker_fill(order, fill_qty, fill_price, event.ts_ns) if fill_qty > 0 else order.status.value
        self._handle_final_order_state(order, event.ts_ns)
        self._refresh_working_entry_state()
        return result

    def on_broker_fill(self, event: BrokerFillEvent) -> str:
        """Apply an explicit broker fill event.

        This is the preferred path for live fills. Duplicate or over-sized fills
        are clamped by the order ledger before the position is touched.
        """
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

    def _apply_broker_fill(self, order: OrderState, qty: int, price: float, now_ns: int = 0) -> str:
        if qty <= 0:
            return "none"
        return self._apply_position_fill(
            side=order.intent.side,
            qty=qty,
            price=price,
            now_ns=now_ns,
            entry_mode=order.intent.strategy,
        )

    def _apply_position_fill(self, *, side: int, qty: int, price: float, now_ns: int = 0, entry_mode: str = "") -> str:
        if qty <= 0:
            return "none"

        # Flat → opening a new position (entry fill)
        if self.position.qty == 0:
            self.position.side = side
            self.position.qty = qty
            self.position.avg_price = price
            self.position.entry_mode = entry_mode
            self.position.entry_ts_ns = now_ns
            self.lollipop.on_entry_fill(price, entry_mode, now_ns, entry_side=side)
            return "entry"

        # Same side → scale-in (treat as entry, reschedule TP)
        if self.position.side == side:
            new_qty = self.position.qty + qty
            self.position.avg_price = (
                self.position.avg_price * self.position.qty + price * qty
            ) / new_qty
            self.position.qty = new_qty
            # Update TP target for the new average price
            self.lollipop.on_entry_fill(
                self.position.avg_price,
                self.position.entry_mode or entry_mode,
                now_ns,
                entry_side=self.position.side,
            )
            return "entry"

        # Opposite side → exit fill
        prev_avg = self.position.avg_price
        prev_side = self.position.side
        self.position.qty = max(0, self.position.qty - qty)
        if self.position.qty == 0:
            won = (price > prev_avg) if prev_side > 0 else (price < prev_avg)
            self.risk.record_trade_result(won, now_ns)
            self.position = PositionState()
            self.lollipop.on_exit_fill()
            return "exit"
        return "partial_exit"

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
            self.lollipop.reschedule(now_ns)
        elif order.status == OrderStatus.REJECTED:
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
