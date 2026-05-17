"""Tests for ReplayRunner and ReplayResult — offline event-driven backtesting."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, LollipopConfig, RiskConfig, StrategyConfig
from kabu_maker_taker.models import BoardSnapshot, BrokerFillEvent, BrokerOrderEvent, EntryDecision, MarketState, SignalPacket
from kabu_maker_taker.replay import ReplayResult, ReplayRunner, _handle_exit_cancel_signal_sim, read_jsonl
from kabu_maker_taker.simulator import DryRunSimulator


def _config() -> AppConfig:
    return AppConfig(
        symbol="9984",
        tick_size=1.0,
        lot_size=100,
        strategy=StrategyConfig(trade_qty=100),
        risk=RiskConfig(
            max_spread_ticks=5.0,
            fee_per_share=0.0,
            slippage_ticks_default=0.0,
        ),
        lollipop=LollipopConfig(tp_delay_ms=0, stop_loss_ticks=0.0),
    )


def _write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _board(ts_ns: int, bid: float = 100.0, ask: float = 101.0) -> dict:
    return {
        "type": "board",
        "symbol": "9984",
        "exchange": 27,
        "ts_ns": ts_ns,
        "bid": bid, "ask": ask,
        "bid_size": 1000, "ask_size": 300,
        "bids": [{"price": bid, "size": 1000}, {"price": bid - 1, "size": 500}],
        "asks": [{"price": ask, "size": 300}, {"price": ask + 1, "size": 400}],
    }


def _trade(ts_ns: int, price: float = 100.5, size: int = 200, side: int = 1) -> dict:
    return {
        "type": "trade",
        "symbol": "9984",
        "exchange": 27,
        "ts_ns": ts_ns,
        "price": price,
        "size": size,
        "side": side,
    }


def _signal(ts_ns: int) -> SignalPacket:
    return SignalPacket(
        ts_ns=ts_ns,
        obi_raw=0.35,
        lob_ofi_raw=0.20,
        tape_ofi_raw=0.20,
        micro_momentum_raw=0.10,
        microprice_tilt_raw=0.30,
        microprice=100.3,
        mid=100.0,
        obi_z=0.0,
        lob_ofi_z=0.0,
        tape_ofi_z=0.0,
        micro_momentum_z=0.0,
        microprice_tilt_z=0.0,
        composite=0.50,
        integrated_ofi=0.20,
        trade_burst_score=0.10,
    )


class ReadJsonlTests(unittest.TestCase):

    def test_read_jsonl_yields_parsed_dicts(self) -> None:
        """read_jsonl() yields all non-empty lines as dicts."""
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write('{"a": 1}\n\n{"b": 2}\n')
            path = Path(fh.name)
        rows = list(read_jsonl(path))
        path.unlink()
        self.assertEqual(rows, [{"a": 1}, {"b": 2}])


class ReplayRunnerTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _events_path(self, events: list[dict]) -> Path:
        path = self._dir / "events.jsonl"
        _write_events(path, events)
        return path

    def test_run_on_empty_file_returns_zero_trades(self) -> None:
        """Replaying an empty JSONL file returns a zero-trade ReplayResult."""
        path = self._dir / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        runner = ReplayRunner(_config())
        result = runner.run(path)
        self.assertIsInstance(result, ReplayResult)
        self.assertEqual(result.trade_count, 0)
        self.assertEqual(result.total_pnl, 0.0)

    def test_run_on_board_events_only_returns_result(self) -> None:
        """ReplayRunner processes board events without error, returns valid ReplayResult."""
        base = 1_700_000_000_000_000_000
        events = [_board(base + i * 100_000_000) for i in range(5)]
        runner = ReplayRunner(_config())
        result = runner.run(self._events_path(events))
        self.assertIsInstance(result, ReplayResult)
        # No trades without signals — zero entries expected
        self.assertEqual(result.trade_count, 0)

    def test_run_processes_trade_events(self) -> None:
        """Trade events are consumed without error."""
        base = 1_700_000_000_000_000_000
        events = [
            _trade(base),
            _board(base + 100_000_000),
            _trade(base + 200_000_000),
            _board(base + 300_000_000),
        ]
        runner = ReplayRunner(_config())
        result = runner.run(self._events_path(events))
        self.assertIsInstance(result, ReplayResult)

    def test_replay_result_fields_present(self) -> None:
        """ReplayResult dataclass has all expected fields."""
        r = ReplayResult()
        self.assertEqual(r.trade_count, 0)
        self.assertEqual(r.win_rate, 0.0)
        self.assertEqual(r.avg_pnl_per_trade, 0.0)
        self.assertEqual(r.total_pnl, 0.0)
        self.assertEqual(r.max_drawdown, 0.0)
        self.assertEqual(r.sharpe, 0.0)
        self.assertEqual(r.fill_rate, 0.0)
        self.assertEqual(r.entry_count, 0)
        self.assertEqual(r.taker_entry_count, 0)

    def test_each_run_is_independent(self) -> None:
        """Two runs on the same file produce equal results — no shared state."""
        base = 1_700_000_000_000_000_000
        events = [_board(base + i * 100_000_000) for i in range(4)]
        path = self._events_path(events)
        runner = ReplayRunner(_config())
        r1 = runner.run(path)
        r2 = runner.run(path)
        self.assertEqual(r1.trade_count, r2.trade_count)
        self.assertEqual(r1.total_pnl, r2.total_pnl)
        self.assertEqual(r1.entry_count, r2.entry_count)

    def test_fill_rate_is_zero_without_taker_entries(self) -> None:
        """fill_rate = 0 when no taker orders are submitted."""
        base = 1_700_000_000_000_000_000
        events = [_board(base + i * 100_000_000) for i in range(3)]
        runner = ReplayRunner(_config())
        result = runner.run(self._events_path(events))
        self.assertEqual(result.fill_rate, 0.0)

    def test_max_drawdown_is_non_negative(self) -> None:
        """max_drawdown is always >= 0.0 regardless of trade sequence."""
        base = 1_700_000_000_000_000_000
        # Mix of board and trade events; no real signal-driven trades expected,
        # but the invariant must hold in all cases.
        events = (
            [_board(base + i * 100_000_000) for i in range(5)]
            + [_trade(base + 600_000_000)]
            + [_board(base + 700_000_000 + i * 100_000_000) for i in range(5)]
        )
        runner = ReplayRunner(_config())
        result = runner.run(self._events_path(events))
        self.assertGreaterEqual(result.max_drawdown, 0.0)

    def test_result_fields_are_consistent(self) -> None:
        """win_rate and avg_pnl_per_trade are 0.0 when trade_count is 0."""
        base = 1_700_000_000_000_000_000
        events = [_board(base + i * 100_000_000) for i in range(4)]
        runner = ReplayRunner(_config())
        result = runner.run(self._events_path(events))
        if result.trade_count == 0:
            self.assertEqual(result.win_rate, 0.0)
            self.assertEqual(result.avg_pnl_per_trade, 0.0)
            self.assertEqual(result.sharpe, 0.0)

    def test_replay_exit_cancel_helper_releases_deferred_force_exit(self) -> None:
        config = AppConfig(
            symbol="9984",
            tick_size=1.0,
            lot_size=1,
            strategy=StrategyConfig(trade_qty=100, maker_confirm_ticks=1, taker_confirm_ticks=1),
            risk=RiskConfig(max_inventory_qty=300, max_spread_ticks=5.0),
            lollipop=LollipopConfig(tp_delay_ms=0, maker_max_hold_seconds=0, taker_max_hold_seconds=0),
        )
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
        simulator = DryRunSimulator(tick_size=1.0)
        entry_snapshot = BoardSnapshot.from_dict(_board(1_000_000_000))
        entry = strategy.on_board(entry_snapshot, now_ns=entry_snapshot.ts_ns)
        assert entry.intent is not None
        strategy.on_broker_fill(
            BrokerFillEvent(order_id=entry.intent.client_order_id, qty=100, price=101.0, ts_ns=1_000_000_100)
        )
        tp_snapshot = BoardSnapshot.from_dict(_board(1_000_000_200))
        tp = strategy.on_board(tp_snapshot, now_ns=tp_snapshot.ts_ns)
        assert tp.exit_intent is not None
        for event in simulator.submit(tp.exit_intent, tp_snapshot, tp_snapshot.ts_ns):
            if isinstance(event, BrokerOrderEvent):
                strategy.on_broker_order_event(event)

        timeout_snapshot = _board(1_000_000_300, bid=99.0, ask=100.0)
        timeout_board = BoardSnapshot.from_dict(timeout_snapshot)
        timeout = strategy.on_board(timeout_board, now_ns=timeout_board.ts_ns)
        self.assertEqual(timeout.exit_cancel_signal, "replace_active_exit_before_force_exit")

        _handle_exit_cancel_signal_sim(
            strategy,
            simulator,
            timeout.exit_cancel_signal,
            timeout_board,
            timeout_board.ts_ns,
        )
        self.assertEqual(strategy.position.qty, 0)
        self.assertEqual(strategy.working_exit_ids, [])


if __name__ == "__main__":
    unittest.main()
