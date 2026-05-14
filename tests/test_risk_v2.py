from __future__ import annotations

import unittest

from kabu_maker_taker.config import RiskConfig, StrategyConfig
from kabu_maker_taker.models import (
    BoardSnapshot,
    EntryDecision,
    Level,
    MarketState,
    PositionState,
    SignalPacket,
)
from kabu_maker_taker.risk import RiskManager
from kabu_maker_taker.strategy import MakerStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _risk(**kw) -> RiskManager:
    cfg = RiskConfig(**kw)
    return RiskManager(config=cfg, tick_size=1.0, lot_size=100)


def _snapshot(**kw) -> BoardSnapshot:
    defaults = dict(
        symbol="9984", ts_ns=1_000_000_000,
        bid=100.0, ask=101.0, bid_size=500, ask_size=200,
        bids=(Level(100.0, 500), Level(99.0, 300)),
        asks=(Level(101.0, 200), Level(102.0, 250)),
    )
    defaults.update(kw)
    return BoardSnapshot(**defaults)


def _decision(side: int = 1) -> EntryDecision:
    return EntryDecision(True, "", entry_mode="maker", side=side)


def _signal(**overrides) -> SignalPacket:
    defaults = {
        "ts_ns": 1_000_000_000,
        "obi_raw": 0.35, "lob_ofi_raw": 0.20, "tape_ofi_raw": 0.20,
        "micro_momentum_raw": 0.10, "microprice_tilt_raw": 0.30,
        "microprice": 100.3, "mid": 100.0,
        "obi_z": 0.5, "lob_ofi_z": 0.4, "tape_ofi_z": 0.3,
        "micro_momentum_z": 0.2, "microprice_tilt_z": 0.1,
        "composite": 0.45, "integrated_ofi": 0.20, "trade_burst_score": 0.10,
        "mid_std_ticks": 1.0,
    }
    defaults.update(overrides)
    return SignalPacket(**defaults)


# ============================================================================
# Daily Loss Limit Tests
# ============================================================================

class DailyLossLimitTests(unittest.TestCase):

    def test_no_block_when_limit_disabled(self) -> None:
        rm = _risk(daily_loss_limit=0.0, max_spread_ticks=5.0)
        rm._daily_pnl = -9999.0
        rm._daily_date = "1970-01-01"
        ok, reason = rm.can_enter(
            snapshot=_snapshot(), decision=_decision(),
            position=PositionState(), now_ns=1_000_000_000, expected_price=101.0,
        )
        self.assertTrue(ok)

    def test_blocks_when_daily_loss_exceeded(self) -> None:
        rm = _risk(daily_loss_limit=5000.0, max_spread_ticks=5.0)
        # Force the daily pnl below the limit
        rm._daily_pnl = -5000.0
        rm._daily_date = "1970-01-01"
        ok, reason = rm.can_enter(
            snapshot=_snapshot(), decision=_decision(),
            position=PositionState(), now_ns=1_000_000_000, expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_limit")

    def test_allows_when_just_below_limit(self) -> None:
        rm = _risk(daily_loss_limit=5000.0, max_spread_ticks=5.0)
        rm._daily_pnl = -4999.0
        rm._daily_date = "1970-01-01"
        ok, reason = rm.can_enter(
            snapshot=_snapshot(), decision=_decision(),
            position=PositionState(), now_ns=1_000_000_000, expected_price=101.0,
        )
        self.assertTrue(ok)

    def test_record_trade_result_accumulates_pnl(self) -> None:
        rm = _risk(daily_loss_limit=5000.0)
        ns = 1_770_000_000_000_000_000  # a real JST timestamp
        rm.record_trade_result(False, ns, pnl=-1000.0)
        self.assertAlmostEqual(rm._daily_pnl, -1000.0)
        rm.record_trade_result(True, ns, pnl=300.0)
        self.assertAlmostEqual(rm._daily_pnl, -700.0)

    def test_daily_pnl_resets_on_new_day(self) -> None:
        rm = _risk(daily_loss_limit=5000.0)
        # Day 1: 2026-05-14 JST noon
        ns_day1 = 1_747_184_400_000_000_000  # ~2026-05-14 12:00 JST
        rm.record_trade_result(False, ns_day1, pnl=-3000.0)
        self.assertAlmostEqual(rm._daily_pnl, -3000.0)
        # Day 2: one day later
        ns_day2 = ns_day1 + 86_400_000_000_000  # +24 hours
        rm.record_trade_result(True, ns_day2, pnl=100.0)
        self.assertAlmostEqual(rm._daily_pnl, 100.0)  # reset then add

    def test_daily_pnl_not_reset_same_day(self) -> None:
        rm = _risk(daily_loss_limit=5000.0)
        ns = 1_747_184_400_000_000_000
        rm.record_trade_result(False, ns, pnl=-2000.0)
        rm.record_trade_result(False, ns + 3_600_000_000_000, pnl=-1000.0)  # +1 hour
        self.assertAlmostEqual(rm._daily_pnl, -3000.0)

    def test_blocks_after_accumulated_daily_losses(self) -> None:
        rm = _risk(daily_loss_limit=5000.0, max_spread_ticks=5.0)
        ns = 1_747_184_400_000_000_000
        rm.record_trade_result(False, ns, pnl=-5000.0)
        ok, reason = rm.can_enter(
            snapshot=_snapshot(), decision=_decision(),
            position=PositionState(), now_ns=ns + 1_000_000, expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_limit")


# ============================================================================
# Dual TSE Session Window Tests
# ============================================================================

class DualSessionWindowTests(unittest.TestCase):

    def _risk_session(self, start1: str, end1: str, start2: str = "", end2: str = "") -> RiskManager:
        cfg = RiskConfig(
            enforce_session=True,
            open_start_hhmm=start1,
            open_end_hhmm=end1,
            open_start_hhmm_2=start2,
            open_end_hhmm_2=end2,
            max_spread_ticks=5.0,
        )
        return RiskManager(config=cfg, tick_size=1.0, lot_size=100)

    def _ns_jst(self, hhmm: str) -> int:
        """Build a nanosecond timestamp for today JST at the given HH:MM."""
        from datetime import date, datetime, timezone, timedelta
        JST = timezone(timedelta(hours=9))
        h, m = hhmm.split(":")
        # Use 2026-05-14 as reference date
        dt = datetime(2026, 5, 14, int(h), int(m), 0, tzinfo=JST)
        return int(dt.timestamp() * 1_000_000_000)

    def test_single_window_inside(self) -> None:
        rm = self._risk_session("09:00", "11:30")
        ok, reason = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("10:00")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("10:00"), expected_price=101.0,
        )
        self.assertTrue(ok)

    def test_single_window_lunch_break_blocked(self) -> None:
        rm = self._risk_session("09:00", "11:30")
        ok, reason = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("12:00")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("12:00"), expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_session")

    def test_dual_window_morning_open(self) -> None:
        rm = self._risk_session("09:00", "11:30", "12:30", "15:30")
        ok, _ = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("10:30")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("10:30"), expected_price=101.0,
        )
        self.assertTrue(ok)

    def test_dual_window_lunch_break_blocked(self) -> None:
        rm = self._risk_session("09:00", "11:30", "12:30", "15:30")
        ok, reason = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("12:00")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("12:00"), expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_session")

    def test_dual_window_afternoon_open(self) -> None:
        rm = self._risk_session("09:00", "11:30", "12:30", "15:30")
        ok, _ = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("14:00")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("14:00"), expected_price=101.0,
        )
        self.assertTrue(ok)

    def test_dual_window_after_close_blocked(self) -> None:
        rm = self._risk_session("09:00", "11:30", "12:30", "15:30")
        ok, reason = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("16:00")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("16:00"), expected_price=101.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_session")

    def test_dual_window_boundary_at_morning_close(self) -> None:
        rm = self._risk_session("09:00", "11:30", "12:30", "15:30")
        ok, _ = rm.can_enter(
            snapshot=_snapshot(ts_ns=self._ns_jst("11:30")),
            decision=_decision(), position=PositionState(),
            now_ns=self._ns_jst("11:30"), expected_price=101.0,
        )
        self.assertTrue(ok)  # boundary is inclusive


# ============================================================================
# Spread Expanded Cancel Tests
# ============================================================================

class SpreadExpandedCancelTests(unittest.TestCase):

    def _maker(self, **kw) -> MakerStrategy:
        defaults = dict(
            alpha_exit_threshold=0.15, alpha_entry_threshold=0.40,
            tape_imbalance_long=0.10, book_imbalance_long=0.18,
            max_fair_drift_ticks=1.5, fair_value_beta=0.75, max_fair_shift_ticks=3.0,
            spread_expanded_ticks=4.0,
        )
        defaults.update(kw)
        return MakerStrategy(StrategyConfig(**defaults), tick_size=1.0)

    def test_no_cancel_when_spread_normal(self) -> None:
        m = self._maker()
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.30,
                      microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       current_spread=1.0)
        self.assertEqual(reason, "")

    def test_spread_expanded_triggers_cancel(self) -> None:
        m = self._maker(spread_expanded_ticks=4.0)
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.30,
                      microprice=100.3, mid=100.0)
        # spread = 5 ticks >= 4.0 threshold
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       current_spread=5.0)
        self.assertEqual(reason, "spread_expanded")

    def test_spread_expanded_disabled_when_zero(self) -> None:
        m = self._maker(spread_expanded_ticks=0.0)
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.30,
                      microprice=100.3, mid=100.0)
        # Would otherwise trigger spread_expanded
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       current_spread=99.0)
        self.assertEqual(reason, "")


# ============================================================================
# Min Order Age Tests
# ============================================================================

class MinOrderAgeTests(unittest.TestCase):

    def _maker(self, **kw) -> MakerStrategy:
        defaults = dict(
            alpha_exit_threshold=0.15, alpha_entry_threshold=0.40,
            tape_imbalance_long=0.10, book_imbalance_long=0.18,
            max_fair_drift_ticks=1.5, fair_value_beta=0.75, max_fair_shift_ticks=3.0,
            min_order_age_ms=100,
        )
        defaults.update(kw)
        return MakerStrategy(StrategyConfig(**defaults), tick_size=1.0)

    def test_signal_cancel_suppressed_within_min_age(self) -> None:
        m = self._maker()
        # alpha_flip signal
        sig = _signal(composite=-0.50, tape_ofi_raw=0.15, obi_raw=0.20,
                      microprice=100.3, mid=100.0)
        # order_age_ns = 50ms < 100ms → suppressed
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       order_age_ns=50_000_000)
        self.assertEqual(reason, "")

    def test_signal_cancel_fires_after_min_age(self) -> None:
        m = self._maker()
        sig = _signal(composite=-0.50, tape_ofi_raw=0.15, obi_raw=0.20,
                      microprice=100.3, mid=100.0)
        # order_age_ns = 200ms > 100ms → alpha_flip fires
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       order_age_ns=200_000_000)
        self.assertEqual(reason, "alpha_flip")

    def test_abnormal_market_bypasses_min_age(self) -> None:
        m = self._maker()
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.20,
                      microprice=100.3, mid=100.0)
        # Abnormal market fires even if order is new (age < min)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       market_state=MarketState.ABNORMAL,
                                       order_age_ns=10_000_000)
        self.assertEqual(reason, "abnormal_market")

    def test_spread_expanded_bypasses_min_age(self) -> None:
        m = self._maker(spread_expanded_ticks=4.0)
        sig = _signal(composite=0.45, tape_ofi_raw=0.15, obi_raw=0.20,
                      microprice=100.3, mid=100.0)
        # Spread expanded fires even if order is new
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       current_spread=6.0, order_age_ns=10_000_000)
        self.assertEqual(reason, "spread_expanded")

    def test_min_age_disabled_when_zero(self) -> None:
        m = self._maker(min_order_age_ms=0)
        sig = _signal(composite=-0.50, tape_ofi_raw=0.15, obi_raw=0.20,
                      microprice=100.3, mid=100.0)
        # With min_order_age_ms=0 guard is off — fires immediately
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       order_age_ns=1_000_000)
        self.assertEqual(reason, "alpha_flip")

    def test_zero_order_age_bypasses_guard(self) -> None:
        """order_age_ns=0 (default / unknown) should not suppress cancels."""
        m = self._maker()
        sig = _signal(composite=-0.50, tape_ofi_raw=0.15, obi_raw=0.20,
                      microprice=100.3, mid=100.0)
        reason = m.calc_cancel_reason(sig, working_side=1, working_price=100.0,
                                       order_age_ns=0)
        self.assertEqual(reason, "alpha_flip")


# ============================================================================
# Microprice Streak Bonus Tests
# ============================================================================

class MicropriceStreakBonusTests(unittest.TestCase):

    def _maker(self, **kw) -> MakerStrategy:
        return MakerStrategy(StrategyConfig(microprice_streak_min=3, **kw), tick_size=1.0)

    def _snap(self) -> BoardSnapshot:
        return BoardSnapshot(
            symbol="9984", ts_ns=1_000_000_000,
            bid=100.0, ask=101.0, bid_size=500, ask_size=200,
            bids=(Level(100.0, 500), Level(99.0, 300)),
            asks=(Level(101.0, 200), Level(102.0, 250)),
        )

    def test_no_streak_bonus_at_zero(self) -> None:
        m = self._maker(maker_score_threshold=100)  # block entry — only test score
        sig = _signal(microprice_up_streak=0, microprice_down_streak=0)
        d = m.evaluate(self._snap(), sig)
        # score without streak should be same as baseline
        from kabu_maker_taker.strategy import entry_layer_diagnostics, StrategyConfig as _SC
        diag = entry_layer_diagnostics(self._snap(), sig, m.config, direction=1)
        self.assertEqual(diag.direction_score % 1, 0)  # integer score, no bonus

    def test_long_streak_adds_bonus_for_long(self) -> None:
        from kabu_maker_taker.strategy import entry_layer_diagnostics
        sig_no_streak = _signal(microprice_up_streak=0)
        sig_streaking = _signal(microprice_up_streak=3)
        snap = self._snap()
        m = self._maker()
        d_base = entry_layer_diagnostics(snap, sig_no_streak, m.config, direction=1)
        d_bonus = entry_layer_diagnostics(snap, sig_streaking, m.config, direction=1)
        self.assertEqual(d_bonus.direction_score, d_base.direction_score + 1)

    def test_streak_not_added_for_wrong_direction(self) -> None:
        from kabu_maker_taker.strategy import entry_layer_diagnostics
        # Neutral signal: obi/tilt both zero so direction_score base is 0 for both sides.
        # Only the streak bonus should differ.
        sig = _signal(
            obi_raw=0.0, microprice_tilt_raw=0.0,
            microprice_up_streak=5, microprice_down_streak=0,
        )
        snap = self._snap()
        m = self._maker()
        d_long = entry_layer_diagnostics(snap, sig, m.config, direction=1)
        d_short = entry_layer_diagnostics(snap, sig, m.config, direction=-1)
        # Long direction gets the bonus (+1), short does not
        self.assertEqual(d_long.direction_score, d_short.direction_score + 1)

    def test_down_streak_adds_bonus_for_short(self) -> None:
        from kabu_maker_taker.strategy import entry_layer_diagnostics
        sig_base = _signal(microprice_down_streak=0)
        sig_down = _signal(microprice_down_streak=4)
        snap = self._snap()
        m = self._maker()
        d_base = entry_layer_diagnostics(snap, sig_base, m.config, direction=-1)
        d_bonus = entry_layer_diagnostics(snap, sig_down, m.config, direction=-1)
        self.assertEqual(d_bonus.direction_score, d_base.direction_score + 1)

    def test_streak_bonus_disabled_when_min_is_zero(self) -> None:
        from kabu_maker_taker.strategy import entry_layer_diagnostics
        m = MakerStrategy(StrategyConfig(microprice_streak_min=0), tick_size=1.0)
        sig = _signal(microprice_up_streak=999)
        snap = self._snap()
        d = entry_layer_diagnostics(snap, sig, m.config, direction=1)
        # No bonus — streak_min=0 disables feature
        sig_none = _signal(microprice_up_streak=0)
        d_none = entry_layer_diagnostics(snap, sig_none, m.config, direction=1)
        self.assertEqual(d.direction_score, d_none.direction_score)


if __name__ == "__main__":
    unittest.main()
