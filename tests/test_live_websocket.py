from __future__ import annotations

import io
import json
import time
import unittest
from contextlib import redirect_stdout

from kabu_maker_taker.broker import BrokerReconciliationSnapshot
from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, KabuConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.kabu_rest import LiveExecutionResult
from kabu_maker_taker.live_runtime import process_live_board, run_websocket_live
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
    def __init__(self) -> None:
        self.polled = 0
        self.submitted = []
        self.registered = 0
        self.unregistered = 0
        self.snapshots = 0

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
        event = BrokerOrderEvent(
            order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=OrderStatus.CANCELED,
            ts_ns=now_ns,
        )
        return LiveExecutionResult(events=(event,), api_success=True)

    def snapshot(self) -> BrokerReconciliationSnapshot:
        self.snapshots += 1
        return BrokerReconciliationSnapshot(ts_ns=time.time_ns())

    def open_order_snapshots(self):
        return ()

    def position_snapshot(self):
        return ()


class RaisingWebSocket:
    def recv(self):
        raise RuntimeError("socket down")

    def close(self) -> None:
        pass


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


if __name__ == "__main__":
    unittest.main()
