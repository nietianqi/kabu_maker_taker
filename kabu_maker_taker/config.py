from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SignalWeights:
    obi: float = 0.30
    lob_ofi: float = 0.25
    tape_ofi: float = 0.25
    micro_momentum: float = 0.12
    microprice_tilt: float = 0.08

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "SignalWeights":
        payload = payload or {}
        return cls(
            obi=float(payload.get("obi", 0.30)),
            lob_ofi=float(payload.get("lob_ofi", 0.25)),
            tape_ofi=float(payload.get("tape_ofi", 0.25)),
            micro_momentum=float(payload.get("micro_momentum", 0.12)),
            microprice_tilt=float(payload.get("microprice_tilt", 0.08)),
        )


@dataclass(frozen=True, slots=True)
class SignalConfig:
    book_depth_levels: int = 5
    book_decay: float = 0.75
    tape_window_seconds: int = 15
    zscore_window: int = 120
    mid_std_window: int = 60
    min_best_volume: int = 1
    use_microprice_tilt: bool = True
    kabu_bidask_reversed: bool = False
    auto_fix_negative_spread: bool = True
    weights: SignalWeights = field(default_factory=SignalWeights)
    # Wall detection (T-04)
    wall_ratio_threshold: float = 2.5
    wall_ema_alpha: float = 0.10
    # Price breakout (T-06)
    breakout_lookback_bars: int = 20
    breakout_buffer_ticks: float = 0.0
    # Volatility expansion (T-09)
    vol_expansion_ratio: float = 2.0
    vol_ema_alpha: float = 0.05

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "SignalConfig":
        payload = payload or {}
        return cls(
            book_depth_levels=int(payload.get("book_depth_levels", 5)),
            book_decay=float(payload.get("book_decay", 0.75)),
            tape_window_seconds=int(payload.get("tape_window_seconds", 15)),
            zscore_window=int(payload.get("zscore_window", 120)),
            mid_std_window=int(payload.get("mid_std_window", 60)),
            min_best_volume=int(payload.get("min_best_volume", 1)),
            use_microprice_tilt=bool(payload.get("use_microprice_tilt", True)),
            kabu_bidask_reversed=bool(payload.get("kabu_bidask_reversed", False)),
            auto_fix_negative_spread=bool(payload.get("auto_fix_negative_spread", True)),
            weights=SignalWeights.from_dict(payload.get("weights")),
            wall_ratio_threshold=float(payload.get("wall_ratio_threshold", 2.5)),
            wall_ema_alpha=float(payload.get("wall_ema_alpha", 0.10)),
            breakout_lookback_bars=int(payload.get("breakout_lookback_bars", 20)),
            breakout_buffer_ticks=float(payload.get("breakout_buffer_ticks", 0.0)),
            vol_expansion_ratio=float(payload.get("vol_expansion_ratio", 2.0)),
            vol_ema_alpha=float(payload.get("vol_ema_alpha", 0.05)),
        )


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    trade_qty: int = 100
    allow_short: bool = False
    maker_score_threshold: int = 6
    taker_score_threshold: int = 9
    maker_confirm_ticks: int = 2
    taker_confirm_ticks: int = 1
    book_imbalance_long: float = 0.18
    of_imbalance_long: float = 0.10
    tape_imbalance_long: float = 0.10
    microprice_tilt_long: float = 0.25
    mom_long_threshold: float = 0.0
    strong_signal_multiplier: float = 1.5
    maker_join_best: bool = True
    maker_retreat_ticks: float = 1.0
    # T-04: minimum consumed ratio for wall-break taker trigger
    wall_consumed_ratio_min: float = 0.60
    # Adverse selection: reject signals older than this (0 = disabled)
    signal_expire_ms: int = 500
    # IOC execution: max allowed slippage ticks
    max_slip_ticks: float = 1.0
    # v2: fair price via composite alpha
    fair_value_beta: float = 0.75
    max_fair_shift_ticks: float = 3.0
    # v2: reservation price inventory skew
    inventory_skew_ticks: float = 1.0
    # v2: tick-improvement threshold (composite >= this → improve one tick)
    strong_signal_threshold: float = 0.75
    # v2: cancel signals
    alpha_exit_threshold: float = 0.15
    alpha_entry_threshold: float = 0.40
    max_fair_drift_ticks: float = 1.5
    # v2: dynamic spread thresholds
    vol_high_ticks: float = 2.0
    vol_low_ticks: float = 0.5
    min_half_spread_ticks: float = 1.0
    mid_half_spread_ticks: float = 1.0
    max_half_spread_ticks: float = 2.0
    # v2: reduce order qty by half when vol_expansion=True
    vol_aware_sizing: bool = False
    # v2: cancel signal — spread (ticks) above which spread_expanded is triggered (0 = disabled)
    spread_expanded_ticks: float = 4.0
    # v2: suppress signal-based cancels within N ms of order placement (0 = disabled)
    min_order_age_ms: int = 100
    # v2: microprice streak (+1 direction score when streak >= this; 0 = disabled)
    microprice_streak_min: int = 3

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "StrategyConfig":
        payload = payload or {}
        return cls(
            trade_qty=int(payload.get("trade_qty", 100)),
            allow_short=bool(payload.get("allow_short", False)),
            maker_score_threshold=int(payload.get("maker_score_threshold", 6)),
            taker_score_threshold=int(payload.get("taker_score_threshold", 9)),
            maker_confirm_ticks=int(payload.get("maker_confirm_ticks", 2)),
            taker_confirm_ticks=int(payload.get("taker_confirm_ticks", 1)),
            book_imbalance_long=float(payload.get("book_imbalance_long", 0.18)),
            of_imbalance_long=float(payload.get("of_imbalance_long", 0.10)),
            tape_imbalance_long=float(payload.get("tape_imbalance_long", 0.10)),
            microprice_tilt_long=float(payload.get("microprice_tilt_long", 0.25)),
            mom_long_threshold=float(payload.get("mom_long_threshold", 0.0)),
            strong_signal_multiplier=float(payload.get("strong_signal_multiplier", 1.5)),
            maker_join_best=bool(payload.get("maker_join_best", True)),
            maker_retreat_ticks=float(payload.get("maker_retreat_ticks", 1.0)),
            wall_consumed_ratio_min=float(payload.get("wall_consumed_ratio_min", 0.60)),
            signal_expire_ms=int(payload.get("signal_expire_ms", 500)),
            max_slip_ticks=float(payload.get("max_slip_ticks", 1.0)),
            fair_value_beta=float(payload.get("fair_value_beta", 0.75)),
            max_fair_shift_ticks=float(payload.get("max_fair_shift_ticks", 3.0)),
            inventory_skew_ticks=float(payload.get("inventory_skew_ticks", 1.0)),
            strong_signal_threshold=float(payload.get("strong_signal_threshold", 0.75)),
            alpha_exit_threshold=float(payload.get("alpha_exit_threshold", 0.15)),
            alpha_entry_threshold=float(payload.get("alpha_entry_threshold", 0.40)),
            max_fair_drift_ticks=float(payload.get("max_fair_drift_ticks", 1.5)),
            vol_high_ticks=float(payload.get("vol_high_ticks", 2.0)),
            vol_low_ticks=float(payload.get("vol_low_ticks", 0.5)),
            min_half_spread_ticks=float(payload.get("min_half_spread_ticks", 1.0)),
            mid_half_spread_ticks=float(payload.get("mid_half_spread_ticks", 1.0)),
            max_half_spread_ticks=float(payload.get("max_half_spread_ticks", 2.0)),
            vol_aware_sizing=bool(payload.get("vol_aware_sizing", False)),
            spread_expanded_ticks=float(payload.get("spread_expanded_ticks", 4.0)),
            min_order_age_ms=int(payload.get("min_order_age_ms", 100)),
            microprice_streak_min=int(payload.get("microprice_streak_min", 3)),
        )


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_inventory_qty: int = 300
    max_notional: float = 3_000_000.0
    max_spread_ticks: float = 3.0
    stale_quote_ms: int = 2_000
    enforce_session: bool = False
    open_start_hhmm: str = "09:00"
    open_end_hhmm: str = "15:25"
    # Consecutive loss cooldown (0 = disabled)
    consecutive_loss_limit: int = 0
    cooling_seconds: int = 120
    # Daily loss limit in JPY (0 = disabled; resets at JST midnight)
    daily_loss_limit: float = 0.0
    # TSE split session: second window (empty = single window)
    # e.g. open_start_hhmm_2="12:30", open_end_hhmm_2="15:30" for TSE afternoon
    open_start_hhmm_2: str = ""
    open_end_hhmm_2: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RiskConfig":
        payload = payload or {}
        return cls(
            max_inventory_qty=int(payload.get("max_inventory_qty", 300)),
            max_notional=float(payload.get("max_notional", 3_000_000.0)),
            max_spread_ticks=float(payload.get("max_spread_ticks", 3.0)),
            stale_quote_ms=int(payload.get("stale_quote_ms", 2_000)),
            enforce_session=bool(payload.get("enforce_session", False)),
            open_start_hhmm=str(payload.get("open_start_hhmm", "09:00")),
            open_end_hhmm=str(payload.get("open_end_hhmm", "15:25")),
            consecutive_loss_limit=int(payload.get("consecutive_loss_limit", 0)),
            cooling_seconds=int(payload.get("cooling_seconds", 120)),
            daily_loss_limit=float(payload.get("daily_loss_limit", 0.0)),
            open_start_hhmm_2=str(payload.get("open_start_hhmm_2", "")),
            open_end_hhmm_2=str(payload.get("open_end_hhmm_2", "")),
        )


@dataclass(frozen=True, slots=True)
class MarketStateConfig:
    enabled: bool = False
    abnormal_spread_ticks: float = 6.0
    abnormal_event_rate_hz: float = 160.0
    event_rate_window_seconds: int = 3
    abnormal_price_jump_ticks: float = 4.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "MarketStateConfig":
        payload = payload or {}
        return cls(
            enabled=bool(payload.get("enabled", False)),
            abnormal_spread_ticks=float(payload.get("abnormal_spread_ticks", 6.0)),
            abnormal_event_rate_hz=float(payload.get("abnormal_event_rate_hz", 160.0)),
            event_rate_window_seconds=int(payload.get("event_rate_window_seconds", 3)),
            abnormal_price_jump_ticks=float(payload.get("abnormal_price_jump_ticks", 4.0)),
        )


@dataclass(frozen=True, slots=True)
class LollipopConfig:
    maker_tp_ticks: float = 2.0
    taker_tp_ticks: float = 3.0
    maker_max_hold_seconds: int = 45
    taker_max_hold_seconds: int = 30
    tp_delay_ms: int = 50
    max_retries: int = 5
    stop_loss_ticks: float = 0.0  # 0 = disabled

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "LollipopConfig":
        payload = payload or {}
        return cls(
            maker_tp_ticks=float(payload.get("maker_tp_ticks", 2.0)),
            taker_tp_ticks=float(payload.get("taker_tp_ticks", 3.0)),
            maker_max_hold_seconds=int(payload.get("maker_max_hold_seconds", 45)),
            taker_max_hold_seconds=int(payload.get("taker_max_hold_seconds", 30)),
            tp_delay_ms=int(payload.get("tp_delay_ms", 50)),
            max_retries=int(payload.get("max_retries", 5)),
            stop_loss_ticks=float(payload.get("stop_loss_ticks", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    symbol: str = "9984"
    exchange: int = 27
    tick_size: float = 1.0
    lot_size: int = 100
    dry_run: bool = True
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    lollipop: LollipopConfig = field(default_factory=LollipopConfig)
    market_state: MarketStateConfig = field(default_factory=MarketStateConfig)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AppConfig":
        return cls(
            symbol=str(payload.get("symbol", "9984")),
            exchange=int(payload.get("exchange", 27)),
            tick_size=float(payload.get("tick_size", 1.0)),
            lot_size=int(payload.get("lot_size", 100)),
            dry_run=bool(payload.get("dry_run", True)),
            strategy=StrategyConfig.from_dict(payload.get("strategy")),
            risk=RiskConfig.from_dict(payload.get("risk")),
            signals=SignalConfig.from_dict(payload.get("signals")),
            lollipop=LollipopConfig.from_dict(payload.get("lollipop")),
            market_state=MarketStateConfig.from_dict(payload.get("market_state")),
        )


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    with Path(path).open("r", encoding="utf-8") as handle:
        return AppConfig.from_dict(json.load(handle))
