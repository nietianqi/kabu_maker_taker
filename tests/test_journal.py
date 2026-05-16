"""Tests for TradeJournal — CSV logging and time-based markout scheduling."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.journal import TradeJournal
from kabu_maker_taker.models import BoardSnapshot, Level, SignalPacket


def _snap(ts_ns: int, bid: float = 100.0, ask: float = 101.0) -> BoardSnapshot:
    return BoardSnapshot(
        symbol="9984", ts_ns=ts_ns,
        bid=bid, ask=ask, bid_size=500, ask_size=200,
        bids=(Level(bid, 500),), asks=(Level(ask, 200),),
    )


def _signal() -> SignalPacket:
    return SignalPacket(
        ts_ns=1_000_000_000,
        obi_raw=0.30, lob_ofi_raw=0.20, tape_ofi_raw=0.15,
        micro_momentum_raw=0.10, microprice_tilt_raw=0.25,
        microprice=100.3, mid=100.5,
        obi_z=0.8, lob_ofi_z=0.5, tape_ofi_z=0.4,
        micro_momentum_z=0.3, microprice_tilt_z=0.6,
        composite=0.55, integrated_ofi=0.20, trade_burst_score=0.10,
    )


class TradeJournalTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_journal(self) -> TradeJournal:
        return TradeJournal(log_dir=self.log_dir, symbol="9984", tick_size=1.0)

    # ------------------------------------------------------------------
    # trades.csv
    # ------------------------------------------------------------------

    def test_on_trade_closed_writes_trades_csv(self) -> None:
        """on_trade_closed writes exactly one row to trades.csv."""
        j = self._make_journal()
        j.on_trade_closed(
            entry_ts_ns=1_000_000_000,
            exit_ts_ns=2_000_000_000,
            side=1, qty=100,
            entry_price=100.0, exit_price=102.0,
            exit_reason="lollipop_tp",
            entry_mode="maker",
            signal=_signal(),
            realized_pnl=180.0,
        )
        j.close()

        trades_path = Path(self.log_dir) / "trades.csv"
        self.assertTrue(trades_path.exists())
        with trades_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "9984")
        self.assertEqual(row["side"], "1")
        self.assertEqual(row["qty"], "100")
        self.assertEqual(row["entry_price"], "100.0")
        self.assertEqual(row["exit_price"], "102.0")
        self.assertAlmostEqual(float(row["realized_pnl"]), 180.0, places=2)
        self.assertEqual(row["exit_reason"], "lollipop_tp")
        self.assertEqual(row["entry_mode"], "maker")
        self.assertEqual(row["hold_ms"], "1000")  # 2s - 1s = 1000ms
        # Signal columns present
        self.assertAlmostEqual(float(row["obi_z"]), 0.8, places=2)
        self.assertAlmostEqual(float(row["composite"]), 0.55, places=2)

    def test_multiple_trades_append(self) -> None:
        """Each on_trade_closed appends a new row; does not overwrite."""
        j = self._make_journal()
        for i in range(3):
            j.on_trade_closed(
                entry_ts_ns=1_000_000_000, exit_ts_ns=2_000_000_000,
                side=1, qty=100,
                entry_price=100.0, exit_price=101.0 + i,
                exit_reason="timeout", entry_mode="taker",
                signal=None, realized_pnl=float(i * 100),
            )
        j.close()
        with (Path(self.log_dir) / "trades.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 3)

    def test_trades_csv_header_written_once(self) -> None:
        """Opening a fresh journal writes the header; reopening appends without a second header."""
        j = self._make_journal()
        j.on_trade_closed(
            entry_ts_ns=1_000_000_000, exit_ts_ns=2_000_000_000,
            side=1, qty=100, entry_price=100.0, exit_price=101.0,
            exit_reason="tp", entry_mode="maker",
            signal=None, realized_pnl=100.0,
        )
        j.close()

        # Reopen and write a second trade — header should not be duplicated
        j2 = TradeJournal(log_dir=self.log_dir, symbol="9984", tick_size=1.0)
        j2.on_trade_closed(
            entry_ts_ns=3_000_000_000, exit_ts_ns=4_000_000_000,
            side=1, qty=100, entry_price=100.0, exit_price=102.0,
            exit_reason="tp", entry_mode="maker",
            signal=None, realized_pnl=200.0,
        )
        j2.close()

        with (Path(self.log_dir) / "trades.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)

    # ------------------------------------------------------------------
    # markouts.csv
    # ------------------------------------------------------------------

    def test_markout_written_when_horizon_reached(self) -> None:
        """on_board() at >= target_ns writes a markout row to markouts.csv."""
        j = self._make_journal()
        exit_ns = 1_000_000_000
        j.on_trade_closed(
            entry_ts_ns=500_000_000, exit_ts_ns=exit_ns,
            side=1, qty=100, entry_price=100.0, exit_price=100.0,
            exit_reason="tp", entry_mode="maker",
            signal=None, realized_pnl=0.0,
        )
        # Feed boards at 500ms, 1s, 3s after exit
        j.on_board(_snap(exit_ns + 500_000_000, bid=103.0, ask=104.0))  # 500ms: mid=103.5
        j.on_board(_snap(exit_ns + 1_000_000_000, bid=105.0, ask=106.0))  # 1s:   mid=105.5
        j.on_board(_snap(exit_ns + 3_000_000_000, bid=107.0, ask=108.0))  # 3s:   mid=107.5
        j.close()

        with (Path(self.log_dir) / "markouts.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 3)
        horizons = [r["markout_horizon"] for r in rows]
        self.assertIn("500ms", horizons)
        self.assertIn("1s", horizons)
        self.assertIn("3s", horizons)

    def test_markout_pnl_ticks_calculation(self) -> None:
        """markout_pnl_ticks = side * (mid - exit_price) / tick_size."""
        j = self._make_journal()
        exit_ns = 1_000_000_000
        j.on_trade_closed(
            entry_ts_ns=500_000_000, exit_ts_ns=exit_ns,
            side=1, qty=100, entry_price=100.0, exit_price=100.0,
            exit_reason="tp", entry_mode="maker",
            signal=None, realized_pnl=0.0,
        )
        # mid = (102+103)/2 = 102.5 → pnl_ticks = 1*(102.5 - 100.0)/1.0 = 2.5
        j.on_board(_snap(exit_ns + 500_000_000, bid=102.0, ask=103.0))
        j.flush()
        j.close()

        with (Path(self.log_dir) / "markouts.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        row_500ms = next(r for r in rows if r["markout_horizon"] == "500ms")
        self.assertAlmostEqual(float(row_500ms["markout_pnl_ticks"]), 2.5, places=2)

    def test_flush_writes_pending_markouts_with_last_mid(self) -> None:
        """flush() outputs remaining pending markouts using last known mid."""
        j = self._make_journal()
        exit_ns = 1_000_000_000
        j.on_trade_closed(
            entry_ts_ns=500_000_000, exit_ts_ns=exit_ns,
            side=1, qty=100, entry_price=100.0, exit_price=100.0,
            exit_reason="tp", entry_mode="maker",
            signal=None, realized_pnl=0.0,
        )
        # Provide a mid via on_board but not at horizon time
        j.on_board(_snap(exit_ns + 100_000_000, bid=104.0, ask=105.0))  # mid=104.5, before any horizon
        j.flush()  # should write all 3 pending markouts with mid=104.5
        j.close()

        with (Path(self.log_dir) / "markouts.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertAlmostEqual(float(row["markout_mid"]), 104.5, places=2)

    def test_signal_none_writes_zeros(self) -> None:
        """signal=None writes 0.0 for all signal columns."""
        j = self._make_journal()
        j.on_trade_closed(
            entry_ts_ns=1_000_000_000, exit_ts_ns=2_000_000_000,
            side=1, qty=100, entry_price=100.0, exit_price=101.0,
            exit_reason="timeout", entry_mode="taker",
            signal=None, realized_pnl=100.0,
        )
        j.close()
        with (Path(self.log_dir) / "trades.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        row = rows[0]
        self.assertEqual(float(row["obi_z"]), 0.0)
        self.assertEqual(float(row["composite"]), 0.0)

    def test_short_side_markout_pnl_sign(self) -> None:
        """side=-1: pnl_ticks = -1*(mid-exit_price)/tick — positive when mid < exit_price."""
        j = self._make_journal()
        exit_ns = 1_000_000_000
        j.on_trade_closed(
            entry_ts_ns=500_000_000, exit_ts_ns=exit_ns,
            side=-1, qty=100, entry_price=105.0, exit_price=100.0,
            exit_reason="lollipop_tp", entry_mode="maker",
            signal=None, realized_pnl=0.0,
        )
        # mid = (97 + 98) / 2 = 97.5 → pnl_ticks = -1 * (97.5 - 100.0) / 1.0 = +2.5
        j.on_board(_snap(exit_ns + 500_000_000, bid=97.0, ask=98.0))
        j.close()

        with (Path(self.log_dir) / "markouts.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        row_500ms = next(r for r in rows if r["markout_horizon"] == "500ms")
        self.assertAlmostEqual(float(row_500ms["markout_pnl_ticks"]), 2.5, places=2)

    def test_methods_are_noop_after_close(self) -> None:
        """on_trade_closed, on_board, and flush are all no-ops after close()."""
        j = self._make_journal()
        j.close()

        # None of these should raise or write a trade row
        j.on_trade_closed(
            entry_ts_ns=1_000_000_000, exit_ts_ns=2_000_000_000,
            side=1, qty=100, entry_price=100.0, exit_price=101.0,
            exit_reason="timeout", entry_mode="taker",
            signal=None, realized_pnl=100.0,
        )
        j.on_board(_snap(2_000_000_000))
        j.flush()

        trades_path = Path(self.log_dir) / "trades.csv"
        with trades_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
