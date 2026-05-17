"""Tests for DecisionTraceWriter — per-board JSONL decision logging."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.models import (
    BoardSnapshot,
    EntryDecision,
    Level,
    MarketState,
    OrderIntent,
    PositionState,
    SignalPacket,
    StrategyResult,
)
from kabu_maker_taker.telemetry import DecisionTraceWriter


def _result(
    allowed: bool = False,
    reason: str = "confirming",
    blocked_reason: str = "confirming",
    with_intents: bool = False,
) -> StrategyResult:
    signal = SignalPacket(
        ts_ns=1_000_000_000,
        obi_raw=0.30, lob_ofi_raw=0.20, tape_ofi_raw=0.15,
        micro_momentum_raw=0.10, microprice_tilt_raw=0.25,
        microprice=100.3, mid=100.5,
        obi_z=0.8, lob_ofi_z=0.5, tape_ofi_z=0.4,
        micro_momentum_z=0.3, microprice_tilt_z=0.6,
        composite=0.55,
    )
    return StrategyResult(
        intent=OrderIntent(
            symbol="9984",
            exchange=27,
            side=1,
            qty=100,
            price=100.0,
            is_market=False,
            strategy="maker",
            reason="maker_passive_edge",
            score=8,
            reference_price=101.0,
            client_order_id="entry-1",
        ) if with_intents else None,
        decision=EntryDecision(allow=allowed, reason=reason, entry_mode="maker", side=1),
        signal=signal,
        blocked_reason=blocked_reason,
        exit_intent=OrderIntent(
            symbol="9984",
            exchange=27,
            side=-1,
            qty=100,
            price=102.0,
            is_market=False,
            strategy="lollipop_tp",
            reason="limit_tp",
            score=0,
            reference_price=100.0,
            client_order_id="exit-1",
        ) if with_intents else None,
        entry_cancel_signal="alpha_flip" if with_intents else "",
        entry_cancel_blocked_reason="cancel_rate_limit" if with_intents else "",
        exit_cancel_signal="replace_active_exit_before_force_exit" if with_intents else "",
        market_state=MarketState.NORMAL,
    )


class DecisionTraceWriterTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_record_writes_jsonl_line(self) -> None:
        """record() appends one parseable JSON line per call."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        pos = PositionState(side=0, qty=0)
        writer.record(_result(), pos, now_ns=1_000_000_000)
        writer.close()

        lines = (Path(self.log_dir) / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertIn("ts_ns", row)
        self.assertIn("market_state", row)
        self.assertIn("entry_allowed", row)
        self.assertIn("signal_composite", row)

    def test_record_multiple_lines(self) -> None:
        """Multiple record() calls accumulate multiple lines."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        pos = PositionState()
        for i in range(5):
            writer.record(_result(), pos, now_ns=i * 1_000_000_000)
        writer.close()

        lines = (Path(self.log_dir) / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 5)

    def test_record_is_noop_when_disabled(self) -> None:
        """enabled=False: record() does nothing and creates no file."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=False)
        writer.record(_result(), PositionState(), now_ns=1_000_000_000)
        writer.close()
        self.assertFalse((Path(self.log_dir) / "decisions.jsonl").exists())

    def test_record_fields_reflect_result(self) -> None:
        """Logged fields match the StrategyResult passed in."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        pos = PositionState(side=1, qty=100)
        r = _result(allowed=True, reason="ok", blocked_reason="")
        writer.record(r, pos, now_ns=2_000_000_000)
        writer.close()

        row = json.loads((Path(self.log_dir) / "decisions.jsonl").read_text(encoding="utf-8").strip())
        self.assertTrue(row["entry_allowed"])
        self.assertEqual(row["entry_reason"], "ok")
        self.assertEqual(row["position_qty"], 100)
        self.assertEqual(row["position_side"], 1)
        self.assertAlmostEqual(row["signal_composite"], 0.55, places=2)

    def test_ts_jst_present_for_nonzero_timestamp(self) -> None:
        """ts_jst is a non-empty string when now_ns > 0."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        writer.record(_result(), PositionState(), now_ns=1_700_000_000_000_000_000)
        writer.close()
        row = json.loads((Path(self.log_dir) / "decisions.jsonl").read_text(encoding="utf-8").strip())
        self.assertTrue(row["ts_jst"])  # non-empty string

    def test_ts_jst_empty_for_zero_timestamp(self) -> None:
        """ts_jst is '' when now_ns == 0."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        writer.record(_result(), PositionState(), now_ns=0)
        writer.close()
        row = json.loads((Path(self.log_dir) / "decisions.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(row["ts_jst"], "")

    def test_close_is_idempotent(self) -> None:
        """Calling close() twice does not raise."""
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        writer.close()
        writer.close()  # should not raise

    def test_record_includes_order_cancel_and_position_details(self) -> None:
        writer = DecisionTraceWriter(log_dir=self.log_dir, symbol="9984", enabled=True)
        pos = PositionState(side=1, qty=100, avg_price=101.5, entry_mode="maker", entry_ts_ns=123)
        writer.record(_result(with_intents=True), pos, now_ns=2_000_000_000)
        writer.close()

        row = json.loads((Path(self.log_dir) / "decisions.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(row["entry_intent_id"], "entry-1")
        self.assertEqual(row["exit_intent_id"], "exit-1")
        self.assertEqual(row["entry_cancel_signal"], "alpha_flip")
        self.assertEqual(row["entry_cancel_blocked_reason"], "cancel_rate_limit")
        self.assertEqual(row["exit_cancel_signal"], "replace_active_exit_before_force_exit")
        self.assertAlmostEqual(row["position_avg_price"], 101.5)
        self.assertEqual(row["position_entry_mode"], "maker")


if __name__ == "__main__":
    unittest.main()
