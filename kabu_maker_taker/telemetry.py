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
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .models import PositionState, StrategyResult

JST = timezone(timedelta(hours=9))


class DecisionTraceWriter:
    """Appends one JSONL line per board tick to ``decisions.jsonl``."""

    def __init__(self, log_dir: str, symbol: str, *, enabled: bool = True, strict: bool = False) -> None:
        self.enabled = enabled
        self._fh = None
        if not enabled:
            return
        try:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            self._fh = (log_path / "decisions.jsonl").open("a", encoding="utf-8")
        except OSError:
            if strict:
                raise
            self.enabled = False  # degrade gracefully — tracing is optional

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
            "market_state_reason": result.market_state_reason,
            "market_state_spread_ticks": round(result.market_state_spread_ticks, 3),
            "market_state_event_rate_hz": round(result.market_state_event_rate_hz, 3),
            "market_state_stale_ms": round(result.market_state_stale_ms, 3),
            "market_state_jump_ticks": round(result.market_state_jump_ticks, 3),
            "market_state_trade_lag_ms": round(result.market_state_trade_lag_ms, 3),
            "entry_allowed": result.decision.allow,
            "entry_reason": result.decision.reason,
            "entry_mode": result.decision.entry_mode,
            "entry_side": result.decision.side,
            "entry_score": result.decision.entry_score,
            "blocked_reason": result.blocked_reason,
            "setup_type": result.setup_type,
            "selection_reason": result.selection_reason,
            "maker_candidate_allow": result.maker_candidate_allow,
            "maker_candidate_reason": result.maker_candidate_reason,
            "maker_candidate_score": result.maker_candidate_score,
            "maker_candidate_trigger": result.maker_candidate_trigger,
            "maker_candidate_edge_ticks": round(result.maker_candidate_edge_ticks, 3),
            "taker_candidate_allow": result.taker_candidate_allow,
            "taker_candidate_reason": result.taker_candidate_reason,
            "taker_candidate_score": result.taker_candidate_score,
            "taker_candidate_trigger": result.taker_candidate_trigger,
            "taker_candidate_exec_quality": result.taker_candidate_exec_quality,
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
            "maker_quote_mode": result.maker_quote_mode,
            "maker_fair_price": result.maker_fair_price,
            "maker_reservation_price": result.maker_reservation_price,
            "maker_edge_ticks": round(result.maker_edge_ticks, 3),
            "maker_half_spread_ticks": round(result.maker_half_spread_ticks, 3),
            "maker_queue_threshold": result.maker_queue_threshold,
            "maker_top_queue_qty": result.maker_top_queue_qty,
            "maker_working_age_ms": round(result.maker_working_age_ms, 3),
            "signal_obi_z": round(sig.obi_z, 3) if sig else 0.0,
            "signal_lob_ofi_z": round(sig.lob_ofi_z, 3) if sig else 0.0,
            "signal_tape_z": round(sig.tape_ofi_z, 3) if sig else 0.0,
            "signal_momentum_z": round(sig.micro_momentum_z, 3) if sig else 0.0,
            "signal_composite": round(sig.composite, 3) if sig else 0.0,
            "signal_obi_raw": round(sig.obi_raw, 3) if sig else 0.0,
            "signal_lob_ofi_raw": round(sig.lob_ofi_raw, 3) if sig else 0.0,
            "signal_tape_ofi_raw": round(sig.tape_ofi_raw, 3) if sig else 0.0,
            "signal_tape_ofi_1s": round(sig.tape_ofi_1s, 3) if sig else 0.0,
            "signal_trade_burst_score": round(sig.trade_burst_score, 3) if sig else 0.0,
            "signal_integrated_ofi": round(sig.integrated_ofi, 3) if sig else 0.0,
            "signal_microprice_tilt_raw": round(sig.microprice_tilt_raw, 3) if sig else 0.0,
            "signal_micro_momentum_raw": round(sig.micro_momentum_raw, 3) if sig else 0.0,
            "signal_wall_ask_consumed_ratio": round(sig.wall_ask_consumed_ratio, 3) if sig else 0.0,
            "signal_wall_bid_consumed_ratio": round(sig.wall_bid_consumed_ratio, 3) if sig else 0.0,
            "signal_bid_cancel_ratio": round(sig.bid_cancel_ratio, 3) if sig else 0.0,
            "signal_ask_cancel_ratio": round(sig.ask_cancel_ratio, 3) if sig else 0.0,
            "signal_breakout_long": bool(sig.breakout_long) if sig else False,
            "signal_breakout_short": bool(sig.breakout_short) if sig else False,
            "signal_vol_expansion": bool(sig.vol_expansion) if sig else False,
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


class RuntimeSummaryWriter:
    """Append compact live-runtime health snapshots to a JSONL file."""

    def __init__(
        self,
        *,
        log_dir: str,
        symbol: str,
        path: str = "",
        enabled: bool = True,
        strict: bool = False,
    ) -> None:
        self.enabled = enabled and bool(str(path).strip())
        self.symbol = symbol
        self._fh = None
        if not self.enabled:
            return
        try:
            raw_path = Path(path)
            log_path = raw_path if raw_path.is_absolute() else Path(log_dir) / raw_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = log_path.open("a", encoding="utf-8")
        except OSError:
            if strict:
                raise
            self.enabled = False

    def write(
        self,
        *,
        strategy: Any,
        status: str,
        source: str = "",
        reason: str = "",
        websocket: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
        cleanup: dict[str, Any] | None = None,
        now_ns: int = 0,
    ) -> None:
        if not self.enabled or self._fh is None:
            return
        ts_ns = now_ns if now_ns > 0 else time.time_ns()
        strategy_snapshot = _strategy_status_snapshot(strategy)
        row = {
            "type": "runtime_summary",
            "status": status,
            "source": source,
            "reason": reason,
            "ts_ns": ts_ns,
            "ts_jst": datetime.fromtimestamp(ts_ns / 1e9, tz=JST).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "symbol": self.symbol,
            "websocket": websocket or {},
            "position": strategy_snapshot.get("position", {}),
            "active_orders": strategy_snapshot.get("active_orders", []),
            "metrics": strategy_snapshot.get("metrics", {}),
            "risk": strategy_snapshot.get("risk", {}),
            "auth": _safe_auth_context(auth or {}),
            "consistency": strategy_snapshot.get("consistency", {}),
        }
        if cleanup:
            row["cleanup"] = cleanup
        self._fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None


def _strategy_status_snapshot(strategy: Any) -> dict[str, Any]:
    snapshot_fn = getattr(strategy, "status_snapshot", None)
    if callable(snapshot_fn):
        return snapshot_fn()
    position = getattr(strategy, "position", PositionState())
    orders = getattr(getattr(strategy, "orders", None), "active", lambda: [])()
    metrics = getattr(getattr(strategy, "metrics", None), "to_dict", lambda: {})()
    return {
        "position": {
            "side": getattr(position, "side", 0),
            "qty": getattr(position, "qty", 0),
            "avg_price": getattr(position, "avg_price", 0.0),
        },
        "active_orders": [getattr(order, "to_dict", lambda: {})() for order in orders],
        "metrics": metrics,
        "risk": {},
        "consistency": {"ok": True, "issues": []},
    }


def _safe_auth_context(auth: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    allowed_token_fields = {"shared_token", "token_source", "token_sha256_8", "token_refresh_count"}
    for key, value in auth.items():
        normalized = str(key).lower()
        if "token" in normalized and key not in allowed_token_fields:
            continue
        safe[key] = value
    return safe
