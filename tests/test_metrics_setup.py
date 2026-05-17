from __future__ import annotations

import unittest

from kabu_maker_taker.metrics import MetricsCollector
from kabu_maker_taker.models import BoardSnapshot, Level, OrderIntent, OrderState
from kabu_maker_taker.strategy import ENTRY_MODE_MAKER, ORDER_ROLE_ENTRY


class MetricsSetupTests(unittest.TestCase):
    def test_setup_counts_and_markout_do_not_break_legacy_counts(self) -> None:
        metrics = MetricsCollector(tick_size=1.0, markout_horizon_boards=1)
        intent = OrderIntent(
            symbol="9984",
            exchange=27,
            side=1,
            qty=100,
            price=100.0,
            is_market=False,
            strategy=ENTRY_MODE_MAKER,
            reason="maker_passive_edge",
            score=8,
            reference_price=100.0,
            setup_type="maker_passive_fair",
            selection_reason="maker_edge_better",
        )

        metrics.record_entry_intent(intent, now_ns=1_000_000_000)
        metrics.on_board(
            BoardSnapshot(
                symbol="9984",
                ts_ns=1_100_000_000,
                bid=100.0,
                ask=102.0,
                bid_size=100,
                ask_size=100,
                bids=(Level(100.0, 100),),
                asks=(Level(102.0, 100),),
            )
        )
        metrics.record_fill(OrderState("entry-1", intent, ORDER_ROLE_ENTRY), "entry")

        payload = metrics.to_dict()

        self.assertEqual(payload["entry_intent_count"], 1)
        self.assertEqual(payload["maker_entry_intent_count"], 1)
        self.assertEqual(payload["maker_fill_count"], 1)
        self.assertEqual(payload["entry_setup_maker_passive_fair_count"], 1)
        self.assertEqual(payload["fill_setup_maker_passive_fair_count"], 1)
        self.assertEqual(payload["markout_setup_maker_passive_fair_count"], 1)
        self.assertAlmostEqual(payload["average_markout_setup_maker_passive_fair_ticks"], 1.0)


if __name__ == "__main__":
    unittest.main()
