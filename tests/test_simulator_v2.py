from __future__ import annotations

import unittest

from kabu_maker_taker.models import BoardSnapshot, BrokerFillEvent, Level, OrderIntent, OrderStatus, TradePrint
from kabu_maker_taker.simulator import DryRunSimulator


def _intent(
    qty: int = 100,
    price: float = 100.0,
    side: int = 1,
    is_market: bool = False,
    cid: str = "ORD-1",
    max_slip_ticks: float = 1.0,
) -> OrderIntent:
    return OrderIntent(
        symbol="9984",
        exchange=27,
        side=side,
        qty=qty,
        price=price,
        is_market=is_market,
        strategy="taker" if is_market else "maker",
        reason="test",
        score=5,
        reference_price=price,
        max_slip_ticks=max_slip_ticks,
        client_order_id=cid,
    )


def _snap(
    bid: float = 100.0,
    ask: float = 101.0,
    bid_size: int = 500,
    ask_size: int = 200,
    ts_ns: int = 1_000_000_000,
) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=ts_ns,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        bids=(Level(bid, bid_size), Level(bid - 1.0, 300)),
        asks=(Level(ask, ask_size), Level(ask + 1.0, 250)),
    )


def _trade(price: float, side: int, size: int, ts_ns: int = 1_000_000_000) -> TradePrint:
    return TradePrint(symbol="9984", ts_ns=ts_ns, price=price, size=size, side=side)


class TakerDepthLimitedFillTests(unittest.TestCase):
    """Market orders use IOC depth; unfilled remainder is canceled."""

    def test_full_fill_when_qty_within_depth(self) -> None:
        sim = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snap(ask_size=200)
        events = sim.submit(_intent(qty=50, is_market=True, side=1), snap, snap.ts_ns)
        fills = [e for e in events if isinstance(e, BrokerFillEvent)]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 50)
        self.assertEqual(fills[0].price, 101.0)
        self.assertEqual(len(sim._orders), 0)

    def test_partial_fill_and_cancel_remainder_when_qty_exceeds_allowed_depth(self) -> None:
        sim = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snap(ask_size=100)
        events = sim.submit(_intent(qty=200, is_market=True, side=1), snap, snap.ts_ns)
        fills = [e for e in events if isinstance(e, BrokerFillEvent)]
        cancels = [e for e in events if not isinstance(e, BrokerFillEvent) and e.status == OrderStatus.CANCELED]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 100)
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0].cum_qty, 100)
        self.assertEqual(len(sim._orders), 0)

    def test_zero_depth_market_order_does_not_fill_or_queue(self) -> None:
        sim = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snap(ask_size=0)
        events = sim.submit(_intent(qty=200, is_market=True, side=1), snap, snap.ts_ns)
        fills = [e for e in events if isinstance(e, BrokerFillEvent)]
        cancels = [e for e in events if not isinstance(e, BrokerFillEvent) and e.status == OrderStatus.CANCELED]
        self.assertEqual(fills, [])
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0].cum_qty, 0)
        self.assertEqual(len(sim._orders), 0)

    def test_sell_market_depth_limited(self) -> None:
        sim = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snap(bid_size=80)
        events = sim.submit(_intent(qty=150, is_market=True, side=-1, max_slip_ticks=0.0), snap, snap.ts_ns)
        fills = [e for e in events if isinstance(e, BrokerFillEvent)]
        cancels = [e for e in events if not isinstance(e, BrokerFillEvent) and e.status == OrderStatus.CANCELED]
        self.assertEqual(fills[0].qty, 80)
        self.assertEqual(fills[0].price, 100.0)
        self.assertEqual(len(cancels), 1)
        self.assertEqual(len(sim._orders), 0)

    def test_multi_level_depth_fills_with_vwap_inside_slip_limit(self) -> None:
        sim = DryRunSimulator(tick_size=1.0, slippage_ticks=0.0)
        snap = _snap(ask_size=100)
        events = sim.submit(_intent(qty=200, is_market=True, side=1, max_slip_ticks=2.0), snap, snap.ts_ns)
        fills = [e for e in events if isinstance(e, BrokerFillEvent)]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 200)
        self.assertAlmostEqual(fills[0].price, 101.5)


class MakerTradePrintQueueTests(unittest.TestCase):
    """Maker queue consumption is trade-print driven, not cancel-size driven."""

    def test_cancel_only_does_not_fill_maker_order(self) -> None:
        sim = DryRunSimulator(tick_size=1.0)
        snap1 = _snap(bid=100.0, bid_size=500)
        sim.submit(_intent(qty=100, price=100.0, side=1, cid="ORD-1"), snap1, snap1.ts_ns)
        self.assertEqual(sim.queue_ahead("ORD-1"), 500)

        snap2 = _snap(bid=100.0, bid_size=200, ts_ns=snap1.ts_ns + 100)
        fills = sim.on_board(snap2, snap2.ts_ns)
        self.assertEqual(len(fills), 0)

    def test_trade_print_consumes_queue_and_fills(self) -> None:
        sim = DryRunSimulator(tick_size=1.0)
        snap1 = _snap(bid=100.0, bid_size=100)
        sim.submit(_intent(qty=50, price=100.0, side=1, cid="ORD-1"), snap1, snap1.ts_ns)

        sim.on_trade(_trade(price=100.0, side=-1, size=150, ts_ns=snap1.ts_ns + 50), snap1.ts_ns + 50)
        snap2 = _snap(bid=100.0, bid_size=50, ts_ns=snap1.ts_ns + 100)
        fills = sim.on_board(snap2, snap2.ts_ns)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 50)

    def test_partial_trade_consumption_does_not_fill_yet(self) -> None:
        sim = DryRunSimulator(tick_size=1.0)
        snap1 = _snap(bid=100.0, bid_size=600)
        sim.submit(_intent(qty=50, price=100.0, side=1, cid="ORD-1"), snap1, snap1.ts_ns)

        sim.on_trade(_trade(price=100.0, side=-1, size=50, ts_ns=snap1.ts_ns + 50))
        snap2 = _snap(bid=100.0, bid_size=550, ts_ns=snap1.ts_ns + 100)
        fills = sim.on_board(snap2, snap2.ts_ns)
        self.assertEqual(len(fills), 0)

    def test_aggressive_cross_still_fills_immediately(self) -> None:
        sim = DryRunSimulator(tick_size=1.0)
        snap1 = _snap(bid=100.0, ask=101.0, bid_size=500)
        sim.submit(_intent(qty=100, price=100.0, side=1, cid="ORD-1"), snap1, snap1.ts_ns)

        snap2 = _snap(bid=100.0, ask=100.0, bid_size=500, ts_ns=snap1.ts_ns + 100)
        fills = sim.on_board(snap2, snap2.ts_ns)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 100)

    def test_acc_fills_reset_each_board(self) -> None:
        sim = DryRunSimulator(tick_size=1.0)
        snap1 = _snap(bid=100.0, bid_size=50)
        sim.submit(_intent(qty=20, price=100.0, side=1, cid="ORD-1"), snap1, snap1.ts_ns)

        sim.on_trade(_trade(price=100.0, side=-1, size=100))
        snap2 = _snap(bid=100.0, bid_size=50, ts_ns=snap1.ts_ns + 100)
        sim.on_board(snap2, snap2.ts_ns)

        self.assertEqual(sim._acc_fills, {})


if __name__ == "__main__":
    unittest.main()
