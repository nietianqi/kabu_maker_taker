"""Decision trace writer — per-board JSONL log of strategy decisions.

Appends one compact JSON line per board tick to ``decisions.jsonl`` inside
``log_dir``.  Each record contains:

  ts_ns, ts_jst, market_state, entry_allowed, entry_reason, blocked_reason,
  signal z-scores (obi, lob_ofi, tape, momentum, composite),
  position_qty, position_side.

Set ``enabled=False`` (via ``config.enable_decision_trace = false``) to make
``record()`` a complete no-op with zero overhead on the hot path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import PositionState, StrategyResult

JST = timezone(timedelta(hours=9))


class DecisionTraceWriter:
    """Appends one JSONL line per board tick to ``decisions.jsonl``."""

    def __init__(self, log_dir: str, symbol: str, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._fh = None
        if not enabled:
            return
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        self._fh = (log_path / "decisions.jsonl").open("a", encoding="utf-8")

    def record(
        self,
        result: StrategyResult,
        position: PositionState,
        now_ns: int,
    ) -> None:
        """Append one JSON line to decisions.jsonl.  No-op when disabled."""
        if not self.enabled or self._fh is None:
            return
        ts_jst = ""
        if now_ns > 0:
            ts_jst = datetime.fromtimestamp(now_ns / 1e9, tz=JST).strftime("%Y-%m-%dT%H:%M:%S.%f")
        sig = result.signal
        row = {
            "ts_ns": now_ns,
            "ts_jst": ts_jst,
            "market_state": result.market_state.value,
            "entry_allowed": result.decision.allow,
            "entry_reason": result.decision.reason,
            "entry_mode": result.decision.entry_mode,
            "entry_side": result.decision.side,
            "entry_score": result.decision.entry_score,
            "blocked_reason": result.blocked_reason,
            "entry_intent_id": result.intent.client_order_id if result.intent else "",
            "entry_intent_qty": result.intent.qty if result.intent else 0,
            "entry_intent_price": result.intent.price if result.intent else 0.0,
            "entry_intent_is_market": result.intent.is_market if result.intent else False,
            "entry_intent_reason": result.intent.reason if result.intent else "",
            "exit_intent_id": result.exit_intent.client_order_id if result.exit_intent else "",
            "exit_intent_qty": result.exit_intent.qty if result.exit_intent else 0,
            "exit_intent_price": result.exit_intent.price if result.exit_intent else 0.0,
            "exit_intent_is_market": result.exit_intent.is_market if result.exit_intent else False,
            "exit_intent_reason": result.exit_intent.reason if result.exit_intent else "",
            "entry_cancel_signal": result.entry_cancel_signal,
            "entry_cancel_blocked_reason": result.entry_cancel_blocked_reason,
            "exit_cancel_signal": result.exit_cancel_signal,
            "signal_obi_z": round(sig.obi_z, 3) if sig else 0.0,
            "signal_lob_ofi_z": round(sig.lob_ofi_z, 3) if sig else 0.0,
            "signal_tape_z": round(sig.tape_ofi_z, 3) if sig else 0.0,
            "signal_momentum_z": round(sig.micro_momentum_z, 3) if sig else 0.0,
            "signal_composite": round(sig.composite, 3) if sig else 0.0,
            "position_qty": position.qty,
            "position_side": position.side,
            "position_avg_price": position.avg_price,
            "position_entry_mode": position.entry_mode,
            "position_entry_ts_ns": position.entry_ts_ns,
        }
        self._fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        """Flush and close the log file."""
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
