"""Configuration schema and JSON loader for the kabu maker/taker strategy.

All config dataclasses are frozen and use ``__slots__``.  The canonical source
of truth is a JSON file loaded via ``load_config(path)``.  Every ``from_dict``
classmethod provides defaults for every field so older JSON files remain
compatible when new fields are added.

Config hierarchy::

    AppConfig
    ├── SignalConfig     (signals.py — microstructure signal parameters)
    │   └── SignalWeights
    ├── StrategyConfig   (strategy.py — entry thresholds, sizing, cancel logic)
    ├── RiskConfig       (risk.py — gates, limits, circuit breakers)
    ├── LollipopConfig   (lollipop.py — TP/stop/timeout parameters)
    ├── MarketStateConfig (strategy.py — abnormal-market detection)
    └── KabuConfig       (app.py / kabu_rest — API credentials and polling)
        └── OrderProfile (execution/ — margin/equity/SOR order parameters)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_REGISTER_EXCHANGES: frozenset[int] = frozenset({1, 2, 3, 5, 6, 23, 24})
TSE_FAMILY_EXCHANGES: frozenset[int] = frozenset({1, 9, 27})


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
    # Integrated OFI blend: weight for lob_ofi; (1 - lob_tape_ofi_weight) goes to tape_ofi
    lob_tape_ofi_weight: float = 0.5
    # Microprice EMA smoothing: microprice_ema = alpha*mp + (1-alpha)*prev_ema
    microprice_ema_alpha: float = 0.2

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
            lob_tape_ofi_weight=float(payload.get("lob_tape_ofi_weight", 0.5)),
            microprice_ema_alpha=float(payload.get("microprice_ema_alpha", 0.2)),
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
    # Inventory skew amplifier: |inventory_ratio| >= high_threshold → multiply skew_ticks by high_multiplier
    inventory_high_threshold: float = 0.66
    inventory_high_multiplier: float = 1.5
    # Quote-drift cancel: reprice when ideal quote drifts ≥N ticks from working price (0 = disabled)
    max_quote_drift_ticks: float = 1.0
    # Queue-depth retreat: retreat extra ticks when top-of-book < threshold (0 = disabled)
    queue_min_top_qty: int = 0
    queue_retreat_ticks: float = 1.0
    # Maker edge gate: block passive entry when quote edge is below this many ticks (0 = disabled)
    maker_min_edge_ticks: float = 0.0
    # Working maker order maximum lifetime before cancel signal (0 = disabled)
    max_pending_ms: int = 2500
    # Taker: execution quality gate — min composite score (0-10) required to enter (0 = disabled)
    exec_quality_min_score: int = 0
    # Taker: aggressive mode — reduce required_confirm to 1 when entry_score >= this (0 = disabled)
    aggressive_taker_entry_score: int = 0
    # Taker: adaptive confirmation — require more ticks when all primary checks pass
    use_adaptive_confirm: bool = False
    strong_signal_confirm: int = 2
    # Taker: flow-flip exit — force-exit taker position when tape/lob OFI flips below -threshold (0 = disabled)
    flow_flip_threshold: float = 0.0
    # Taker: dynamic sizing — scale qty up by multiplier when entry_score >= threshold
    scale_qty_by_score: bool = False
    scale_qty_score_threshold: int = 11
    scale_qty_multiplier: float = 1.5
    # Taker: T-09 volatility-expansion alternative entry path (False = disabled)
    use_vol_expansion_taker: bool = False
    # T-09: max spread ticks allowed when vol_expansion fires (0 = inherit RiskConfig.max_spread_ticks)
    vol_expansion_spread_max_ticks: float = 2.0
    # Taker: exec quality — require 1s tape OFI to also confirm direction (0 = disabled)
    tape_ofi_1s_min: float = 0.0
    # Entry selector policy: adaptive, taker_priority, or maker_priority
    entry_selection_policy: str = "adaptive"
    # Adaptive selector: minimum maker quote edge when maker competes with taker
    adaptive_maker_min_edge_ticks: float = 0.25
    # Adaptive selector: urgency score required for taker to beat a valid maker quote
    adaptive_taker_urgency_score: int = 2

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
            inventory_high_threshold=float(payload.get("inventory_high_threshold", 0.66)),
            inventory_high_multiplier=float(payload.get("inventory_high_multiplier", 1.5)),
            max_quote_drift_ticks=float(payload.get("max_quote_drift_ticks", 1.0)),
            queue_min_top_qty=int(payload.get("queue_min_top_qty", 0)),
            queue_retreat_ticks=float(payload.get("queue_retreat_ticks", 1.0)),
            maker_min_edge_ticks=float(payload.get("maker_min_edge_ticks", 0.0)),
            max_pending_ms=int(payload.get("max_pending_ms", 2500)),
            exec_quality_min_score=int(payload.get("exec_quality_min_score", 0)),
            aggressive_taker_entry_score=int(payload.get("aggressive_taker_entry_score", 0)),
            use_adaptive_confirm=bool(payload.get("use_adaptive_confirm", False)),
            strong_signal_confirm=int(payload.get("strong_signal_confirm", 2)),
            flow_flip_threshold=float(payload.get("flow_flip_threshold", 0.0)),
            scale_qty_by_score=bool(payload.get("scale_qty_by_score", False)),
            scale_qty_score_threshold=int(payload.get("scale_qty_score_threshold", 11)),
            scale_qty_multiplier=float(payload.get("scale_qty_multiplier", 1.5)),
            use_vol_expansion_taker=bool(payload.get("use_vol_expansion_taker", False)),
            vol_expansion_spread_max_ticks=float(payload.get("vol_expansion_spread_max_ticks", 2.0)),
            tape_ofi_1s_min=float(payload.get("tape_ofi_1s_min", 0.0)),
            entry_selection_policy=str(payload.get("entry_selection_policy", "adaptive")),
            adaptive_maker_min_edge_ticks=float(payload.get("adaptive_maker_min_edge_ticks", 0.25)),
            adaptive_taker_urgency_score=int(payload.get("adaptive_taker_urgency_score", 2)),
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
    # P1 live-safety gates (0 = disabled)
    max_entry_orders_per_minute: int = 0
    max_cancel_requests_per_minute: int = 0
    api_error_limit: int = 0
    api_cooling_seconds: int = 120
    order_latency_limit_ms: int = 3000
    cancel_latency_limit_ms: int = 3000
    poll_latency_limit_ms: int = 3000
    latency_breach_limit: int = 3
    # Stale-board guard: block entry + cancel working order if inter-board gap > this ms (0 = disabled)
    stale_board_ms: int = 0
    # Cost model for dry-run accounting/backtest estimates
    fee_per_share: float = 0.0
    slippage_ticks_default: float = 0.0
    # Live safety: when true, block automatic exit orders that can realize a loss.
    prevent_loss_exit: bool = False

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
            max_entry_orders_per_minute=int(payload.get("max_entry_orders_per_minute", 0)),
            max_cancel_requests_per_minute=int(payload.get("max_cancel_requests_per_minute", 0)),
            api_error_limit=int(payload.get("api_error_limit", 0)),
            api_cooling_seconds=int(payload.get("api_cooling_seconds", 120)),
            order_latency_limit_ms=int(payload.get("order_latency_limit_ms", 3000)),
            cancel_latency_limit_ms=int(payload.get("cancel_latency_limit_ms", 3000)),
            poll_latency_limit_ms=int(payload.get("poll_latency_limit_ms", 3000)),
            latency_breach_limit=int(payload.get("latency_breach_limit", 3)),
            stale_board_ms=int(payload.get("stale_board_ms", 0)),
            fee_per_share=float(payload.get("fee_per_share", 0.0)),
            slippage_ticks_default=float(payload.get("slippage_ticks_default", 0.0)),
            prevent_loss_exit=bool(payload.get("prevent_loss_exit", False)),
        )


@dataclass(frozen=True, slots=True)
class MarketStateConfig:
    enabled: bool = False
    abnormal_spread_ticks: float = 6.0
    abnormal_event_rate_hz: float = 160.0
    event_rate_window_seconds: int = 3
    abnormal_price_jump_ticks: float = 4.0
    event_burst_min_events: int = 6
    queue_spread_max_ticks: float = 1.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "MarketStateConfig":
        payload = payload or {}
        return cls(
            enabled=bool(payload.get("enabled", False)),
            abnormal_spread_ticks=float(payload.get("abnormal_spread_ticks", 6.0)),
            abnormal_event_rate_hz=float(payload.get("abnormal_event_rate_hz", 160.0)),
            event_rate_window_seconds=int(payload.get("event_rate_window_seconds", 3)),
            abnormal_price_jump_ticks=float(payload.get("abnormal_price_jump_ticks", 4.0)),
            event_burst_min_events=int(payload.get("event_burst_min_events", 6)),
            queue_spread_max_ticks=float(payload.get("queue_spread_max_ticks", 1.0)),
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
class OrderProfile:
    mode: str = "margin"
    allow_short: bool = False
    account_type: int = 4
    cash_buy_fund_type: str = "02"
    cash_buy_deliv_type: int = 2
    cash_sell_fund_type: str = ""
    cash_sell_deliv_type: int = 0
    margin_trade_type: int = 1
    margin_open_fund_type: str = "11"
    margin_open_deliv_type: int = 0
    margin_close_deliv_type: int = 2
    front_order_type_limit: int = 20
    front_order_type_market: int = 10
    front_order_type_ioc_limit: int = 27

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "OrderProfile":
        payload = payload or {}
        return cls(
            mode=str(payload.get("mode", "margin")),
            allow_short=bool(payload.get("allow_short", False)),
            account_type=int(payload.get("account_type", 4)),
            cash_buy_fund_type=str(payload.get("cash_buy_fund_type", "02")),
            cash_buy_deliv_type=int(payload.get("cash_buy_deliv_type", 2)),
            cash_sell_fund_type=str(payload.get("cash_sell_fund_type", "")),
            cash_sell_deliv_type=int(payload.get("cash_sell_deliv_type", 0)),
            margin_trade_type=int(payload.get("margin_trade_type", 1)),
            margin_open_fund_type=str(payload.get("margin_open_fund_type", "11")),
            margin_open_deliv_type=int(payload.get("margin_open_deliv_type", 0)),
            margin_close_deliv_type=int(payload.get("margin_close_deliv_type", 2)),
            front_order_type_limit=int(payload.get("front_order_type_limit", 20)),
            front_order_type_market=int(payload.get("front_order_type_market", 10)),
            front_order_type_ioc_limit=int(payload.get("front_order_type_ioc_limit", 27)),
        )


@dataclass(frozen=True, slots=True)
class KabuConfig:
    base_url: str = "http://localhost:18080"
    api_password: str = ""
    order_rate_per_sec: float = 4.0
    poll_rate_per_sec: float = 4.0
    poll_interval_ms: int = 250
    register_exchange: int = 0
    websocket_url: str = ""
    websocket_reconnect_attempts: int = 3
    websocket_reconnect_forever_when_flat: bool = False
    websocket_preflight_messages: int = 3
    websocket_preflight_timeout_s: float = 15.0
    live_preflight_max_age_minutes: int = 30
    live_arm_path: str = "live_arm.txt"
    startup_open_order_policy: str = "reject"
    order_profile: OrderProfile = field(default_factory=OrderProfile)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "KabuConfig":
        payload = payload or {}
        return cls(
            base_url=str(payload.get("base_url", "http://localhost:18080")),
            api_password=str(payload.get("api_password", "")),
            order_rate_per_sec=float(payload.get("order_rate_per_sec", 4.0)),
            poll_rate_per_sec=float(payload.get("poll_rate_per_sec", 4.0)),
            poll_interval_ms=int(payload.get("poll_interval_ms", 250)),
            register_exchange=int(payload.get("register_exchange", 0)),
            websocket_url=str(payload.get("websocket_url", "")),
            websocket_reconnect_attempts=int(payload.get("websocket_reconnect_attempts", 3)),
            websocket_reconnect_forever_when_flat=bool(payload.get("websocket_reconnect_forever_when_flat", False)),
            websocket_preflight_messages=int(payload.get("websocket_preflight_messages", 3)),
            websocket_preflight_timeout_s=float(payload.get("websocket_preflight_timeout_s", 15.0)),
            live_preflight_max_age_minutes=int(payload.get("live_preflight_max_age_minutes", 30)),
            live_arm_path=str(payload.get("live_arm_path", "live_arm.txt")),
            startup_open_order_policy=str(payload.get("startup_open_order_policy", "reject")),
            order_profile=OrderProfile.from_dict(payload.get("order_profile")),
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
    kabu: KabuConfig = field(default_factory=KabuConfig)
    # Trade journal: write trades.csv + markouts.csv to log_dir
    log_dir: str = "logs"
    enable_journal: bool = False
    # Decision trace: append per-board JSONL to log_dir/decisions.jsonl
    enable_decision_trace: bool = False
    # Kill-switch files: touch to halt new entries (soft) or force-exit + stop (hard)
    kill_switch_path: str = "halt.txt"
    kill_switch_hard_path: str = "halt_hard.txt"

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
            kabu=KabuConfig.from_dict(payload.get("kabu")),
            log_dir=str(payload.get("log_dir", "logs")),
            enable_journal=bool(payload.get("enable_journal", False)),
            enable_decision_trace=bool(payload.get("enable_decision_trace", False)),
            kill_switch_path=str(payload.get("kill_switch_path", "halt.txt")),
            kill_switch_hard_path=str(payload.get("kill_switch_hard_path", "halt_hard.txt")),
        )


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    with Path(path).open("r", encoding="utf-8") as handle:
        return AppConfig.from_dict(json.load(handle))


def effective_register_exchange(trade_exchange: int, register_exchange: int = 0) -> int:
    """Resolve the kabu PUSH registration exchange for a trading exchange.

    kabu uses SOR/TSE+ codes on the order API, but PUSH registration expects
    the venue code. For TSE-family stock routing, register the TSE venue.
    """
    explicit = int(register_exchange)
    if explicit > 0:
        return explicit
    exchange = int(trade_exchange)
    if exchange in {9, 27}:
        return 1
    return exchange


def is_valid_register_exchange(exchange: int) -> bool:
    return int(exchange) in VALID_REGISTER_EXCHANGES


def market_data_exchange_compatible(trade_exchange: int, market_data_exchange: int) -> bool:
    trade = int(trade_exchange)
    market = int(market_data_exchange)
    if trade == market:
        return True
    return trade in TSE_FAMILY_EXCHANGES and market in TSE_FAMILY_EXCHANGES
