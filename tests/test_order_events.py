from __future__ import annotations

import unittest
from pathlib import Path

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import (
    BoardSnapshot,
    BrokerFillEvent,
    BrokerOrderEvent,
    EntryDecision,
    Level,
    LollipopPhase,
    MarketState,
    OrderIntent,
    OrderStatus,
    SignalPacket,
)
from kabu_maker_taker.orders import OrderLedger


def _signal(**overrides) -> SignalPacket:
    values = {
        "ts_ns": 1_000_000_000,
        "obi_raw": 0.35,
        "lob_ofi_raw": 0.20,
        "tape_ofi_raw": 0.20,
        "micro_momentum_raw": 0.10,
        "microprice_tilt_raw": 0.30,
        "microprice": 100.3,
        "mid": 100.0,
        "obi_z": 0.0,
        "lob_ofi_z": 0.0,
        "tape_ofi_z": 0.0,
        "micro_momentum_z": 0.0,
        "microprice_tilt_z": 0.0,
        "composite": 0.50,
        "integrated_ofi": 0.20,
        "trade_burst_score": 0.10,
    }
    values.update(overrides)
    return SignalPacket(**values)


def _snapshot(ts_ns: int = 1_000_000_000, bid: float = 100.0, ask: float = 101.0) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=ts_ns,
        bid=bid,
        ask=ask,
        bid_size=500,
        ask_size=200,
        bids=(Level(bid, 500), Level(bid - 1.0, 300)),
        asks=(Level(ask, 200), Level(ask + 1.0, 250)),
    )


def _strategy() -> CombinedMakerTakerStrategy:
    config = AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=1,
        strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1),
        risk=RiskConfig(max_inventory_qty=300, max_spread_ticks=5.0),
    )
    strategy = CombinedMakerTakerStrategy(config)
    strategy.signals.on_board = lambda snapshot: _signal(ts_ns=snapshot.ts_ns)
    strategy._choose_decision = lambda snapshot, signal, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
        True,
        "",
        entry_mode="maker",
        side=1,
        entry_score=8,
        required_confirm=1,
    )
    return strategy


class BrokerOrderEventTests(unittest.TestCase):
    def test_intent_creates_order_but_does_not_change_position_until_broker_fill(self) -> None:
        strategy = _strategy()
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)

        self.assertIsNotNone(result.intent)
        assert result.intent is not None
        self.assertTrue(result.intent.client_order_id)
        self.assertEqual(strategy.position.qty, 0)
        order = strategy.orders.get(result.intent.client_order_id)
        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.status, OrderStatus.NEW_PENDING)
        self.assertTrue(strategy.entry_order_active)

        ack = strategy.on_broker_order_event(
            BrokerOrderEvent(
                order_id=result.intent.client_order_id,
                broker_order_id="B-1",
                status=OrderStatus.WORKING,
                ts_ns=1_000_000_100,
            )
        )
        self.assertEqual(ack, OrderStatus.WORKING.value)
        self.assertEqual(strategy.position.qty, 0)
        self.assertEqual(strategy.orders.get("B-1"), order)

        fill = strategy.on_broker_fill(
            BrokerFillEvent(order_id="B-1", qty=100, price=101.0, ts_ns=1_000_000_200)
        )
        self.assertEqual(fill, "entry")
        self.assertEqual(strategy.position.qty, 100)
        self.assertEqual(strategy.position.avg_price, 101.0)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertFalse(strategy.entry_order_active)
        self.assertTrue(strategy.lollipop.is_busy)

    def test_cumulative_broker_event_is_idempotent(self) -> None:
        strategy = _strategy()
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert result.intent is not None

        first = BrokerOrderEvent(
            order_id=result.intent.client_order_id,
            status=OrderStatus.PARTIALLY_FILLED,
            cum_qty=40,
            avg_fill_price=101.0,
            ts_ns=1_000_000_100,
        )
        self.assertEqual(strategy.on_broker_order_event(first), "entry")
        self.assertEqual(strategy.position.qty, 40)
        self.assertTrue(strategy.entry_order_active)

        duplicate = BrokerOrderEvent(
            order_id=result.intent.client_order_id,
            status=OrderStatus.PARTIALLY_FILLED,
            cum_qty=40,
            avg_fill_price=101.0,
            ts_ns=1_000_000_200,
        )
        self.assertEqual(strategy.on_broker_order_event(duplicate), OrderStatus.PARTIALLY_FILLED.value)
        self.assertEqual(strategy.position.qty, 40)

        final = BrokerOrderEvent(
            order_id=result.intent.client_order_id,
            status=OrderStatus.FILLED,
            cum_qty=100,
            avg_fill_price=101.2,
            ts_ns=1_000_000_300,
        )
        self.assertEqual(strategy.on_broker_order_event(final), "entry")
        self.assertEqual(strategy.position.qty, 100)
        self.assertAlmostEqual(strategy.position.avg_price, 101.2)
        self.assertFalse(strategy.entry_order_active)

    def test_zero_price_explicit_fill_is_ignored(self) -> None:
        strategy = _strategy()
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert result.intent is not None

        outcome = strategy.on_broker_fill(
            BrokerFillEvent(
                order_id=result.intent.client_order_id,
                qty=100,
                price=0.0,
                ts_ns=1_000_000_100,
            )
        )
        self.assertEqual(outcome, OrderStatus.NEW_PENDING.value)
        self.assertEqual(strategy.position.qty, 0)
        order = strategy.orders.get(result.intent.client_order_id)
        assert order is not None
        self.assertEqual(order.cum_qty, 0)
        self.assertEqual(order.avg_fill_price, 0.0)
        self.assertEqual(order.status, OrderStatus.NEW_PENDING)

    def test_rejected_entry_order_releases_working_entry_without_position_change(self) -> None:
        strategy = _strategy()
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert result.intent is not None

        outcome = strategy.on_broker_order_event(
            BrokerOrderEvent(
                order_id=result.intent.client_order_id,
                status=OrderStatus.REJECTED,
                reason="broker_reject",
                ts_ns=1_000_000_100,
            )
        )
        self.assertEqual(outcome, OrderStatus.REJECTED.value)
        self.assertEqual(strategy.position.qty, 0)
        self.assertFalse(strategy.entry_order_active)
        order = strategy.orders.get(result.intent.client_order_id)
        assert order is not None
        self.assertEqual(order.reject_reason, "broker_reject")

    def test_cancel_pending_order_can_still_fill_and_become_filled(self) -> None:
        strategy = _strategy()
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert result.intent is not None
        strategy.on_broker_order_event(
            BrokerOrderEvent(order_id=result.intent.client_order_id, status=OrderStatus.WORKING, ts_ns=1_000_000_010)
        )

        strategy.request_cancel(result.intent.client_order_id, reason="alpha_flip", now_ns=1_000_000_020)
        outcome = strategy.on_broker_fill(
            BrokerFillEvent(order_id=result.intent.client_order_id, qty=100, price=100.0, ts_ns=1_000_000_030)
        )

        order = strategy.orders.get(result.intent.client_order_id)
        assert order is not None
        self.assertEqual(outcome, "entry")
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(strategy.position.qty, 100)

    def test_partial_fill_then_cancel_keeps_fill_and_finalizes_remaining_qty(self) -> None:
        strategy = _strategy()
        result = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert result.intent is not None

        self.assertEqual(
            strategy.on_broker_order_event(
                BrokerOrderEvent(
                    order_id=result.intent.client_order_id,
                    status=OrderStatus.PARTIALLY_FILLED,
                    cum_qty=40,
                    avg_fill_price=100.0,
                    ts_ns=1_000_000_010,
                )
            ),
            "entry",
        )
        outcome = strategy.on_broker_order_event(
            BrokerOrderEvent(
                order_id=result.intent.client_order_id,
                status=OrderStatus.CANCELED,
                cum_qty=40,
                avg_fill_price=100.0,
                ts_ns=1_000_000_020,
                reason="cancel_ack",
            )
        )

        order = strategy.orders.get(result.intent.client_order_id)
        assert order is not None
        self.assertEqual(outcome, OrderStatus.CANCELED.value)
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.cum_qty, 40)
        self.assertEqual(strategy.position.qty, 40)
        self.assertFalse(strategy.entry_order_active)

    def test_exit_order_fill_closes_position_from_broker_event(self) -> None:
        strategy = _strategy()
        entry = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert entry.intent is not None
        strategy.on_broker_fill(
            BrokerFillEvent(order_id=entry.intent.client_order_id, qty=100, price=101.0, ts_ns=1_000_000_100)
        )

        exit_result = strategy.on_board(_snapshot(ts_ns=1_060_000_000, bid=101.0, ask=102.0), now_ns=1_060_000_000)
        self.assertIsNotNone(exit_result.exit_intent)
        assert exit_result.exit_intent is not None
        self.assertEqual(exit_result.exit_intent.strategy, "lollipop_tp")
        self.assertTrue(exit_result.exit_intent.client_order_id)

        outcome = strategy.on_broker_fill(
            BrokerFillEvent(
                order_id=exit_result.exit_intent.client_order_id,
                qty=100,
                price=103.0,
                ts_ns=1_060_000_100,
            )
        )
        self.assertEqual(outcome, "exit")
        self.assertEqual(strategy.position.qty, 0)
        self.assertFalse(strategy.lollipop.is_busy)

    def test_exit_tp_rejected_switches_to_force_exit_path(self) -> None:
        strategy = _strategy()
        entry = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert entry.intent is not None
        strategy.on_broker_fill(
            BrokerFillEvent(order_id=entry.intent.client_order_id, qty=100, price=101.0, ts_ns=1_000_000_100)
        )

        exit_result = strategy.on_board(_snapshot(ts_ns=1_060_000_000, bid=101.0, ask=102.0), now_ns=1_060_000_000)
        assert exit_result.exit_intent is not None
        outcome = strategy.on_broker_order_event(
            BrokerOrderEvent(
                order_id=exit_result.exit_intent.client_order_id,
                status=OrderStatus.REJECTED,
                reason="tp_rejected",
                ts_ns=1_060_000_100,
            )
        )
        self.assertEqual(outcome, OrderStatus.REJECTED.value)
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.TIMEOUT)

        force = strategy.on_board(_snapshot(ts_ns=1_060_000_200, bid=100.0, ask=101.0), now_ns=1_060_000_200)
        self.assertIsNotNone(force.exit_intent)
        assert force.exit_intent is not None
        self.assertTrue(force.exit_intent.is_market)
        self.assertEqual(force.exit_intent.reason, "timeout_exit")

    def test_exit_tp_canceled_reschedules_with_retry_budget(self) -> None:
        strategy = _strategy()
        entry = strategy.on_board(_snapshot(), now_ns=1_000_000_000)
        assert entry.intent is not None
        strategy.on_broker_fill(
            BrokerFillEvent(order_id=entry.intent.client_order_id, qty=100, price=101.0, ts_ns=1_000_000_100)
        )

        first_exit = strategy.on_board(_snapshot(ts_ns=1_060_000_000, bid=101.0, ask=102.0), now_ns=1_060_000_000)
        assert first_exit.exit_intent is not None
        strategy.on_broker_order_event(
            BrokerOrderEvent(
                order_id=first_exit.exit_intent.client_order_id,
                status=OrderStatus.CANCELED,
                reason="user_cancel",
                ts_ns=1_060_000_100,
            )
        )
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.SCHEDULED)

        second_exit = strategy.on_board(_snapshot(ts_ns=1_120_000_200, bid=101.0, ask=102.0), now_ns=1_120_000_200)
        self.assertIsNotNone(second_exit.exit_intent)
        assert second_exit.exit_intent is not None
        self.assertFalse(second_exit.exit_intent.is_market)
        self.assertEqual(second_exit.exit_intent.reason, "limit_tp")
        self.assertNotEqual(first_exit.exit_intent.client_order_id, second_exit.exit_intent.client_order_id)

    def test_manual_apply_fill_is_disabled(self) -> None:
        strategy = _strategy()
        with self.assertRaisesRegex(RuntimeError, "on_broker_fill"):
            strategy.apply_fill(side=1, qty=100, price=101.0, now_ns=1, entry_mode="maker")

    def test_final_order_history_is_trimmed_but_active_orders_remain(self) -> None:
        ledger = OrderLedger(max_final_history=2)

        def add_entry(order_no: int) -> str:
            order = ledger.add_intent(
                OrderIntent(
                    symbol="9984",
                    exchange=27,
                    side=1,
                    qty=100,
                    price=100.0,
                    is_market=False,
                    strategy="maker",
                    reason="test",
                    score=0,
                    reference_price=101.0,
                    client_order_id=f"entry-{order_no}",
                ),
                role="entry",
                now_ns=order_no,
            )
            return order.client_order_id

        first = add_entry(1)
        second = add_entry(2)
        active = add_entry(99)
        ledger.apply_order_event(BrokerOrderEvent(order_id=first, status=OrderStatus.FILLED, cum_qty=100, avg_fill_price=100.0))
        ledger.apply_order_event(BrokerOrderEvent(order_id=second, status=OrderStatus.FILLED, cum_qty=100, avg_fill_price=100.0))
        third = add_entry(3)
        ledger.apply_order_event(BrokerOrderEvent(order_id=third, status=OrderStatus.FILLED, cum_qty=100, avg_fill_price=100.0))

        self.assertIsNone(ledger.get(first))
        self.assertIsNotNone(ledger.get(second))
        self.assertIsNotNone(ledger.get(third))
        self.assertIsNotNone(ledger.get(active))
        self.assertIn(active, {order.client_order_id for order in ledger.active()})

    def test_combined_uses_role_constants_for_entry_tracking(self) -> None:
        text = Path(__file__).resolve().parents[1].joinpath("kabu_maker_taker", "combined.py").read_text(encoding="utf-8")
        self.assertNotIn('active_by_role("entry")', text)
        self.assertNotIn("role == \"entry\"", text)
        self.assertNotIn("role='entry'", text)


if __name__ == "__main__":
    unittest.main()
