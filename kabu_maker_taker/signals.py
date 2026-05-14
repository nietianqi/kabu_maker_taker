from __future__ import annotations

import math
from collections import deque

from .config import SignalConfig
from .models import BoardSnapshot, Level, SignalPacket, TradePrint


class RollingZScore:
    def __init__(self, window: int):
        self.window = max(int(window), 1)
        self.min_samples = min(self.window, 20)
        self.values: deque[float] = deque()
        self.sum_x = 0.0
        self.sum_x2 = 0.0

    def update(self, value: float) -> float:
        self.values.append(value)
        self.sum_x += value
        self.sum_x2 += value * value
        if len(self.values) > self.window:
            removed = self.values.popleft()
            self.sum_x -= removed
            self.sum_x2 -= removed * removed
        return self.score(value)

    def score(self, value: float) -> float:
        count = len(self.values)
        if count < self.min_samples:
            return 0.0
        mean = self.sum_x / count
        variance = max(self.sum_x2 / count - mean * mean, 0.0)
        if variance <= 1e-12:
            return 0.0
        return max(-4.0, min(4.0, (value - mean) / math.sqrt(variance)))


class RollingStdTicks:
    def __init__(self, window: int, tick_size: float):
        self.window = max(int(window), 2)
        self.tick_size = max(tick_size, 1e-9)
        self.values: deque[float] = deque()
        self.sum_x = 0.0
        self.sum_x2 = 0.0

    def update(self, value: float) -> float:
        self.values.append(value)
        self.sum_x += value
        self.sum_x2 += value * value
        if len(self.values) > self.window:
            removed = self.values.popleft()
            self.sum_x -= removed
            self.sum_x2 -= removed * removed
        count = len(self.values)
        if count < 5:
            return 0.0
        mean = self.sum_x / count
        variance = max(self.sum_x2 / count - mean * mean, 0.0)
        return math.sqrt(variance) / self.tick_size


class TapePressure:
    def __init__(self, window_seconds: int):
        self.window_ns = max(window_seconds, 1) * 1_000_000_000
        self.win1s_ns = 1_000_000_000
        self.burst_window_ns = 500_000_000
        self.events: deque[tuple[int, int, int]] = deque()
        self.events_1s: deque[tuple[int, int, int]] = deque()
        self.burst_events: deque[tuple[int, int, int]] = deque()
        self.buy_qty = 0
        self.sell_qty = 0
        self.buy_qty_1s = 0
        self.sell_qty_1s = 0
        self.burst_buy_qty = 0
        self.burst_sell_qty = 0

    def on_trade(self, trade: TradePrint) -> float:
        buy = trade.size if trade.side > 0 else 0
        sell = trade.size if trade.side < 0 else 0
        self.events.append((trade.ts_ns, buy, sell))
        self.events_1s.append((trade.ts_ns, buy, sell))
        self.burst_events.append((trade.ts_ns, buy, sell))
        self.buy_qty += buy
        self.sell_qty += sell
        self.buy_qty_1s += buy
        self.sell_qty_1s += sell
        self.burst_buy_qty += buy
        self.burst_sell_qty += sell
        self._trim(trade.ts_ns)
        return self.current

    def _trim(self, now_ns: int) -> None:
        while self.events and now_ns - self.events[0][0] > self.window_ns:
            _, buy, sell = self.events.popleft()
            self.buy_qty -= buy
            self.sell_qty -= sell
        while self.events_1s and now_ns - self.events_1s[0][0] > self.win1s_ns:
            _, buy, sell = self.events_1s.popleft()
            self.buy_qty_1s -= buy
            self.sell_qty_1s -= sell
        while self.burst_events and now_ns - self.burst_events[0][0] > self.burst_window_ns:
            _, buy, sell = self.burst_events.popleft()
            self.burst_buy_qty -= buy
            self.burst_sell_qty -= sell

    @property
    def current(self) -> float:
        total = self.buy_qty + self.sell_qty
        return 0.0 if total <= 0 else (self.buy_qty - self.sell_qty) / total

    @property
    def ofi_1s(self) -> float:
        total = self.buy_qty_1s + self.sell_qty_1s
        return 0.0 if total <= 0 else (self.buy_qty_1s - self.sell_qty_1s) / total

    @property
    def burst(self) -> float:
        total = self.burst_buy_qty + self.burst_sell_qty
        return 0.0 if total <= 0 else (self.burst_buy_qty - self.burst_sell_qty) / total


class WallDetector:
    """Detects large resting orders (walls) and tracks whether they were consumed by trades."""

    def __init__(self, ema_alpha: float, ratio_threshold: float):
        self.alpha = max(min(float(ema_alpha), 1.0), 1e-6)
        self.ratio = max(float(ratio_threshold), 1.0)
        self._ask_ema: float | None = None
        self._bid_ema: float | None = None
        self._prev_ask1: int = 0
        self._prev_bid1: int = 0

    def update(
        self,
        ask1_size: int,
        bid1_size: int,
        fill_at_ask: int,
        fill_at_bid: int,
    ) -> tuple[bool, bool, bool, bool, float, float]:
        """
        Returns (wall_ask_detected, wall_bid_detected,
                 wall_ask_consumed, wall_bid_consumed,
                 wall_ask_consumed_ratio, wall_bid_consumed_ratio).
        """
        ask1 = max(ask1_size, 0)
        bid1 = max(bid1_size, 0)

        if self._ask_ema is None:
            self._ask_ema = float(ask1)
            self._bid_ema = float(bid1)
            self._prev_ask1 = ask1
            self._prev_bid1 = bid1
            return False, False, False, False, 0.0, 0.0

        # Wall: previous size was >= ratio * EMA before this update
        ask_wall = self._prev_ask1 >= self._ask_ema * self.ratio and self._ask_ema > 0
        bid_wall = self._prev_bid1 >= self._bid_ema * self.ratio and self._bid_ema > 0

        # Consumed: size dropped AND fills-at-that-side explain it
        ask_drop = self._prev_ask1 - ask1
        bid_drop = self._prev_bid1 - bid1
        ask_consumed = ask_wall and ask_drop > 0 and fill_at_ask > 0
        bid_consumed = bid_wall and bid_drop > 0 and fill_at_bid > 0
        ask_consumed_ratio = fill_at_ask / self._prev_ask1 if (ask_consumed and self._prev_ask1 > 0) else 0.0
        bid_consumed_ratio = fill_at_bid / self._prev_bid1 if (bid_consumed and self._prev_bid1 > 0) else 0.0

        # Update EMA and prev sizes
        self._ask_ema = self.alpha * ask1 + (1.0 - self.alpha) * self._ask_ema
        assert self._bid_ema is not None
        self._bid_ema = self.alpha * bid1 + (1.0 - self.alpha) * self._bid_ema
        self._prev_ask1 = ask1
        self._prev_bid1 = bid1

        return ask_wall, bid_wall, ask_consumed, bid_consumed, ask_consumed_ratio, bid_consumed_ratio


class CancelImbalanceTracker:
    """Estimates how much of a bid/ask size drop is due to cancellations vs. fills."""

    def update(
        self,
        bid1_prev: int,
        bid1_curr: int,
        fill_at_bid: int,
        ask1_prev: int,
        ask1_curr: int,
        fill_at_ask: int,
    ) -> tuple[float, float]:
        """Returns (bid_cancel_ratio, ask_cancel_ratio)."""
        bid_cancel_qty = max(0, bid1_prev - bid1_curr - fill_at_bid)
        ask_cancel_qty = max(0, ask1_prev - ask1_curr - fill_at_ask)
        bid_ratio = bid_cancel_qty / bid1_prev if bid1_prev > 0 else 0.0
        ask_ratio = ask_cancel_qty / ask1_prev if ask1_prev > 0 else 0.0
        return min(bid_ratio, 1.0), min(ask_ratio, 1.0)


class BreakoutTracker:
    """Tracks whether mid price has broken above recent high or below recent low."""

    def __init__(self, lookback_bars: int, buffer_ticks: float, tick_size: float):
        self.lookback = max(int(lookback_bars), 2)
        self.buffer = float(buffer_ticks) * max(tick_size, 1e-9)
        self.history: deque[float] = deque(maxlen=self.lookback)

    def update(self, mid: float) -> tuple[bool, bool]:
        """Returns (breakout_long, breakout_short)."""
        if len(self.history) < self.lookback:
            self.history.append(mid)
            return False, False
        recent_high = max(self.history)
        recent_low = min(self.history)
        breakout_long = mid > recent_high + self.buffer
        breakout_short = mid < recent_low - self.buffer
        self.history.append(mid)
        return breakout_long, breakout_short


class VolExpansionDetector:
    """Detects when current volatility is significantly above its recent EMA."""

    def __init__(self, ema_alpha: float, ratio: float):
        self.alpha = max(min(float(ema_alpha), 1.0), 1e-6)
        self.ratio = max(float(ratio), 1.0)
        self._vol_ema: float | None = None

    def update(self, mid_std_ticks: float) -> bool:
        vol = max(mid_std_ticks, 0.0)
        if self._vol_ema is None:
            self._vol_ema = vol
            return False
        expanding = vol >= self._vol_ema * self.ratio and self._vol_ema > 0
        self._vol_ema = self.alpha * vol + (1.0 - self.alpha) * self._vol_ema
        return expanding


class MicropriceStreakTracker:
    """Counts consecutive board ticks where microprice moved up or down."""

    def __init__(self) -> None:
        self._prev: float | None = None
        self._up_streak: int = 0
        self._down_streak: int = 0

    def update(self, microprice: float) -> tuple[int, int]:
        """Returns (up_streak, down_streak). Flat or reversal resets the active streak."""
        if self._prev is None:
            self._prev = microprice
            return 0, 0
        if microprice > self._prev:
            self._up_streak += 1
            self._down_streak = 0
        elif microprice < self._prev:
            self._down_streak += 1
            self._up_streak = 0
        else:
            # Flat tick breaks the "consecutive" streak
            self._up_streak = 0
            self._down_streak = 0
        self._prev = microprice
        return self._up_streak, self._down_streak


class MicrostructureSignalEngine:
    def __init__(self, *, tick_size: float, config: SignalConfig):
        self.tick_size = max(tick_size, 1e-9)
        self.config = config
        self.decay_weights = [config.book_decay**index for index in range(max(config.book_depth_levels, 1))]
        self.tape = TapePressure(config.tape_window_seconds)
        self.mid_std = RollingStdTicks(config.mid_std_window, self.tick_size)
        self.z_obi = RollingZScore(config.zscore_window)
        self.z_lob = RollingZScore(config.zscore_window)
        self.z_tape = RollingZScore(config.zscore_window)
        self.z_mom = RollingZScore(config.zscore_window)
        self.z_tilt = RollingZScore(config.zscore_window)
        self.wall = WallDetector(config.wall_ema_alpha, config.wall_ratio_threshold)
        self.cancel = CancelImbalanceTracker()
        self.breakout = BreakoutTracker(
            config.breakout_lookback_bars,
            config.breakout_buffer_ticks,
            self.tick_size,
        )
        self.vol_exp = VolExpansionDetector(config.vol_ema_alpha, config.vol_expansion_ratio)
        self.streak = MicropriceStreakTracker()
        self.last_board: BoardSnapshot | None = None
        self.micro_ema: float | None = None
        self.last_signal: SignalPacket | None = None
        # Accumulated fills between board snapshots (reset on each on_board call)
        self._acc_fill_at_ask: int = 0
        self._acc_fill_at_bid: int = 0

    def on_trade(self, trade: TradePrint) -> float:
        result = self.tape.on_trade(trade)
        # Accumulate fills by side for wall consumption tracking
        if trade.side > 0:
            self._acc_fill_at_ask += trade.size
        else:
            self._acc_fill_at_bid += trade.size
        return result

    def on_board(self, snapshot: BoardSnapshot) -> SignalPacket:
        obi_raw = self._book_imbalance(snapshot)
        lob_ofi_raw = self._lob_ofi(snapshot)
        tape_ofi_raw = self.tape.current
        tape_ofi_1s = self.tape.ofi_1s
        microprice, micro_momentum_raw, microprice_tilt_raw = self._micro_signals(snapshot)
        mid_std_ticks = self.mid_std.update(snapshot.mid)
        integrated_ofi = 0.5 * lob_ofi_raw + 0.5 * tape_ofi_raw

        obi_z = self.z_obi.update(obi_raw)
        lob_z = self.z_lob.update(lob_ofi_raw)
        tape_z = self.z_tape.update(tape_ofi_raw)
        mom_z = self.z_mom.update(micro_momentum_raw)
        tilt_z = self.z_tilt.update(microprice_tilt_raw)
        weights = self.config.weights
        composite = (
            weights.obi * obi_z
            + weights.lob_ofi * lob_z
            + weights.tape_ofi * tape_z
            + weights.micro_momentum * mom_z
            + weights.microprice_tilt * tilt_z
        )

        # New signals
        ask1 = snapshot.asks[0].size if snapshot.asks else 0
        bid1 = snapshot.bids[0].size if snapshot.bids else 0
        (
            wall_ask_detected, wall_bid_detected,
            wall_ask_consumed, wall_bid_consumed,
            wall_ask_consumed_ratio, wall_bid_consumed_ratio,
        ) = self.wall.update(ask1, bid1, self._acc_fill_at_ask, self._acc_fill_at_bid)

        prev_ask1 = self.last_board.asks[0].size if (self.last_board and self.last_board.asks) else ask1
        prev_bid1 = self.last_board.bids[0].size if (self.last_board and self.last_board.bids) else bid1
        bid_cancel_ratio, ask_cancel_ratio = self.cancel.update(
            prev_bid1, bid1, self._acc_fill_at_bid,
            prev_ask1, ask1, self._acc_fill_at_ask,
        )

        breakout_long, breakout_short = self.breakout.update(snapshot.mid)
        vol_expansion = self.vol_exp.update(mid_std_ticks)
        microprice_up_streak, microprice_down_streak = self.streak.update(microprice)

        # Reset accumulated fills for next interval
        self._acc_fill_at_ask = 0
        self._acc_fill_at_bid = 0

        packet = SignalPacket(
            ts_ns=snapshot.ts_ns,
            obi_raw=obi_raw,
            lob_ofi_raw=lob_ofi_raw,
            tape_ofi_raw=tape_ofi_raw,
            micro_momentum_raw=micro_momentum_raw,
            microprice_tilt_raw=microprice_tilt_raw,
            microprice=microprice,
            mid=snapshot.mid,
            obi_z=obi_z,
            lob_ofi_z=lob_z,
            tape_ofi_z=tape_z,
            micro_momentum_z=mom_z,
            microprice_tilt_z=tilt_z,
            composite=composite,
            mid_std_ticks=mid_std_ticks,
            microprice_gap_ticks=microprice_tilt_raw,
            integrated_ofi=integrated_ofi,
            trade_burst_score=self.tape.burst,
            tape_ofi_1s=tape_ofi_1s,
            wall_ask_detected=wall_ask_detected,
            wall_bid_detected=wall_bid_detected,
            wall_ask_consumed=wall_ask_consumed,
            wall_bid_consumed=wall_bid_consumed,
            wall_ask_consumed_ratio=wall_ask_consumed_ratio,
            wall_bid_consumed_ratio=wall_bid_consumed_ratio,
            bid_cancel_ratio=bid_cancel_ratio,
            ask_cancel_ratio=ask_cancel_ratio,
            breakout_long=breakout_long,
            breakout_short=breakout_short,
            vol_expansion=vol_expansion,
            microprice_up_streak=microprice_up_streak,
            microprice_down_streak=microprice_down_streak,
        )
        self.last_board = snapshot
        self.last_signal = packet
        return packet

    def _book_imbalance(self, snapshot: BoardSnapshot) -> float:
        bid_weight = self._weighted_size(snapshot.bids)
        ask_weight = self._weighted_size(snapshot.asks)
        total = bid_weight + ask_weight
        return 0.0 if total <= 0 else (bid_weight - ask_weight) / total

    def _weighted_size(self, levels: tuple[Level, ...]) -> float:
        total = 0.0
        for index, weight in enumerate(self.decay_weights):
            if index >= len(levels):
                break
            total += weight * max(levels[index].size, 0)
        return total

    def _lob_ofi(self, snapshot: BoardSnapshot) -> float:
        if self.last_board is None:
            return 0.0
        buy_delta = 0.0
        sell_delta = 0.0
        for index in range(len(self.decay_weights)):
            curr_bid = snapshot.bids[index] if index < len(snapshot.bids) else None
            curr_ask = snapshot.asks[index] if index < len(snapshot.asks) else None
            prev_bid = self.last_board.bids[index] if index < len(self.last_board.bids) else None
            prev_ask = self.last_board.asks[index] if index < len(self.last_board.asks) else None

            bid_buy, bid_sell = self._bid_delta(curr_bid, prev_bid)
            ask_buy, ask_sell = self._ask_delta(curr_ask, prev_ask)
            weight = self.decay_weights[index]
            buy_delta += weight * (bid_buy + ask_buy)
            sell_delta += weight * (bid_sell + ask_sell)

        total = buy_delta + sell_delta
        return 0.0 if total <= 0 else (buy_delta - sell_delta) / total

    def _bid_delta(self, curr: Level | None, prev: Level | None) -> tuple[float, float]:
        minimum = self.config.min_best_volume
        if curr and not prev:
            return max(curr.size, minimum), 0.0
        if prev and not curr:
            return 0.0, max(prev.size, minimum)
        if not curr or not prev:
            return 0.0, 0.0
        if curr.price > prev.price:
            return max(curr.size, minimum), 0.0
        if curr.price < prev.price:
            return 0.0, max(prev.size, minimum)
        diff = curr.size - prev.size
        return (float(diff), 0.0) if diff > 0 else (0.0, float(-diff))

    def _ask_delta(self, curr: Level | None, prev: Level | None) -> tuple[float, float]:
        minimum = self.config.min_best_volume
        if curr and not prev:
            return 0.0, max(curr.size, minimum)
        if prev and not curr:
            return max(prev.size, minimum), 0.0
        if not curr or not prev:
            return 0.0, 0.0
        if curr.price < prev.price:
            return max(curr.size, minimum), 0.0
        if curr.price > prev.price:
            return 0.0, max(prev.size, minimum)
        diff = curr.size - prev.size
        return (0.0, float(diff)) if diff > 0 else (float(-diff), 0.0)

    def _micro_signals(self, snapshot: BoardSnapshot) -> tuple[float, float, float]:
        total_size = snapshot.bid_size + snapshot.ask_size
        microprice = (
            snapshot.mid
            if total_size <= 0
            else (snapshot.ask * snapshot.bid_size + snapshot.bid * snapshot.ask_size) / total_size
        )
        if self.micro_ema is None:
            self.micro_ema = microprice
            momentum = 0.0
        else:
            momentum = (microprice - self.micro_ema) / self.tick_size
            self.micro_ema = 0.2 * microprice + 0.8 * self.micro_ema
        tilt = (microprice - snapshot.mid) / self.tick_size if self.config.use_microprice_tilt else 0.0
        return microprice, momentum, tilt
