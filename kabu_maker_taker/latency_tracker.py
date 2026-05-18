"""Sliding-window latency histogram for P95/P99 monitoring.

LatencyHistogram maintains a fixed-size rolling window of latency samples
and computes approximate percentiles on demand.  Designed for per-request-kind
(submit / cancel / poll) tracking inside RiskManager.

Derived from kabu_micro_edge_c diagnostics latency tracking.
"""
from __future__ import annotations

from collections import deque


class LatencyHistogram:
    """Rolling window of latency samples with percentile queries.

    Uses a sorted insertion approach — O(N) per sample for window sizes
    expected here (<= 200).  For larger N, a heap or t-digest is preferable.
    """

    __slots__ = ("_window_size", "_samples", "_sorted_cache", "_dirty")

    def __init__(self, window_size: int = 100) -> None:
        self._window_size = max(window_size, 1)
        self._samples: deque[float] = deque()
        self._sorted_cache: list[float] = []
        self._dirty: bool = False

    def record(self, latency_ms: float) -> None:
        """Add a latency sample, evicting the oldest when the window is full."""
        if len(self._samples) >= self._window_size:
            self._samples.popleft()
        self._samples.append(max(latency_ms, 0.0))
        self._dirty = True

    def percentile(self, p: float) -> float:
        """Return the p-th percentile (0–100) of the current window.

        Returns 0.0 when the window is empty.
        """
        if not self._samples:
            return 0.0
        if self._dirty:
            self._sorted_cache = sorted(self._samples)
            self._dirty = False
        n = len(self._sorted_cache)
        idx = max(0, min(n - 1, int(p / 100.0 * n)))
        return self._sorted_cache[idx]

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def count(self) -> int:
        return len(self._samples)
