from __future__ import annotations

import unittest

from kabu_maker_taker.models import BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderStatus
from kabu_maker_taker.orders import OrderLedger


def _intent(qty: int = 100, price: float = 100.0, side: int = 1) -> OrderIntent:
    return OrderIntent(
        symbol="9984",
        exchange=27,
        side=side,
        qty=qty,
        price=price,
        is_market=False,
        strategy="maker",
        reason="test",
        score=5,
        reference_price=price,
        client_order_id="ORD-1",
    )


class FillDeduplicationTests(unittest.TestCase):
    def _ledger_with_working_order(self) -> tuple[OrderLedger, str]:
        ledger = OrderLedger()
        order = ledger.add_intent(_intent(), role="entry")
        ledger.apply_order_event(
            BrokerOrderEvent(order_id=order.client_order_id, status=OrderStatus.WORKING)
        )
        return ledger, order.client_order_id

    # ------------------------------------------------------------------ #
    def test_duplicate_trade_id_ignored_on_second_apply(self) -> None:
        """Same trade_id applied twice must not double-count cum_qty."""
        ledger, oid = self._ledger_with_working_order()
        fill = BrokerFillEvent(order_id=oid, qty=50, price=100.0, ts_ns=1, trade_id="T-001")

        _, qty1, _ = ledger.apply_fill_event(fill)
        _, qty2, _ = ledger.apply_fill_event(fill)

        self.assertEqual(qty1, 50)
        self.assertEqual(qty2, 0)   # duplicate — must be silently discarded
        self.assertEqual(ledger.get(oid).cum_qty, 50)  # type: ignore[union-attr]

    def test_different_trade_ids_both_applied(self) -> None:
        """Two fills with different trade_ids must both be counted."""
        ledger, oid = self._ledger_with_working_order()
        f1 = BrokerFillEvent(order_id=oid, qty=30, price=100.0, ts_ns=1, trade_id="T-001")
        f2 = BrokerFillEvent(order_id=oid, qty=40, price=100.0, ts_ns=2, trade_id="T-002")

        _, q1, _ = ledger.apply_fill_event(f1)
        _, q2, _ = ledger.apply_fill_event(f2)

        self.assertEqual(q1, 30)
        self.assertEqual(q2, 40)
        self.assertEqual(ledger.get(oid).cum_qty, 70)  # type: ignore[union-attr]

    def test_empty_trade_id_uses_composite_key_dedup(self) -> None:
        """trade_id='' falls back to ts_ns:qty:price composite key.
        Replaying the exact same event twice must count only once."""
        ledger, oid = self._ledger_with_working_order()
        f = BrokerFillEvent(order_id=oid, qty=20, price=100.0, ts_ns=1)  # trade_id=""

        _, q1, _ = ledger.apply_fill_event(f)
        _, q2, _ = ledger.apply_fill_event(f)  # identical replay — same composite key

        # Second fill is a duplicate; only the first fill counts
        self.assertEqual(q1, 20)
        self.assertEqual(q2, 0)
        self.assertEqual(ledger.get(oid).cum_qty, 20)  # type: ignore[union-attr]

    def test_fill_ids_cleaned_up_when_order_pruned_from_history(self) -> None:
        """_order_fill_ids must not grow unboundedly; entries removed when order pruned."""
        ledger = OrderLedger(max_final_history=1)
        # Create and fully fill two orders so the first gets pruned
        for seq in range(2):
            oid_str = f"ORD-{seq}"
            intent = OrderIntent(
                symbol="9984", exchange=27, side=1, qty=10, price=100.0,
                is_market=False, strategy="maker", reason="t", score=1,
                reference_price=100.0, client_order_id=oid_str,
            )
            order = ledger.add_intent(intent, role="entry")
            fill = BrokerFillEvent(order_id=order.client_order_id, qty=10, price=100.0,
                                   ts_ns=seq, trade_id=f"T-{seq}")
            ledger.apply_fill_event(fill)

        # After pruning the first order, its fill IDs must be gone
        self.assertNotIn("ORD-0", ledger._order_fill_ids)
        # Second order's fill IDs are still tracked
        self.assertIn("ORD-1", ledger._order_fill_ids)


if __name__ == "__main__":
    unittest.main()
