"""Volatility estimation for adaptive take-profit sizing.

ATREstimator computes a smoothed average of per-board price ranges
(max of |mid move|, spread) using exponential moving average decay.
The result is expressed in ticks and fed into LollipopTPManager to
scale TP distance dynamically.

Derived from kabu_hft_new risk/guard.py VolatilityEstimator.
"""
from __future__ import annotations


class ATREstimator:
    """EMA-based ATR (in ticks) computed from consecutive board snapshots.

    Each board contributes ``max(|mid - prev_mid|, spread) / tick_size``.
    Set ``alpha=0`` to disable (``atr_ticks`` always returns 0.0).
    """

    __slots__ = ("_tick_size", "_alpha", "_atr_ticks", "_prev_mid")

    def __init__(self, tick_size: float, alpha: float = 0.1) -> None:
        self._tick_size = max(tick_size, 1e-9)
        self._alpha = max(0.0, min(1.0, alpha))
        self._atr_ticks: float = 0.0
        self._prev_mid: float = 0.0

    def update(self, mid: float, spread: float) -> float:
        """Update estimator with the latest board mid and spread.

        Returns the updated ATR in ticks.
        """
        if self._alpha <= 0.0:
            return 0.0
        if self._prev_mid > 0.0:
            range_price = max(abs(mid - self._prev_mid), max(spread, 0.0))
            range_ticks = range_price / self._tick_size
            if self._atr_ticks <= 0.0:
                self._atr_ticks = range_ticks
            else:
                self._atr_ticks += self._alpha * (range_ticks - self._atr_ticks)
        self._prev_mid = mid
        return self._atr_ticks

    @property
    def atr_ticks(self) -> float:
        return self._atr_ticks
