from __future__ import annotations

import unittest

from kabu_maker_taker.config import LollipopConfig
from kabu_maker_taker.lollipop import LollipopTPManager
from kabu_maker_taker.models import BoardSnapshot, Level, LollipopPhase, PositionState


def _snap(bid: float = 100.0, ask: float = 101.0, bid_size: int = 1000, ask_size: int = 500) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        ts_ns=0,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        bids=(Level(bid, bid_size),),
        asks=(Level(ask, ask_size),),
    )


def _pos(avg_price: float, qty: int = 100, side: int = 1, entry_mode: str = "maker") -> PositionState:
    p = PositionState()
    p.side = side
    p.qty = qty
    p.avg_price = avg_price
    p.entry_mode = entry_mode
    p.entry_ts_ns = 0
    return p


_CFG = LollipopConfig(
    maker_tp_ticks=2.0,
    taker_tp_ticks=3.0,
    maker_max_hold_seconds=10,
    taker_max_hold_seconds=5,
    tp_delay_ms=0,  # no delay for tests
    max_retries=3,
    stop_loss_ticks=0.0,
)

_TICK = 1.0
_LOT = 100
_KW = dict(symbol="9984", exchange=27)


class LollipopPhaseTests(unittest.TestCase):
    def test_idle_at_start(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        self.assertEqual(mgr.phase, LollipopPhase.IDLE)
        self.assertFalse(mgr.is_busy)

    def test_entry_fill_transitions_to_scheduled(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=1_000)
        self.assertEqual(mgr.phase, LollipopPhase.SCHEDULED)
        self.assertTrue(mgr.is_busy)

    def test_maker_tp_price(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        # tp_delay_ms=0, so tick at now_ns=0 should fire
        pos = _pos(100.0)
        action = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action.action, "submit_tp")
        assert action.intent is not None
        self.assertEqual(action.intent.price, 102.0)  # 100 + 2 ticks
        self.assertFalse(action.intent.is_market)
        self.assertEqual(action.intent.strategy, "lollipop_tp")
        self.assertEqual(mgr.phase, LollipopPhase.ACTIVE)

    def test_short_maker_tp_price(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0, entry_side=-1)
        pos = _pos(100.0, side=-1)
        action = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action.action, "submit_tp")
        assert action.intent is not None
        self.assertEqual(action.intent.price, 98.0)  # 100 - 2 ticks
        self.assertEqual(action.intent.side, 1)
        self.assertFalse(action.intent.is_market)

    def test_taker_tp_price(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "taker", now_ns=0)
        pos = _pos(100.0, entry_mode="taker")
        action = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action.action, "submit_tp")
        assert action.intent is not None
        self.assertEqual(action.intent.price, 103.0)  # 100 + 3 ticks

    def test_active_phase_returns_none_before_timeout(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=0, **_KW)  # SCHEDULED → ACTIVE
        action = mgr.tick(_snap(), pos, now_ns=1_000_000_000, **_KW)  # 1 second later
        self.assertEqual(action.action, "none")
        self.assertEqual(mgr.phase, LollipopPhase.ACTIVE)

    def test_timeout_triggers_force_exit(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        now = 0
        mgr.on_entry_fill(100.0, "maker", now_ns=now)
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=now, **_KW)  # → ACTIVE
        # Advance past maker_max_hold_seconds (10s)
        future = now + 11 * 1_000_000_000
        action = mgr.tick(_snap(), pos, now_ns=future, **_KW)
        self.assertEqual(action.action, "force_exit")
        assert action.intent is not None
        self.assertTrue(action.intent.is_market)
        self.assertEqual(action.intent.reason, "timeout_exit")
        self.assertEqual(action.intent.side, -1)  # closing long

    def test_taker_hold_timeout_shorter(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        now = 0
        mgr.on_entry_fill(100.0, "taker", now_ns=now)
        pos = _pos(100.0, entry_mode="taker")
        mgr.tick(_snap(), pos, now_ns=now, **_KW)  # → ACTIVE
        # 6 seconds > taker_max_hold_seconds (5s)
        action = mgr.tick(_snap(), pos, now_ns=6_000_000_000, **_KW)
        self.assertEqual(action.action, "force_exit")

    def test_exit_fill_resets_to_idle(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=0, **_KW)  # → ACTIVE
        mgr.on_exit_fill()
        self.assertEqual(mgr.phase, LollipopPhase.IDLE)
        self.assertFalse(mgr.is_busy)

    def test_retry_budget_exhausted_triggers_timeout(self) -> None:
        cfg = LollipopConfig(
            maker_tp_ticks=2.0,
            taker_tp_ticks=3.0,
            maker_max_hold_seconds=100,
            taker_max_hold_seconds=100,
            tp_delay_ms=0,
            max_retries=2,
            stop_loss_ticks=0.0,
        )
        mgr = LollipopTPManager(cfg, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        pos = _pos(100.0)
        # Each tick in SCHEDULED fires submit_tp and moves to ACTIVE
        mgr.tick(_snap(), pos, now_ns=0, **_KW)   # retry 1 → ACTIVE
        mgr.reschedule(now_ns=0)                   # back to SCHEDULED
        mgr.tick(_snap(), pos, now_ns=0, **_KW)   # retry 2 → ACTIVE
        mgr.reschedule(now_ns=0)                   # back to SCHEDULED
        action = mgr.tick(_snap(), pos, now_ns=0, **_KW)  # retries exhausted → TIMEOUT
        self.assertEqual(action.action, "force_exit")

    def test_stop_loss_triggers_force_exit(self) -> None:
        cfg = LollipopConfig(
            maker_tp_ticks=2.0,
            taker_tp_ticks=3.0,
            maker_max_hold_seconds=100,
            taker_max_hold_seconds=100,
            tp_delay_ms=0,
            max_retries=5,
            stop_loss_ticks=2.0,
        )
        mgr = LollipopTPManager(cfg, _TICK, _LOT)
        now = 0
        mgr.on_entry_fill(100.0, "maker", now_ns=now)
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=now, **_KW)  # → ACTIVE
        # bid drops to 97 → loss = (100 - 97) / 1 = 3 ticks ≥ stop_loss_ticks 2
        bad_snap = _snap(bid=97.0, ask=98.0)
        action = mgr.tick(bad_snap, pos, now_ns=1_000_000_000, **_KW)
        self.assertEqual(action.action, "force_exit")

    def test_short_stop_loss_uses_ask_and_triggers_force_exit(self) -> None:
        cfg = LollipopConfig(
            maker_tp_ticks=2.0,
            taker_tp_ticks=3.0,
            maker_max_hold_seconds=100,
            taker_max_hold_seconds=100,
            tp_delay_ms=0,
            max_retries=5,
            stop_loss_ticks=2.0,
        )
        mgr = LollipopTPManager(cfg, _TICK, _LOT)
        now = 0
        mgr.on_entry_fill(100.0, "maker", now_ns=now, entry_side=-1)
        pos = _pos(100.0, side=-1)
        mgr.tick(_snap(), pos, now_ns=now, **_KW)  # -> ACTIVE
        bad_snap = _snap(bid=102.0, ask=103.0)
        action = mgr.tick(bad_snap, pos, now_ns=1_000_000_000, **_KW)
        self.assertEqual(action.action, "force_exit")
        assert action.intent is not None
        self.assertEqual(action.intent.side, 1)
        self.assertEqual(action.intent.reference_price, 103.0)

    def test_idle_tick_returns_none(self) -> None:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        pos = _pos(100.0)
        action = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action.action, "none")
        self.assertIsNone(action.intent)


class LollipopIntegrationTests(unittest.TestCase):
    """Test lollipop through CombinedMakerTakerStrategy."""

    def _make_strategy(self, tp_delay_ms: int = 0):
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.config import AppConfig, RiskConfig, SignalConfig, StrategyConfig

        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            lot_size=100,
            strategy=StrategyConfig(taker_confirm_ticks=1, maker_confirm_ticks=1),
            risk=RiskConfig(max_spread_ticks=3.0),
            signals=SignalConfig(zscore_window=2),
            lollipop=LollipopConfig(
                maker_tp_ticks=2.0,
                taker_tp_ticks=3.0,
                maker_max_hold_seconds=60,
                taker_max_hold_seconds=30,
                tp_delay_ms=tp_delay_ms,
                max_retries=5,
                stop_loss_ticks=0.0,
            ),
        )
        return CombinedMakerTakerStrategy(config)

    def test_exit_intent_after_entry_fill(self) -> None:
        from kabu_maker_taker.models import BrokerFillEvent, TradePrint

        strategy = self._make_strategy(tp_delay_ms=0)
        base = 1_770_000_000_000_000_000

        strategy.on_trade(TradePrint("9984", base, 100.8, 500, 1))
        snap1 = BoardSnapshot(
            "9984", base + 100_000_000, 100.0, 101.0, 900, 200,
            bids=(Level(100.0, 900), Level(99.0, 500)),
            asks=(Level(101.0, 200), Level(102.0, 250)),
        )
        strategy.on_board(snap1, now_ns=snap1.ts_ns)

        strategy.on_trade(TradePrint("9984", base + 150_000_000, 101.0, 800, 1))
        snap2 = BoardSnapshot(
            "9984", base + 200_000_000, 101.0, 102.0, 1200, 180,
            bids=(Level(101.0, 1200), Level(100.0, 700)),
            asks=(Level(102.0, 180), Level(103.0, 220)),
        )
        result = strategy.on_board(snap2, now_ns=snap2.ts_ns)
        if result.intent is not None:
            strategy.on_broker_fill(
                BrokerFillEvent(
                    order_id=result.intent.client_order_id,
                    qty=result.intent.qty,
                    price=snap2.ask,
                    ts_ns=snap2.ts_ns,
                )
            )
            # Next board event should yield exit_intent (TP)
            snap3 = BoardSnapshot(
                "9984", base + 300_000_000, 101.0, 102.0, 1200, 180,
                bids=(Level(101.0, 1200),),
                asks=(Level(102.0, 180),),
            )
            result2 = strategy.on_board(snap3, now_ns=snap3.ts_ns)
            # Either exit_intent is set (TP ready) or lollipop_active blocks entry
            self.assertIn(result2.blocked_reason, {"lollipop_active", "working_entry", ""})

    def test_no_entry_while_lollipop_active(self) -> None:
        from kabu_maker_taker.models import BrokerFillEvent, TradePrint

        strategy = self._make_strategy()
        base = 1_770_000_000_000_000_000

        strategy.on_trade(TradePrint("9984", base, 100.8, 500, 1))
        snap1 = BoardSnapshot(
            "9984", base + 100_000_000, 100.0, 101.0, 900, 200,
            bids=(Level(100.0, 900), Level(99.0, 500)),
            asks=(Level(101.0, 200), Level(102.0, 250)),
        )
        result = strategy.on_board(snap1, now_ns=snap1.ts_ns)

        if result.intent is not None:
            strategy.on_broker_fill(
                BrokerFillEvent(
                    order_id=result.intent.client_order_id,
                    qty=result.intent.qty,
                    price=snap1.ask,
                    ts_ns=snap1.ts_ns,
                )
            )
            self.assertTrue(strategy.lollipop.is_busy)

            snap2 = BoardSnapshot(
                "9984", base + 200_000_000, 101.0, 102.0, 1200, 180,
                bids=(Level(101.0, 1200),),
                asks=(Level(102.0, 180),),
            )
            result2 = strategy.on_board(snap2, now_ns=snap2.ts_ns)
            self.assertIsNone(result2.intent, "should not open new entry while lollipop active")


class ForceExitOneShotTests(unittest.TestCase):
    """Verify that TIMEOUT emits force_exit exactly once until reset."""

    def _make_timeout_mgr(self) -> LollipopTPManager:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        # Advance to ACTIVE
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=0, **_KW)
        # Manually push to TIMEOUT
        mgr.force_exit_next_tick()
        return mgr

    def test_force_exit_emitted_only_once_per_timeout(self) -> None:
        """First tick in TIMEOUT → force_exit; second tick → none (already emitted)."""
        mgr = self._make_timeout_mgr()
        pos = _pos(100.0)

        action1 = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action1.action, "force_exit")
        self.assertIsNotNone(action1.intent)
        self.assertTrue(action1.intent.is_market)

        action2 = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action2.action, "none")

    def test_force_exit_emitted_again_after_reset(self) -> None:
        """After reset_force_exit(), next tick re-emits force_exit."""
        mgr = self._make_timeout_mgr()
        pos = _pos(100.0)

        mgr.tick(_snap(), pos, now_ns=0, **_KW)  # first emission
        mgr.reset_force_exit()

        action = mgr.tick(_snap(), pos, now_ns=0, **_KW)
        self.assertEqual(action.action, "force_exit")
        self.assertIsNotNone(action.intent)

    def test_position_flat_in_timeout_resets_to_idle(self) -> None:
        """position.qty=0 while in TIMEOUT → tick() returns none and state goes IDLE."""
        mgr = self._make_timeout_mgr()
        flat_pos = PositionState()  # qty=0

        action = mgr.tick(_snap(), flat_pos, now_ns=0, **_KW)
        self.assertEqual(action.action, "none")
        self.assertEqual(mgr.phase, LollipopPhase.IDLE)
        self.assertFalse(mgr.is_busy)

    def test_canceled_exit_in_timeout_resets_force_exit_flag(self) -> None:
        """CANCELED exit order while in TIMEOUT: force_exit_requested becomes False."""
        from kabu_maker_taker.models import BrokerFillEvent, BrokerOrderEvent, OrderStatus, TradePrint

        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig

        config = AppConfig(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            lollipop=LollipopConfig(
                maker_tp_ticks=2.0,
                taker_tp_ticks=3.0,
                tp_delay_ms=0,
                maker_max_hold_seconds=0,
                taker_max_hold_seconds=0,
                max_retries=3,
            ),
        )
        strategy = CombinedMakerTakerStrategy(config)

        # Simulate an entry fill to put lollipop into ACTIVE, then TIMEOUT
        strategy.lollipop.on_entry_fill(100.0, "maker", now_ns=0, entry_side=1)
        pos = _pos(100.0)
        strategy.position.side = 1
        strategy.position.qty = 100
        strategy.position.avg_price = 100.0

        # Manually enter TIMEOUT
        strategy.lollipop.force_exit_next_tick()
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.TIMEOUT)

        # Emit the force_exit (sets force_exit_requested=True)
        strategy.lollipop.tick(_snap(), strategy.position, 0, **_KW)
        self.assertTrue(strategy.lollipop.state.force_exit_requested)

        # Add a fake working exit order to the ledger so on_broker_order_event can find it
        from kabu_maker_taker.models import OrderIntent
        from kabu_maker_taker.strategy import ORDER_ROLE_EXIT

        intent = OrderIntent(
            symbol="9984", exchange=27, side=-1, qty=100,
            price=0.0, is_market=True, strategy="lollipop_tp",
            reason="timeout_exit", score=0, reference_price=100.0,
        )
        tracked = strategy.orders.add_intent(intent, role=ORDER_ROLE_EXIT, now_ns=0)
        oid = tracked.client_order_id

        cancel_event = BrokerOrderEvent(order_id=oid, status=OrderStatus.CANCELED)
        strategy.on_broker_order_event(cancel_event)

        # force_exit_requested should be cleared so we can retry
        self.assertFalse(strategy.lollipop.state.force_exit_requested)

    def test_canceled_exit_in_active_reschedules(self) -> None:
        """CANCELED exit order while in ACTIVE phase → lollipop goes back to SCHEDULED."""
        from kabu_maker_taker.models import BrokerOrderEvent, OrderStatus, OrderIntent
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.config import AppConfig, LollipopConfig
        from kabu_maker_taker.strategy import ORDER_ROLE_EXIT

        config = AppConfig(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            lollipop=LollipopConfig(
                maker_tp_ticks=2.0,
                taker_tp_ticks=3.0,
                tp_delay_ms=0,
                maker_max_hold_seconds=300,
                taker_max_hold_seconds=300,
                max_retries=3,
            ),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.lollipop.on_entry_fill(100.0, "maker", now_ns=0, entry_side=1)
        strategy.position.side = 1
        strategy.position.qty = 100
        strategy.position.avg_price = 100.0

        # Advance to ACTIVE via tick
        strategy.lollipop.tick(_snap(), strategy.position, 0, **_KW)
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.ACTIVE)

        # Add a fake working exit order and cancel it
        intent = OrderIntent(
            symbol="9984", exchange=27, side=-1, qty=100,
            price=102.0, is_market=False, strategy="lollipop_tp",
            reason="limit_tp", score=0, reference_price=100.0,
        )
        tracked = strategy.orders.add_intent(intent, role=ORDER_ROLE_EXIT, now_ns=0)
        oid = tracked.client_order_id

        cancel_event = BrokerOrderEvent(order_id=oid, status=OrderStatus.CANCELED)
        strategy.on_broker_order_event(cancel_event)

        self.assertEqual(strategy.lollipop.phase, LollipopPhase.SCHEDULED)


class ScaleInFillTests(unittest.TestCase):
    """Verify on_scale_in_fill() updates tp_price without resetting the state machine."""

    def _make_active_mgr(self) -> tuple[LollipopTPManager, PositionState]:
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=0, **_KW)  # SCHEDULED → ACTIVE
        self.assertEqual(mgr.phase, LollipopPhase.ACTIVE)
        return mgr, pos

    def test_scale_in_while_scheduled_updates_tp_price(self) -> None:
        """Scale-in during SCHEDULED updates tp_price; phase stays SCHEDULED.

        With tick_size=1.0 and maker_tp_ticks=2.0, exit_side=-1 so _align_price floors:
          avg=100.0 → tp = floor(102.0) = 102.0
          avg=101.0 → tp = floor(103.0) = 103.0  (clearly different)
        """
        mgr = LollipopTPManager(_CFG, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        self.assertEqual(mgr.phase, LollipopPhase.SCHEDULED)
        old_tp = mgr.state.tp_price  # 102.0

        mgr.on_scale_in_fill(101.0, "maker", entry_side=1)
        self.assertEqual(mgr.phase, LollipopPhase.SCHEDULED)
        self.assertAlmostEqual(mgr.state.tp_price, 103.0, places=4)
        self.assertNotEqual(mgr.state.tp_price, old_tp)

    def test_scale_in_while_active_does_not_reset_state(self) -> None:
        """Scale-in during ACTIVE updates tp_price; phase stays ACTIVE; retry_count unchanged."""
        mgr, _ = self._make_active_mgr()
        old_retry = mgr.state.retry_count
        old_submit_after = mgr.state.submit_after_ns

        mgr.on_scale_in_fill(101.0, "maker", entry_side=1)

        self.assertEqual(mgr.phase, LollipopPhase.ACTIVE, "Phase must stay ACTIVE after scale-in")
        self.assertEqual(mgr.state.retry_count, old_retry, "retry_count must not reset")
        self.assertEqual(mgr.state.submit_after_ns, old_submit_after, "submit_after_ns must not reset")
        self.assertAlmostEqual(mgr.state.tp_price, 103.0, places=4)

    def test_scale_in_while_timeout_preserves_timeout(self) -> None:
        """Scale-in during TIMEOUT only updates tp_price; phase stays TIMEOUT."""
        mgr, _ = self._make_active_mgr()
        mgr.force_exit_next_tick()
        self.assertEqual(mgr.phase, LollipopPhase.TIMEOUT)

        mgr.on_scale_in_fill(100.2, "maker", entry_side=1)

        self.assertEqual(mgr.phase, LollipopPhase.TIMEOUT,
                         "TIMEOUT phase must not change on scale-in")

    def test_scale_in_via_combined_does_not_double_submit_tp(self) -> None:
        """combined._apply_position_fill() with scale-in does not reset ACTIVE lollipop."""
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.config import AppConfig, LollipopConfig
        from kabu_maker_taker.models import BrokerFillEvent

        config = AppConfig(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            lollipop=LollipopConfig(
                maker_tp_ticks=2.0,
                taker_tp_ticks=3.0,
                tp_delay_ms=0,
                maker_max_hold_seconds=300,
                taker_max_hold_seconds=300,
                max_retries=3,
            ),
        )
        strategy = CombinedMakerTakerStrategy(config)

        # Simulate first entry fill (IDLE → SCHEDULED).
        # Use the tracked order's client_order_id (OrderIntent.client_order_id defaults to "").
        from kabu_maker_taker.models import OrderIntent
        from kabu_maker_taker.strategy import ORDER_ROLE_ENTRY
        entry_intent = OrderIntent(
            symbol="9984", exchange=27, side=1, qty=100, price=101.0,
            is_market=False, strategy="taker", reason="taker_entry", score=10,
            reference_price=101.0,
        )
        tracked1 = strategy.orders.add_intent(entry_intent, role=ORDER_ROLE_ENTRY, now_ns=0)
        strategy.on_broker_fill(BrokerFillEvent(
            order_id=tracked1.client_order_id,
            qty=100, price=101.0, ts_ns=0,
        ))
        # Advance lollipop to ACTIVE via tick
        strategy.lollipop.tick(_snap(bid=100.0, ask=101.0), strategy.position, 0, **_KW)
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.ACTIVE)

        # Simulate scale-in fill (same side)
        entry_intent2 = OrderIntent(
            symbol="9984", exchange=27, side=1, qty=100, price=101.5,
            is_market=False, strategy="taker", reason="taker_entry", score=10,
            reference_price=101.5,
        )
        tracked2 = strategy.orders.add_intent(entry_intent2, role=ORDER_ROLE_ENTRY, now_ns=0)
        strategy.on_broker_fill(BrokerFillEvent(
            order_id=tracked2.client_order_id,
            qty=100, price=101.5, ts_ns=1_000_000,
        ))

        # After scale-in, lollipop must still be ACTIVE (not SCHEDULED)
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.ACTIVE,
                         "Scale-in must not reset lollipop from ACTIVE to SCHEDULED")


class RejectedForceExitTests(unittest.TestCase):
    """Verify that a REJECTED force-exit order correctly resets the lollipop
    so the next tick can re-emit a fresh force_exit."""

    def _make_combined_in_timeout(self):
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.config import AppConfig, LollipopConfig
        from kabu_maker_taker.models import OrderIntent
        from kabu_maker_taker.strategy import ORDER_ROLE_EXIT

        config = AppConfig(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            lollipop=LollipopConfig(
                maker_tp_ticks=2.0,
                taker_tp_ticks=3.0,
                tp_delay_ms=0,
                maker_max_hold_seconds=300,
                taker_max_hold_seconds=300,
                max_retries=3,
            ),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.lollipop.on_entry_fill(100.0, "maker", now_ns=0, entry_side=1)
        strategy.position.side = 1
        strategy.position.qty = 100
        strategy.position.avg_price = 100.0

        # Push directly to TIMEOUT
        strategy.lollipop.force_exit_next_tick()
        # Emit first force_exit (sets force_exit_requested=True)
        strategy.lollipop.tick(_snap(), strategy.position, 0, **_KW)

        # Register a fake working exit order so _handle_final_order_state triggers
        intent = OrderIntent(
            symbol="9984", exchange=27, side=-1, qty=100,
            price=0.0, is_market=True, strategy="lollipop_tp",
            reason="timeout_exit", score=0, reference_price=100.0,
        )
        tracked = strategy.orders.add_intent(intent, role=ORDER_ROLE_EXIT, now_ns=0)
        return strategy, tracked.client_order_id

    def test_rejected_force_exit_in_timeout_resets_flag(self) -> None:
        """REJECTED exit while in TIMEOUT: force_exit_requested cleared → retry possible."""
        from kabu_maker_taker.models import BrokerOrderEvent, OrderStatus

        strategy, oid = self._make_combined_in_timeout()
        self.assertTrue(strategy.lollipop.state.force_exit_requested)

        reject_event = BrokerOrderEvent(order_id=oid, status=OrderStatus.REJECTED)
        strategy.on_broker_order_event(reject_event)

        self.assertFalse(strategy.lollipop.state.force_exit_requested,
                         "REJECTED in TIMEOUT must reset force_exit_requested for retry")

    def test_rejected_force_exit_in_timeout_allows_reemission(self) -> None:
        """After REJECTED reset, the next tick() call re-emits a force_exit."""
        from kabu_maker_taker.models import BrokerOrderEvent, OrderStatus

        strategy, oid = self._make_combined_in_timeout()
        reject_event = BrokerOrderEvent(order_id=oid, status=OrderStatus.REJECTED)
        strategy.on_broker_order_event(reject_event)

        action = strategy.lollipop.tick(_snap(), strategy.position, 0, **_KW)
        self.assertEqual(action.action, "force_exit",
                         "After REJECTED+reset, next tick should re-emit force_exit")

    def test_rejected_exit_outside_timeout_calls_force_exit_next_tick(self) -> None:
        """REJECTED exit while in ACTIVE phase → lollipop transitions to TIMEOUT."""
        from kabu_maker_taker.combined import CombinedMakerTakerStrategy
        from kabu_maker_taker.config import AppConfig, LollipopConfig
        from kabu_maker_taker.models import BrokerOrderEvent, OrderIntent, OrderStatus
        from kabu_maker_taker.strategy import ORDER_ROLE_EXIT

        config = AppConfig(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            lollipop=LollipopConfig(
                maker_tp_ticks=2.0,
                taker_tp_ticks=3.0,
                tp_delay_ms=0,
                maker_max_hold_seconds=300,
                taker_max_hold_seconds=300,
                max_retries=3,
            ),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.lollipop.on_entry_fill(100.0, "maker", now_ns=0, entry_side=1)
        strategy.position.side = 1
        strategy.position.qty = 100
        strategy.position.avg_price = 100.0
        # Advance to ACTIVE
        strategy.lollipop.tick(_snap(), strategy.position, 0, **_KW)
        self.assertEqual(strategy.lollipop.phase, LollipopPhase.ACTIVE)

        intent = OrderIntent(
            symbol="9984", exchange=27, side=-1, qty=100,
            price=102.0, is_market=False, strategy="lollipop_tp",
            reason="limit_tp", score=0, reference_price=100.0,
        )
        tracked = strategy.orders.add_intent(intent, role=ORDER_ROLE_EXIT, now_ns=0)
        reject_event = BrokerOrderEvent(order_id=tracked.client_order_id, status=OrderStatus.REJECTED)
        strategy.on_broker_order_event(reject_event)

        self.assertEqual(strategy.lollipop.phase, LollipopPhase.TIMEOUT,
                         "REJECTED exit in ACTIVE should escalate to TIMEOUT")


class StopLossZeroBidTests(unittest.TestCase):
    """Verify stop-loss is not falsely triggered when snapshot prices are zero."""

    def test_stop_loss_not_triggered_when_bid_is_zero(self) -> None:
        """bid=0 must not cause spurious stop-loss force_exit."""
        cfg = LollipopConfig(
            maker_tp_ticks=2.0,
            taker_tp_ticks=3.0,
            maker_max_hold_seconds=300,
            taker_max_hold_seconds=300,
            tp_delay_ms=0,
            max_retries=5,
            stop_loss_ticks=2.0,
        )
        mgr = LollipopTPManager(cfg, _TICK, _LOT)
        mgr.on_entry_fill(100.0, "maker", now_ns=0)
        pos = _pos(100.0)
        mgr.tick(_snap(), pos, now_ns=0, **_KW)  # → ACTIVE

        zero_snap = BoardSnapshot(
            symbol="9984",
            ts_ns=1_000_000_000,
            bid=0.0,
            ask=0.0,
            bid_size=0,
            ask_size=0,
            bids=(),
            asks=(),
        )
        action = mgr.tick(zero_snap, pos, now_ns=1_000_000_000, **_KW)
        self.assertEqual(action.action, "none",
                         "Zero bid/ask must not trigger stop-loss force_exit")
        self.assertEqual(mgr.phase, LollipopPhase.ACTIVE)


if __name__ == "__main__":
    unittest.main()
