from __future__ import annotations

import io
import json
import time
import tempfile
import unittest
from contextlib import redirect_stdout

from kabu_maker_taker.broker import BrokerOpenOrderSnapshot, BrokerPositionSnapshot, BrokerReconciliationSnapshot
from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, KabuConfig, MarketStateConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.kabu_rest import LiveExecutionResult
from kabu_maker_taker.live_runtime import (
    perform_live_preflight,
    process_live_board,
    run_websocket_live,
    validate_live_preflight_stamp,
    write_live_preflight_stamp,
)
from kabu_maker_taker.models import (
    BoardSnapshot,
    BrokerOrderEvent,
    EntryDecision,
    Level,
    MarketState,
    OrderStatus,
    SignalPacket,
)


def _signal(ts_ns: int) -> SignalPacket:
    return SignalPacket(
        ts_ns=ts_ns,
        obi_raw=0.35,
        lob_ofi_raw=0.20,
        tape_ofi_raw=0.20,
        micro_momentum_raw=0.10,
        microprice_tilt_raw=0.30,
        microprice=100.3,
        mid=100.5,
        obi_z=0.0,
        lob_ofi_z=0.0,
        tape_ofi_z=0.0,
        micro_momentum_z=0.0,
        microprice_tilt_z=0.0,
        composite=0.50,
        integrated_ofi=0.20,
        trade_burst_score=0.10,
    )


def _snapshot(ts_ns: int) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984",
        exchange=27,
        ts_ns=ts_ns,
        bid=100.0,
        ask=101.0,
        bid_size=500,
        ask_size=200,
        bids=(Level(100.0, 500),),
        asks=(Level(101.0, 200),),
    )


def _config(**kabu_overrides) -> AppConfig:
    return AppConfig(
        symbol="9984",
        exchange=27,
        tick_size=1.0,
        lot_size=100,
        strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1),
        risk=RiskConfig(max_spread_ticks=5.0, stale_quote_ms=2_000),
        kabu=KabuConfig(api_password="pw", poll_interval_ms=0, **kabu_overrides),
    )


class FakeTracer:
    def __init__(self) -> None:
        self.rows = 0

    def record(self, *_args) -> None:
        self.rows += 1


class FakeExecutor:
    def __init__(self, *, positions=(), ignored_open_orders=()) -> None:
        self.polled = 0
        self.submitted = []
        self.canceled = []
        self.registered = 0
        self.unregistered = 0
        self.snapshots = 0
        self.positions = tuple(positions)
        self.ignored_open_orders = tuple(ignored_open_orders)

    def register_market_data(self) -> None:
        self.registered += 1

    def unregister_market_data(self) -> None:
        self.unregistered += 1

    def poll_order_events(self, _active, *, now_ns: int = 0) -> LiveExecutionResult:
        _ = now_ns
        self.polled += 1
        return LiveExecutionResult(api_success=True)

    def submit(self, intent, *, role: str, now_ns: int = 0) -> LiveExecutionResult:
        self.submitted.append((intent, role))
        event = BrokerOrderEvent(
            order_id=intent.client_order_id,
            broker_order_id=f"B-{intent.client_order_id}",
            status=OrderStatus.WORKING,
            ts_ns=now_ns,
        )
        return LiveExecutionResult(events=(event,), api_success=True)

    def cancel(self, order, *, now_ns: int = 0) -> LiveExecutionResult:
        self.canceled.append(order)
        event = BrokerOrderEvent(
            order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=OrderStatus.CANCELED,
            ts_ns=now_ns,
        )
        return LiveExecutionResult(events=(event,), api_success=True)

    def snapshot(self) -> BrokerReconciliationSnapshot:
        self.snapshots += 1
        return BrokerReconciliationSnapshot(
            ts_ns=time.time_ns(),
            positions=self.positions,
            ignored_open_orders=self.ignored_open_orders,
        )

    def open_order_snapshots(self):
        return ()

    def position_snapshot(self):
        return ()


class RaisingWebSocket:
    def recv(self):
        raise RuntimeError("socket down")

    def close(self) -> None:
        pass


class SequenceWebSocket:
    def __init__(self, payloads) -> None:
        self.payloads = list(payloads)
        self.closed = False

    def recv(self):
        if not self.payloads:
            raise TimeoutError("no more websocket messages")
        return self.payloads.pop(0)

    def close(self) -> None:
        self.closed = True


def _websocket_payload(ts_ns: int, *, symbol: str = "9984", exchange: int = 27) -> str:
    return json.dumps(
        {
            "Symbol": symbol,
            "Exchange": exchange,
            "BidPrice": 100.0,
            "AskPrice": 101.0,
            "BidQty": 500,
            "AskQty": 200,
            "BidTimeNs": ts_ns,
        }
    )


def _null_quote_websocket_payload(ts_ns: int, *, symbol: str = "9984", exchange: int = 27) -> str:
    return json.dumps(
        {
            "Symbol": symbol,
            "Exchange": exchange,
            "BidPrice": None,
            "AskPrice": None,
            "BidQty": None,
            "AskQty": None,
            "CurrentPrice": None,
            "BidTimeNs": ts_ns,
        }
    )


class LiveWebSocketTests(unittest.TestCase):
    def test_process_live_board_polls_traces_and_submits_entry(self) -> None:
        config = _config()
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(snapshot.ts_ns)
        strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            True,
            "",
            entry_mode="maker",
            side=1,
            entry_score=8,
            required_confirm=1,
        )
        executor = FakeExecutor()
        tracer = FakeTracer()

        with redirect_stdout(io.StringIO()):
            halt = process_live_board(strategy, executor, config, tracer, _snapshot(time.time_ns()), time.time_ns())

        self.assertEqual(halt, "")
        self.assertEqual(tracer.rows, 1)
        self.assertEqual(len(executor.submitted), 1)
        self.assertGreaterEqual(executor.polled, 2)

    def test_websocket_disconnect_reconnects_when_flat(self) -> None:
        config = _config(websocket_reconnect_attempts=1)
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(snapshot.ts_ns)
        strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            False,
            "no_entry",
        )
        executor = FakeExecutor()
        tracer = FakeTracer()

        def factory(_url, timeout=0):
            _ = timeout
            return RaisingWebSocket()

        with redirect_stdout(io.StringIO()) as stdout:
            code = run_websocket_live(strategy, executor, config, tracer, websocket_factory=factory)

        self.assertEqual(code, 3)
        self.assertEqual(executor.registered, 2)
        self.assertEqual(executor.unregistered, 2)
        self.assertEqual(executor.snapshots, 1)
        self.assertIn("live_websocket_reconnect", stdout.getvalue())

    def test_board_snapshot_uses_quote_timestamps_for_websocket_payload(self) -> None:
        now_ns = time.time_ns()
        payload = {
            "Symbol": "9984",
            "Exchange": 27,
            "BidPrice": 100.0,
            "AskPrice": 101.0,
            "BidQty": 500,
            "AskQty": 200,
            "BidTimeNs": now_ns,
        }

        snapshot = BoardSnapshot.from_dict(payload)

        self.assertEqual(snapshot.ts_ns, now_ns)

    def test_preflight_success_reads_fresh_boards_without_orders(self) -> None:
        config = _config(websocket_preflight_messages=2, websocket_preflight_timeout_s=1.0)
        executor = FakeExecutor()
        now_ns = time.time_ns()
        ws = SequenceWebSocket([_websocket_payload(now_ns), _websocket_payload(now_ns + 1_000_000)])

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertEqual(summary["received_boards"], 2)
        self.assertEqual(executor.registered, 1)
        self.assertEqual(executor.unregistered, 1)
        self.assertEqual(executor.submitted, [])
        self.assertEqual(executor.canceled, [])
        self.assertTrue(ws.closed)

    def test_preflight_ignores_other_symbol_boards(self) -> None:
        config = _config(websocket_preflight_messages=1, websocket_preflight_timeout_s=1.0)
        executor = FakeExecutor()
        now_ns = time.time_ns()
        ws = SequenceWebSocket(
            [
                _websocket_payload(now_ns, symbol="7203"),
                _websocket_payload(now_ns + 1_000_000, symbol="9984"),
            ]
        )

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertEqual(summary["received_boards"], 1)
        self.assertEqual(summary["ignored_boards"], 1)

    def test_preflight_ignores_null_quote_boards_until_valid_quote(self) -> None:
        config = _config(websocket_preflight_messages=1, websocket_preflight_timeout_s=1.0)
        executor = FakeExecutor()
        now_ns = time.time_ns()
        ws = SequenceWebSocket(
            [
                _null_quote_websocket_payload(now_ns),
                _websocket_payload(now_ns + 1_000_000),
            ]
        )

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertEqual(summary["received_boards"], 1)
        self.assertEqual(summary["ignored_boards"], 1)

    def test_preflight_accepts_tse_board_for_sor_trading_exchange(self) -> None:
        config = AppConfig(
            symbol="9984",
            exchange=9,
            tick_size=1.0,
            lot_size=100,
            risk=RiskConfig(max_spread_ticks=5.0, stale_quote_ms=2_000),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0, websocket_preflight_messages=1),
        )
        executor = FakeExecutor()
        ws = SequenceWebSocket([_websocket_payload(time.time_ns(), exchange=1)])

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertEqual(summary["trade_exchange"], 9)
        self.assertEqual(summary["register_exchange"], 1)

    def test_preflight_accepts_stale_quote_as_connectivity_warning(self) -> None:
        config = AppConfig(
            symbol="9984",
            exchange=27,
            tick_size=1.0,
            lot_size=100,
            risk=RiskConfig(max_spread_ticks=5.0, stale_quote_ms=2_000),
            market_state=MarketStateConfig(enabled=True),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0, websocket_preflight_messages=1),
        )
        executor = FakeExecutor()
        old_ts = time.time_ns() - 10_000_000_000
        ws = SequenceWebSocket([_websocket_payload(old_ts)])

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertEqual(summary["received_boards"], 1)
        self.assertEqual(summary["stale_boards"], 1)
        self.assertEqual(summary["market_state_reason"], "stale_quote")

    def test_preflight_allows_partial_board_count_after_first_valid_board(self) -> None:
        config = _config(websocket_preflight_messages=3, websocket_preflight_timeout_s=1.0)
        executor = FakeExecutor()
        ws = SequenceWebSocket([_websocket_payload(time.time_ns())])

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertTrue(summary["preflight_partial"])
        self.assertEqual(summary["received_boards"], 1)
        self.assertEqual(summary["required_boards"], 3)

    def test_websocket_live_accepts_tse_board_for_sor_trading_exchange(self) -> None:
        config = AppConfig(
            symbol="9984",
            exchange=9,
            tick_size=1.0,
            lot_size=100,
            strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1),
            risk=RiskConfig(max_spread_ticks=5.0, stale_quote_ms=2_000),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0, websocket_reconnect_attempts=0),
        )
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(snapshot.ts_ns)
        strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            False,
            "no_entry",
        )
        executor = FakeExecutor()
        tracer = FakeTracer()
        ws = SequenceWebSocket([_websocket_payload(time.time_ns(), exchange=1)])

        with redirect_stdout(io.StringIO()):
            code = run_websocket_live(strategy, executor, config, tracer, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertEqual(code, 3)
        self.assertEqual(tracer.rows, 1)
        self.assertEqual(executor.submitted, [])

    def test_websocket_live_processes_stale_quote_without_immediate_halt(self) -> None:
        config = _config(websocket_reconnect_attempts=0)
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(snapshot.ts_ns)
        strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            False,
            "no_entry",
        )
        executor = FakeExecutor()
        tracer = FakeTracer()
        old_ts = time.time_ns() - 10_000_000_000
        ws = SequenceWebSocket([_websocket_payload(old_ts)])

        with redirect_stdout(io.StringIO()) as stdout:
            code = run_websocket_live(strategy, executor, config, tracer, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertEqual(code, 3)
        self.assertEqual(tracer.rows, 1)
        self.assertIn("websocket_reconnect_exhausted", stdout.getvalue())
        self.assertNotIn("websocket_stale", stdout.getvalue())

    def test_websocket_live_timeout_uses_stale_board_window(self) -> None:
        config = AppConfig(
            symbol="9984",
            exchange=27,
            risk=RiskConfig(max_spread_ticks=5.0, stale_quote_ms=2_000, stale_board_ms=30_000),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0, websocket_reconnect_attempts=0),
        )
        strategy = CombinedMakerTakerStrategy(config)
        executor = FakeExecutor()
        tracer = FakeTracer()
        timeouts = []

        def factory(_url, timeout=0):
            timeouts.append(timeout)
            return RaisingWebSocket()

        with redirect_stdout(io.StringIO()):
            run_websocket_live(strategy, executor, config, tracer, websocket_factory=factory)

        self.assertEqual(timeouts, [30.0])

    def test_preflight_rejects_broker_position(self) -> None:
        config = _config()
        executor = FakeExecutor(
            positions=(
                BrokerPositionSnapshot(
                    symbol="9984",
                    exchange=27,
                    side=1,
                    qty=100,
                    avg_price=100.0,
                    entry_mode="broker_unknown",
                ),
            )
        )

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: RaisingWebSocket())

        self.assertFalse(ok)
        self.assertEqual(summary["reason"], "broker_position_not_flat")
        self.assertEqual(executor.registered, 0)

    def test_preflight_allows_ignored_open_orders_and_reports_them(self) -> None:
        config = _config(websocket_preflight_messages=1, websocket_preflight_timeout_s=1.0)
        executor = FakeExecutor(
            ignored_open_orders=(
                BrokerOpenOrderSnapshot(
                    symbol="9984",
                    exchange=27,
                    side=1,
                    qty=100,
                    price=100.0,
                    role="entry",
                    broker_order_id="B-IGNORED",
                    strategy="broker_ignored",
                ),
            )
        )
        ws = SequenceWebSocket([_websocket_payload(time.time_ns())])

        ok, summary = perform_live_preflight(config, executor, websocket_factory=lambda *_args, **_kwargs: ws)

        self.assertTrue(ok, summary)
        self.assertEqual(summary["ignored_broker_open_orders"][0]["order_id"], "B-IGNORED")

    def test_preflight_stamp_must_be_today_and_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                symbol="9984",
                exchange=27,
                log_dir=tmp,
                kabu=KabuConfig(live_preflight_max_age_minutes=30),
            )
            now_ns = time.time_ns()
            write_live_preflight_stamp(config, now_ns, {"received_boards": 3})

            self.assertEqual(validate_live_preflight_stamp(config, now_ns=now_ns), "")
            old_ns = now_ns + 31 * 60 * 1_000_000_000
            self.assertIn("too old", validate_live_preflight_stamp(config, now_ns=old_ns))

    def test_shadow_process_live_board_does_not_submit_and_clears_local_order(self) -> None:
        config = _config()
        strategy = CombinedMakerTakerStrategy(config)
        strategy.signals.on_board = lambda snapshot: _signal(snapshot.ts_ns)
        strategy._choose_decision = lambda snapshot, sig, now_ns=0, market_state=MarketState.NORMAL: EntryDecision(
            True,
            "",
            entry_mode="maker",
            side=1,
            entry_score=8,
            required_confirm=1,
        )
        executor = FakeExecutor()
        tracer = FakeTracer()

        with redirect_stdout(io.StringIO()) as stdout:
            halt = process_live_board(
                strategy,
                executor,
                config,
                tracer,
                _snapshot(time.time_ns()),
                time.time_ns(),
                shadow=True,
            )

        self.assertEqual(halt, "")
        self.assertEqual(executor.submitted, [])
        self.assertEqual(strategy.orders.active(), [])
        self.assertFalse(strategy.entry_order_active)
        output = stdout.getvalue()
        self.assertIn("shadow_would_submit", output)
        self.assertIn("shadow_not_sent", json.dumps(strategy.orders.snapshot()))


if __name__ == "__main__":
    unittest.main()
