from __future__ import annotations

import unittest

from kabu_maker_taker.models import BoardSnapshot


class BoardSnapshotKabuBidAskTests(unittest.TestCase):
    def test_kabu_stock_bid_ask_fields_are_reversed(self) -> None:
        snapshot = BoardSnapshot.from_dict(
            {
                "Symbol": "9984",
                "Exchange": 1,
                "BidQty": 100,
                "BidPrice": 2408.5,
                "Sell1": {"Price": 2408.5, "Qty": 100},
                "AskQty": 200,
                "AskPrice": 2407.5,
                "Buy1": {"Price": 2407.5, "Qty": 200},
            },
            kabu_bidask_reversed=True,
            auto_fix_negative_spread=False,
        )

        self.assertEqual(snapshot.bid, 2407.5)
        self.assertEqual(snapshot.ask, 2408.5)
        self.assertEqual(snapshot.bid_size, 200)
        self.assertEqual(snapshot.ask_size, 100)
        self.assertTrue(snapshot.valid)

    def test_null_kabu_quote_fields_become_invalid_snapshot_without_exception(self) -> None:
        snapshot = BoardSnapshot.from_dict(
            {
                "Symbol": "9984",
                "Exchange": 1,
                "BidQty": None,
                "BidPrice": None,
                "AskQty": None,
                "AskPrice": None,
                "CurrentPrice": None,
                "Sell1": {"Price": None, "Qty": None},
                "Buy1": {"Price": None, "Qty": None},
            },
            kabu_bidask_reversed=True,
        )

        self.assertEqual(snapshot.bid, 0.0)
        self.assertEqual(snapshot.ask, 0.0)
        self.assertEqual(snapshot.bid_size, 0)
        self.assertEqual(snapshot.ask_size, 0)
        self.assertFalse(snapshot.valid)


if __name__ == "__main__":
    unittest.main()
