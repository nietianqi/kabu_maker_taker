"""Trade journal — CSV logging of completed round-trips and time-based markouts.

Writes two files to ``log_dir``:

* ``trades.csv``   — one row per full position close (entry_price, exit_price,
                     PnL, hold_ms, exit_reason, signal z-scores at entry).
* ``markouts.csv`` — one row per scheduled horizon snapshot (500ms / 1s / 3s
                     after exit) showing mid-price and shadow PnL in ticks.

The journal is deliberately I/O-simple: it appends rows with the stdlib ``csv``
module and never buffers large objects.  ``flush()`` should be called on
graceful shutdown to write any pending markout rows using the last observed mid.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import BoardSnapshot, SignalPacket

JST = timezone(timedelta(hours=9))

_TRADE_FIELDS = [
    "ts_jst", "symbol", "side", "qty",
    "entry_price", "exit_price", "realized_pnl", "hold_ms",
    "exit_reason", "entry_mode",
    "obi_z", "lob_ofi_z", "tape_ofi_z", "momentum_z", "composite",
]
_MARKOUT_FIELDS = [
    "entry_ts_jst", "exit_ts_jst", "symbol",
    "markout_horizon", "markout_mid", "markout_pnl_ticks",
]

# (label, nanosecond offset from exit)
_HORIZONS: tuple[tuple[str, int], ...] = (
    ("500ms", 500_000_000),
    ("1s",  1_000_000_000),
    ("3s",  3_000_000_000),
)


@dataclass(slots=True)
class _PendingMarkout:
    target_ns: int
    entry_ts_jst: str
    exit_ts_jst: str
    symbol: str
    side: int
    exit_price: float
    horizon: str
    tick_size: float


def _ns_to_jst(ts_ns: int) -> str:
    """Convert nanosecond timestamp to JST ISO-8601 string (microsecond precision)."""
    if ts_ns <= 0:
        return ""
    return datetime.fromtimestamp(ts_ns / 1e9, tz=JST).strftime("%Y-%m-%dT%H:%M:%S.%f")


class TradeJournal:
    """Logs completed trades to CSV and schedules time-based markouts.

    Typical wiring::

        journal = TradeJournal(log_dir="logs", symbol=config.symbol, tick_size=config.tick_size)

        # In combined.py on full exit:
        journal.on_trade_closed(entry_ts_ns=..., exit_ts_ns=..., ...)

        # In combined.py / app.py on every board tick:
        journal.on_board(snapshot)

        # On graceful shutdown:
        journal.flush()
        journal.close()
    """

    def __init__(self, log_dir: str, symbol: str, tick_size: float) -> None:
        self.symbol = symbol
        self.tick_size = max(tick_size, 1e-9)
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        trades_path = self._log_dir / "trades.csv"
        markouts_path = self._log_dir / "markouts.csv"

        trades_new = not trades_path.exists()
        markouts_new = not markouts_path.exists()

        self._trades_fh = trades_path.open("a", newline="", encoding="utf-8")
        self._trades_writer = csv.DictWriter(self._trades_fh, fieldnames=_TRADE_FIELDS)
        if trades_new:
            self._trades_writer.writeheader()
            self._trades_fh.flush()

        try:
            self._markouts_fh = markouts_path.open("a", newline="", encoding="utf-8")
        except Exception:
            self._trades_fh.close()
            raise
        self._markouts_writer = csv.DictWriter(self._markouts_fh, fieldnames=_MARKOUT_FIELDS)
        if markouts_new:
            self._markouts_writer.writeheader()
            self._markouts_fh.flush()

        self._pending: list[_PendingMarkout] = []
        self._last_mid: float = 0.0
        self._closed = False

    def on_trade_closed(
        self,
        *,
        entry_ts_ns: int,
        exit_ts_ns: int,
        side: int,
        qty: int,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        entry_mode: str,
        signal: SignalPacket | None,
        realized_pnl: float,
    ) -> None:
        """Write one row to trades.csv and schedule 3 markout tasks."""
        if self._closed:
            return
        exit_jst = _ns_to_jst(exit_ts_ns)
        entry_jst = _ns_to_jst(entry_ts_ns)
        hold_ms = max(0, (exit_ts_ns - entry_ts_ns) // 1_000_000) if exit_ts_ns > entry_ts_ns > 0 else 0

        self._trades_writer.writerow({
            "ts_jst": exit_jst,
            "symbol": self.symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "realized_pnl": round(realized_pnl, 4),
            "hold_ms": hold_ms,
            "exit_reason": exit_reason,
            "entry_mode": entry_mode,
            "obi_z": round(signal.obi_z, 4) if signal else 0.0,
            "lob_ofi_z": round(signal.lob_ofi_z, 4) if signal else 0.0,
            "tape_ofi_z": round(signal.tape_ofi_z, 4) if signal else 0.0,
            "momentum_z": round(signal.micro_momentum_z, 4) if signal else 0.0,
            "composite": round(signal.composite, 4) if signal else 0.0,
        })
        self._trades_fh.flush()

        for horizon, offset_ns in _HORIZONS:
            self._pending.append(
                _PendingMarkout(
                    target_ns=exit_ts_ns + offset_ns,
                    entry_ts_jst=entry_jst,
                    exit_ts_jst=exit_jst,
                    symbol=self.symbol,
                    side=side,
                    exit_price=exit_price,
                    horizon=horizon,
                    tick_size=self.tick_size,
                )
            )

    def on_board(self, snapshot: BoardSnapshot) -> None:
        """Write matured markout tasks; update last observed mid."""
        if self._closed:
            return
        mid = snapshot.mid
        if mid > 0:
            self._last_mid = mid
        if not self._pending:
            return
        ts = snapshot.ts_ns
        still: list[_PendingMarkout] = []
        for task in self._pending:
            if ts >= task.target_ns:
                if mid > 0:
                    self._write_markout(task, mid)
                else:
                    still.append(task)  # defer until valid mid
            else:
                still.append(task)
        self._pending = still

    def flush(self) -> None:
        """Write all remaining markouts using the last observed mid, then sync."""
        if self._closed:
            return
        mid = self._last_mid
        if mid > 0:
            for task in self._pending:
                self._write_markout(task, mid)
            self._pending.clear()
        self._trades_fh.flush()
        self._markouts_fh.flush()

    def close(self) -> None:
        """Flush pending markouts and close file handles."""
        if self._closed:
            return
        self.flush()
        self._trades_fh.close()
        self._markouts_fh.close()
        self._closed = True

    def _write_markout(self, task: _PendingMarkout, mid: float) -> None:
        pnl_ticks = task.side * (mid - task.exit_price) / task.tick_size
        self._markouts_writer.writerow({
            "entry_ts_jst": task.entry_ts_jst,
            "exit_ts_jst": task.exit_ts_jst,
            "symbol": task.symbol,
            "markout_horizon": task.horizon,
            "markout_mid": round(mid, 4),
            "markout_pnl_ticks": round(pnl_ticks, 4),
        })
        self._markouts_fh.flush()
