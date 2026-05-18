"""Microstructure event filters for kabu maker/taker strategy.

JumpFilter        — blocks new entry for ``jump_cooldown_ms`` after mid moves
                    more than ``jump_mid_ticks`` between consecutive boards.
                    Mitigates adverse-selection on gap-open / news-driven moves.
                    Derived from kabu_micro_edge_c strategy.hpp jump detection.

FlowFlipDetector  — signals forced exit when tape_ofi / lob_ofi reverse against
                    the current position.  Extends the original inline check in
                    combined.py to cover both Maker *and* Taker positions.
                    Derived from kabu_micro_edge_c execution_controller.hpp.
"""
from __future__ import annotations


class JumpFilter:
    """Block new-entry for ``jump_cooldown_ms`` after a price jump.

    A jump is defined as consecutive-board mid movement exceeding
    ``jump_mid_ticks``.  Set either parameter to 0 to disable.
    """

    __slots__ = ("_jump_mid_ticks", "_cooldown_ns", "_last_jump_ns", "_prev_mid")

    def __init__(self, jump_mid_ticks: float, jump_cooldown_ms: int) -> None:
        self._jump_mid_ticks = jump_mid_ticks
        self._cooldown_ns = jump_cooldown_ms * 1_000_000
        self._last_jump_ns: int = 0
        self._prev_mid: float = 0.0

    def on_board(self, mid: float, tick_size: float, now_ns: int) -> None:
        """Update state; record a jump timestamp when threshold is exceeded."""
        if (
            self._prev_mid > 0.0
            and tick_size > 0.0
            and self._jump_mid_ticks > 0.0
            and self._cooldown_ns > 0
        ):
            mid_move_ticks = abs(mid - self._prev_mid) / tick_size
            if mid_move_ticks > self._jump_mid_ticks:
                self._last_jump_ns = now_ns
        self._prev_mid = mid

    def is_blocked(self, now_ns: int) -> bool:
        """Return True when inside the post-jump cooldown window."""
        if self._cooldown_ns <= 0 or self._last_jump_ns <= 0:
            return False
        return (now_ns - self._last_jump_ns) < self._cooldown_ns


class FlowFlipDetector:
    """Force-exit detector triggered by order-flow reversal against position.

    Covers both Maker and Taker positions.  ``threshold=0`` disables it.
    When ``lob_enabled=True`` (default), *either* tape_ofi *or* lob_ofi
    reversing beyond the threshold triggers a force-exit.
    """

    __slots__ = ("threshold", "lob_enabled")

    def __init__(self, threshold: float, lob_enabled: bool = True) -> None:
        self.threshold = threshold
        self.lob_enabled = lob_enabled

    def should_force_exit(
        self,
        tape_ofi: float,
        lob_ofi: float,
        position_side: int,
    ) -> bool:
        """Return True when a forced exit is warranted.

        Args:
            tape_ofi:      raw tape OFI (positive = buy pressure)
            lob_ofi:       raw LOB OFI (positive = buy pressure)
            position_side: +1 long, -1 short, 0 flat (never triggers)
        """
        if self.threshold <= 0.0 or position_side == 0:
            return False
        if position_side > 0:
            if tape_ofi <= -self.threshold:
                return True
            if self.lob_enabled and lob_ofi <= -self.threshold:
                return True
        else:
            if tape_ofi >= self.threshold:
                return True
            if self.lob_enabled and lob_ofi >= self.threshold:
                return True
        return False
