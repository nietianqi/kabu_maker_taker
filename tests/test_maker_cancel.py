"""Tests for the three maker-cancel improvements ported from kabu_hft_new:
  - Quote-drift cancel (max_quote_drift_ticks)
  - Queue-depth retreat (queue_min_top_qty / queue_retreat_ticks)
  - Stale-board guard (stale_board_ms)
"""
from __future__ import annotations

import unittest

from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, EntryDecision, Level, PositionState, SignalPacket
from kabu_maker_taker.risk import RiskManager
from kabu_maker_taker.strategy import MakerStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    bid: float = 100.0,
    ask: float = 101.0,
    bid_size: int = 500,
    ask_size: int = 300,
    ts_ns: int = 0,
) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=ts_ns,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        bids=(Level(bid, bid_size),),
        asks=(Level(ask, ask_size),),
    )


def _signal(
    composite: float = 0.5,
    obi_raw: float = 0.3,
    tape_ofi_raw: float = 0.2,
    lob_ofi_raw: float = 0.2,
    micro_momentum_raw: float = 0.1,
    microprice_tilt_raw: float = 0.1,
    microprice: float = 100.4,
    mid: float = 100.5,
    mid_std_ticks: float = 0.5,
) -> SignalPacket:
    return SignalPacket(
        ts_ns=0,
        composite=composite,
        obi_raw=obi_raw,
        obi_z=obi_raw * 2,
        tape_ofi_raw=tape_ofi_raw,
        tape_ofi_z=tape_ofi_raw * 2,
        lob_ofi_raw=lob_ofi_raw,
        lob_ofi_z=lob_ofi_raw * 2,
        micro_momentum_raw=micro_momentum_raw,
        micro_momentum_z=micro_momentum_raw * 2,
        microprice_tilt_raw=microprice_tilt_raw,
        microprice_tilt_z=microprice_tilt_raw * 2,
        microprice=microprice,
        mid=mid,
        mid_std_ticks=mid_std_ticks,
    )


def _maker(
    max_quote_drift_ticks: float = 1.0,
    queue_min_top_qty: int = 0,
    queue_retreat_ticks: float = 1.0,
    min_order_age_ms: int = 100,
    max_fair_drift_ticks: float = 1.5,
    tick_size: float = 1.0,
) -> MakerStrategy:
    cfg = StrategyConfig(
        max_quote_drift_ticks=max_quote_drift_ticks,
        queue_min_top_qty=queue_min_top_qty,
        queue_retreat_ticks=queue_retreat_ticks,
        min_order_age_ms=min_order_age_ms,
        max_fair_drift_ticks=max_fair_drift_ticks,
    )
    return MakerStrategy(cfg, tick_size=tick_size)


def _risk(stale_board_ms: int = 0, max_cancel_requests_per_minute: int = 0) -> RiskManager:
    return RiskManager(
        config=RiskConfig(
            stale_board_ms=stale_board_ms,
            max_cancel_requests_per_minute=max_cancel_requests_per_minute,
        ),
        tick_size=1.0,
        lot_size=100,
    )


# ---------------------------------------------------------------------------
# Fix 1: Quote-drift cancel
# ---------------------------------------------------------------------------

class QuoteDriftCancelTests(unittest.TestCase):

    def test_quote_drift_fires_when_bid_moves_1_tick(self) -> None:
        """Bid moves up 1 tick; working price stays at old bid → quote_drift cancel.

        fair_drift is disabled (large threshold) so quote_drift is the first trigger.
        """
        maker = _maker(max_quote_drift_ticks=1.0, min_order_age_ms=0,
                       max_fair_drift_ticks=10.0)  # disable fair_drift
        snap = _snap(bid=101.0, ask=102.0)     # bid moved up by 1 tick from 100
        # microprice > mid so microprice_flip doesn't fire for a long
        sig = _signal(mid=101.5, microprice=101.6)
        # working price is still 100 (old bid) — 1 tick behind ideal
        reason = maker.calc_cancel_reason(
            sig, 1, working_price=100.0,
            current_spread=1.0,
            order_age_ns=200_000_000,     # 200 ms — past min_order_age
            desired_price=maker.compute_quote_price(snap, sig, 1),
        )
        self.assertEqual(reason, "quote_drift")

    def test_quote_drift_suppressed_before_min_order_age(self) -> None:
        """Same bid move but order is too young → min-order-age guard suppresses cancel."""
        maker = _maker(max_quote_drift_ticks=1.0, min_order_age_ms=100)
        snap = _snap(bid=101.0, ask=102.0)
        sig = _signal(mid=101.5, microprice=101.4)
        reason = maker.calc_cancel_reason(
            sig, 1, working_price=100.0,
            current_spread=1.0,
            order_age_ns=50_000_000,      # 50 ms — inside guard window
            desired_price=maker.compute_quote_price(snap, sig, 1),
        )
        self.assertEqual(reason, "")

    def test_quote_drift_disabled_when_zero(self) -> None:
        """max_quote_drift_ticks=0 → quote_drift never fires."""
        maker = _maker(max_quote_drift_ticks=0.0, min_order_age_ms=0)
        snap = _snap(bid=101.0, ask=102.0)
        sig = _signal(mid=101.5, microprice=101.4)
        reason = maker.calc_cancel_reason(
            sig, 1, working_price=100.0,
            current_spread=1.0,
            order_age_ns=200_000_000,
            desired_price=maker.compute_quote_price(snap, sig, 1),
        )
        self.assertNotEqual(reason, "quote_drift")

    def test_no_drift_when_price_within_threshold(self) -> None:
        """Desired price == working price → no quote_drift cancel."""
        # composite=0.5 avoids alpha_decay; microprice > mid avoids microprice_flip
        maker = _maker(max_quote_drift_ticks=1.0, min_order_age_ms=0,
                       max_fair_drift_ticks=10.0)  # disable fair_drift
        snap = _snap(bid=100.0, ask=101.0)
        sig = _signal(mid=100.5, microprice=100.6, composite=0.5)
        desired = maker.compute_quote_price(snap, sig, 1)
        # Pass working_price = desired so drift = 0
        reason = maker.calc_cancel_reason(
            sig, 1, working_price=desired,
            current_spread=1.0,
            order_age_ns=200_000_000,
            desired_price=desired,
        )
        self.assertEqual(reason, "")


# ---------------------------------------------------------------------------
# Fix 2: Queue-depth retreat
# ---------------------------------------------------------------------------

class QueueRetreatTests(unittest.TestCase):

    def test_quote_retreats_when_bid_size_thin(self) -> None:
        """bid_size < queue_min_top_qty → build_intent returns price 1 tick behind best bid."""
        cfg = StrategyConfig(
            queue_min_top_qty=300,
            queue_retreat_ticks=1.0,
            maker_join_best=True,
            min_half_spread_ticks=1.0,
            mid_half_spread_ticks=1.0,
        )
        maker = MakerStrategy(cfg, tick_size=1.0)
        snap = _snap(bid=100.0, ask=101.0, bid_size=100)  # thin: 100 < 300
        sig = _signal(mid=100.5)
        pos = PositionState()  # flat position
        decision = EntryDecision(allow=True, reason="", entry_mode="maker", side=1, entry_score=6)
        intent = maker.build_intent(
            symbol="9984", exchange=27, tick_size=1.0, lot_size=100, qty=100,
            snapshot=snap, decision=decision,
            signal=sig, position=pos, max_inventory_qty=300,
        )
        # With maker_join_best=True + thin queue, extra retreat → price < bid (100.0).
        self.assertLess(intent.price, 100.0)

    def test_quote_does_not_retreat_when_thick(self) -> None:
        """bid_size >= queue_min_top_qty → price unchanged (no queue retreat)."""
        cfg = StrategyConfig(
            queue_min_top_qty=300,
            queue_retreat_ticks=1.0,
            maker_join_best=True,
            min_half_spread_ticks=1.0,
            mid_half_spread_ticks=1.0,
        )
        maker = MakerStrategy(cfg, tick_size=1.0)
        snap = _snap(bid=100.0, ask=101.0, bid_size=500)  # thick: 500 >= 300
        sig = _signal(mid=100.5)
        pos = PositionState()
        decision = EntryDecision(allow=True, reason="", entry_mode="maker", side=1, entry_score=6)
        intent = maker.build_intent(
            symbol="9984", exchange=27, tick_size=1.0, lot_size=100, qty=100,
            snapshot=snap, decision=decision,
            signal=sig, position=pos, max_inventory_qty=300,
        )
        # With thick queue no extra retreat → price at bid (100.0).
        self.assertEqual(intent.price, 100.0)

    def test_queue_retreat_disabled_when_zero(self) -> None:
        """queue_min_top_qty=0 → even with 1-share bid, no retreat."""
        cfg = StrategyConfig(
            queue_min_top_qty=0,
            queue_retreat_ticks=1.0,
            maker_join_best=True,
            min_half_spread_ticks=1.0,
            mid_half_spread_ticks=1.0,
        )
        maker = MakerStrategy(cfg, tick_size=1.0)
        snap = _snap(bid=100.0, ask=101.0, bid_size=1)  # very thin but feature disabled
        sig = _signal(mid=100.5)
        pos = PositionState()
        decision = EntryDecision(allow=True, reason="", entry_mode="maker", side=1, entry_score=6)
        intent = maker.build_intent(
            symbol="9984", exchange=27, tick_size=1.0, lot_size=100, qty=100,
            snapshot=snap, decision=decision,
            signal=sig, position=pos, max_inventory_qty=300,
        )
        self.assertEqual(intent.price, 100.0)


# ---------------------------------------------------------------------------
# Fix 3: Stale-board guard
# ---------------------------------------------------------------------------

class StaleBoardTests(unittest.TestCase):

    def test_stale_board_blocks_entry(self) -> None:
        """After a 31-second inter-board gap, can_enter() returns 'stale_board'."""
        risk = _risk(stale_board_ms=30_000)
        t0_ns = 1_000_000_000_000_000_000
        risk.update_board_ts(t0_ns)
        snap = _snap(ts_ns=t0_ns + 31_000_000_000)  # 31 s later

        decision = EntryDecision(allow=True, reason="", entry_mode="maker", side=1)
        pos = PositionState()
        allowed, reason = risk.can_enter(
            snapshot=snap, decision=decision, position=pos,
            now_ns=snap.ts_ns, expected_price=100.0,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "stale_board")

    def test_stale_board_triggers_urgent_cancel(self) -> None:
        """board_stale=True → calc_cancel_reason returns 'stale_board' even within min_order_age."""
        maker = _maker(min_order_age_ms=5000)  # very long guard — would suppress normal cancels
        sig = _signal()
        # board_stale bypasses the min-order-age guard (it is urgent)
        reason = maker.calc_cancel_reason(
            sig, 1, working_price=100.0,
            order_age_ns=10_000_000,      # 10 ms — well inside the 5s guard
            board_stale=True,
        )
        self.assertEqual(reason, "stale_board")

    def test_stale_board_disabled_when_zero(self) -> None:
        """stale_board_ms=0 → is_stale_board always returns False regardless of gap."""
        risk = _risk(stale_board_ms=0)
        t0_ns = 1_000_000_000_000_000_000
        risk.update_board_ts(t0_ns)
        # 2-hour gap
        self.assertFalse(risk.is_stale_board(t0_ns + 7_200_000_000_000))

    def test_first_board_never_stale(self) -> None:
        """No previous board timestamp → is_stale_board returns False."""
        risk = _risk(stale_board_ms=1000)
        # Never called update_board_ts
        self.assertFalse(risk.is_stale_board(1_000_000_000_000_000_000))

    def test_stale_board_clears_after_update(self) -> None:
        """After receiving a fresh board, subsequent boards within threshold are not stale."""
        risk = _risk(stale_board_ms=30_000)
        t0_ns = 1_000_000_000_000_000_000
        # First board (gap > threshold)
        risk.update_board_ts(t0_ns)
        t1_ns = t0_ns + 31_000_000_000
        self.assertTrue(risk.is_stale_board(t1_ns))
        # After processing the stale board, update timestamp
        risk.update_board_ts(t1_ns)
        # Next board arrives 1 second later — not stale
        t2_ns = t1_ns + 1_000_000_000
        self.assertFalse(risk.is_stale_board(t2_ns))


if __name__ == "__main__":
    unittest.main()
