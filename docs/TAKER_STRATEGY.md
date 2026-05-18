# Micro Edge Taker Scalp 策略规格

本文档定义 `kabu_maker_taker` 的 Taker 入场策略规格。目标不是罗列所有主动吃单策略，而是把当前系统可落地的核心组合写清楚：

```text
Tape OFI + Weighted Book Imbalance + Microprice Momentum + Breakout
=> Taker Entry
=> Maker Take Profit
=> Taker Stop / Timeout Exit
```

Taker 的本质是主动支付点差和滑点换取确定成交，因此每一笔交易都必须回答：

```text
未来有利移动 > spread + slip + fee
```

如果这个不等式长期不成立，Taker 策略即使胜率高也会亏损。

> 注意：本文是工程规格和复盘标准，不是投资建议。实盘前必须经过回放、仿真、限额和熔断验证。

## 1. 策略目标

`Micro Edge Taker Scalp` 捕捉秒级到几十秒级的微观方向优势。它只在盘口、成交流和 microprice 同向时主动入场，入场后优先用 maker 限价止盈，若行情失效则用 taker 快速退出。

核心目标：

- 快速拿到仓位：当信号足够强时，不再等待 maker 排队。
- 控制主动成交成本：只在 spread、深度、订单流质量可接受时吃单。
- 缩短持仓暴露：taker 入场后立刻进入 lollipop 止盈/超时/止损状态机。
- 保持可复盘：每次入场都要能解释 entry score、breakout 条件、风控通过/拒绝原因。

当前 v1 默认只做多。做空逻辑通过 `allow_short=true` 后按相同信号镜像扩展，但实盘启用前需要单独验证卖空权限、保证金配置和退出路径。

## 2. 现有系统映射

当前 Python 项目中的职责划分：

| 模块 | 职责 |
| --- | --- |
| `MicrostructureSignalEngine` | 从 board/trade 事件计算盘口、成交流和 microprice 信号。 |
| `TakerStrategy` | 判断是否允许主动吃单入场，并生成 taker `OrderIntent`。 |
| `MakerStrategy` | 当 taker 不满足时，作为较慢、更保守的 maker 入场路径。 |
| `CombinedMakerTakerStrategy` | taker 优先、maker 兜底；统一确认计数、风控、working-entry 和 lollipop 状态。 |
| `RiskManager` | 阻止 spread 过大、行情过期、仓位/名义金额超限、非交易时段等入场。 |
| `LollipopTPManager` | 入场成交后挂 maker TP；超时或止损时输出 taker force-exit 意图。 |

与 `kabu_micro_edge_c` 的对应关系：

- `strategy_policy.hpp` 的 `entry_layer_diagnostics()` 对应当前 Python 的 `entry_layer_diagnostics()`。
- `has_taker_breakout_signal()` 对应当前 Python 的 `_breakout_ready()`。
- C++ 版 `ENTRY_MODE_TAKER` 对应 Python `entry_mode="taker"`。
- C++ 版 limit TP / force exit 对应 Python `LollipopTPManager`。

### 2.1 接口约定

当前文档只描述现有接口，不要求改代码。

`OrderIntent` 是策略层输出的订单意图：

- taker entry 使用 `is_market=true`、`price=0.0`、`strategy="taker"`、`reason="taker_breakout"`。
- maker TP 使用 `is_market=false`、`strategy="lollipop_tp"`、`reason="limit_tp"`。
- taker force exit 使用 `is_market=true`、`strategy="lollipop_tp"`、`reason="timeout_exit"`。

`StrategyResult` 是每个 board tick 的策略结果：

- `intent` 表示新的入场意图，可能是 taker 或 maker。
- `exit_intent` 表示 lollipop 产生的退出意图。
- `blocked_reason` 记录本 tick 未入场的主要原因。
- `signal` 保留当时的微观结构信号，用于复盘。

配置类型职责：

- `StrategyConfig` 管入场阈值、确认次数、打分阈值和 maker/taker 选择。
- `LollipopConfig` 管入场成交后的 TP、超时和止损。
- `RiskConfig` 管账户和行情质量过滤。
- `SignalConfig` 管信号窗口、盘口深度、权重和 kabu 行情归一化。

## 3. 信号定义

### 3.1 Weighted Book Imbalance

衡量多层盘口中买卖力量是否明显失衡。越靠近最优价，权重越高。

```text
weighted_bid = sum(bid_size[i] * decay^i)
weighted_ask = sum(ask_size[i] * decay^i)
obi_raw = (weighted_bid - weighted_ask) / (weighted_bid + weighted_ask)
```

相关参数：

- `book_depth_levels`
- `book_decay`
- `book_imbalance_long`

做多解释：`obi_raw >= book_imbalance_long` 表示买盘相对卖盘更强。  
做空解释：`-obi_raw >= book_imbalance_long`。

### 3.2 LOB OFI

衡量盘口挂单变化中的主动压力。当前实现比较当前 L2 与上一笔 L2：

- bid 价格上移或同价补量，偏多。
- bid 价格下移或撤量，偏空。
- ask 价格下移或撤量，偏多。
- ask 价格上移或补量，偏空。

相关参数：

- `of_imbalance_long`
- `min_best_volume`

### 3.3 Tape OFI

衡量最近成交方向压力：

```text
tape_ofi_raw = (buy_qty - sell_qty) / (buy_qty + sell_qty)
```

当前系统依赖 `TradePrint.side`。如果 kabu 原始行情没有真实逐笔方向，接入层必须用成交价相对 bid/ask 的规则近似推断。

相关参数：

- `tape_window_seconds`
- `tape_imbalance_long`
- `strong_signal_multiplier`

Taker 需要强 tape：

```text
direction * tape_ofi_raw >= tape_imbalance_long * strong_signal_multiplier
```

### 3.4 Microprice Tilt

Microprice 用一档买卖量修正 mid：

```text
mid = (bid + ask) / 2
microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
microprice_tilt_raw = (microprice - mid) / tick_size
```

做多时，`microprice` 高于 `mid` 代表价格有向上压力。  
做空时反向。

相关参数：

- `use_microprice_tilt`
- `microprice_tilt_long`

### 3.5 Micro Momentum

衡量 microprice 相对 EMA 的短期变化：

```text
micro_momentum_raw = (microprice - micro_ema_prev) / tick_size
```

当前 primary checks 要求 micro momentum 同向通过，避免只因为静态盘口厚度而误吃单。

相关参数：

- `mom_long_threshold`

### 3.6 Integrated OFI

综合 LOB OFI 和 Tape OFI：

```text
integrated_ofi = 0.5 * lob_ofi_raw + 0.5 * tape_ofi_raw
```

Taker breakout 要求 `integrated_ofi` 与交易方向一致。

### 3.7 Trade Burst

衡量最近极短窗口的成交流方向。当前实现使用 500ms burst window：

```text
trade_burst_score = burst_buy_sell_imbalance
```

Taker breakout 要求 `trade_burst_score` 与交易方向一致，避免在成交已经冷却后追单。

## 4. 入场打分模型

当前 entry score 最高 13 分，沿用 `kabu_micro_edge_c` 的分层思想：

| 层 | 条件 | 分数 |
| --- | --- | ---: |
| Direction | book imbalance 同向 | +2 |
| Direction | microprice tilt 同向 | +2 |
| Confirmation | LOB OFI 同向 | +2 |
| Confirmation | Tape OFI 同向 | +3 |
| Trigger | micro momentum 同向 | +2 |
| Filter | 对手盘不比本方盘厚 | +1 |
| Filter | integrated OFI 同向 | +1 |

Primary checks 必须先通过：

```text
(book 或 microprice_tilt)
AND (lob_ofi 或 tape)
AND micro_momentum
```

Taker 入场还必须满足：

```text
entry_score >= taker_score_threshold
confirm_count >= taker_confirm_ticks
breakout_ready == true
```

默认参数：

- `taker_score_threshold = 9`
- `taker_confirm_ticks = 1`
- `strong_signal_multiplier = 1.5`

Maker 与 Taker 的关系：

- Taker 是优先路径，信号最强时先尝试主动成交。
- 如果 Taker 不满足 breakout 或分数不够，但 maker 分数足够，则回落到 MakerStrategy。
- 如果已有 `working_entry`，不再产生新的入场意图。
- 如果 lollipop 正在管理退出，阻止新开仓。

## 5. Taker Breakout 条件

Taker 不只看分数，还需要确认“主动吃单后仍有延续性”。当前 `_breakout_ready()` 使用五个条件：

| 条件 | 做多含义 | 做空镜像 |
| --- | --- | --- |
| 对手盘变薄 | ask 前两档 <= bid 前两档的 50% | bid 前两档 <= ask 前两档的 50% |
| strong tape | 主动买成交显著强于主动卖 | 主动卖成交显著强于主动买 |
| microprice tilt | microprice 明显高于 mid | microprice 明显低于 mid |
| integrated OFI | LOB + tape 综合偏多 | LOB + tape 综合偏空 |
| trade burst | 极短窗口成交偏多 | 极短窗口成交偏空 |

做多示意：

```text
same_depth = bid1 + bid2
opposite_depth = ask1 + ask2

opposite_depth <= 0.5 * same_depth
tape_ofi_raw >= tape_imbalance_long * strong_signal_multiplier
microprice_tilt_raw >= microprice_tilt_long
integrated_ofi > 0
trade_burst_score > 0
```

只有五个条件全部通过，才允许 `entry_mode="taker"`。

## 6. 执行模型

当前 Python 代码只输出订单意图，不直接调用 kabu REST：

```text
OrderIntent(
    is_market=true,
    price=0.0,
    strategy="taker",
    reason="taker_breakout"
)
```

含义：

- `is_market=true`：主动成交意图。
- `price=0.0`：当前抽象层不指定限价，后续执行适配器可以映射成市价单。
- `reference_price`：做多时记录当时 ask，做空时记录 bid，用于复盘滑点。

实盘执行适配器可选映射：

| 执行方式 | 状态 |
| --- | --- |
| 市价单 | 当前意图可直接映射，但滑点风险最高。 |
| IOC aggressive limit | 已接入实盘适配器；`is_market` taker intent 会映射为 aggressive IOC limit。 |
| FOK | 后续扩展，只适合必须全成且深度足够的场景。 |
| 立即可成交限价单 | 后续扩展，可用 `best_ask + slip_ticks` 控制买入上限。 |

实盘 taker 默认使用 IOC aggressive limit：

```text
buy_limit = best_ask + max_slip_ticks * tick_size
sell_limit = best_bid - max_slip_ticks * tick_size
time_in_force = IOC
```

默认 `max_slip_ticks=2.0`，即做多挂到 `best_ask + 2 ticks`，做空/逃生挂到 `best_bid - 2 ticks`。日志仍保留 `is_market` intent 语义，但实盘执行层会按 `max_slip_ticks` 生成 IOC 限价单。

## 7. 出场模型

Taker 入场后，由 `LollipopTPManager` 管理退出。

### 7.1 Maker Take Profit

入场成交后调用：

```text
apply_fill(side, qty, price, now_ns, entry_mode="taker")
```

随后 `LollipopTPManager.on_entry_fill()` 进入 `SCHEDULED`：

```text
submit_after_ns = entry_ts_ns + tp_delay_ms
tp_price = avg_price + taker_tp_ticks * tick_size
```

下一次 board tick 满足 delay 后输出：

```text
exit_intent.strategy = "lollipop_tp"
exit_intent.reason = "limit_tp"
exit_intent.is_market = false
```

这就是 “Taker 入场 + Maker 止盈”。

### 7.2 Timeout / Stop Taker Exit

如果 TP 长时间没有成交，或触发止损，lollipop 进入 `TIMEOUT` 并输出 force exit：

```text
exit_intent.strategy = "lollipop_tp"
exit_intent.reason = "timeout_exit"
exit_intent.is_market = true
```

相关参数：

- `taker_tp_ticks`
- `taker_max_hold_seconds`
- `tp_delay_ms`
- `max_retries`
- `stop_loss_ticks`

当前 `stop_loss_ticks=0.0` 表示禁用固定 tick 止损。实盘建议启用非零止损，并记录触发原因。

## 8. 风控过滤

Taker 成本高，必须比 maker 更严格。当前系统已有以下过滤：

| 过滤 | 参数/状态 | 行为 |
| --- | --- | --- |
| spread | `max_spread_ticks` | spread 超过阈值拒绝入场。 |
| stale quote | `stale_quote_ms` | 行情过期拒绝入场。 |
| inventory | `max_inventory_qty` | 超过最大持仓拒绝入场。 |
| notional | `max_notional` | 超过最大名义金额拒绝入场。 |
| session | `enforce_session`、`open_start_hhmm`、`open_end_hhmm` | 非允许时段拒绝入场。 |
| working entry | `entry_order_active` | 已有入场订单时拒绝重复开仓。 |
| lollipop active | `lollipop.is_busy` | 持仓退出管理中拒绝新开仓。 |

仍建议重点复盘或继续强化的过滤：

- order latency / cancel latency 异常熔断。
- 连续亏损冷却。
- 最小盘口深度。
- 最大瞬时涨跌幅，避免追入过度冲击后反转。
- API 拒单率/超时率熔断。

## 9. 参数说明

### 9.1 `StrategyConfig`

| 参数 | 含义 |
| --- | --- |
| `trade_qty` | 单次目标下单数量，最终按 `lot_size` 对齐。 |
| `allow_short` | 默认 false；true 后允许镜像做空逻辑。 |
| `maker_score_threshold` | maker 入场最低分。 |
| `taker_score_threshold` | taker 入场最低分。 |
| `maker_confirm_ticks` | maker 连续确认次数。 |
| `taker_confirm_ticks` | taker 连续确认次数。 |
| `book_imbalance_long` | book imbalance 同向阈值。 |
| `of_imbalance_long` | LOB OFI 同向阈值。 |
| `tape_imbalance_long` | Tape OFI 同向阈值。 |
| `microprice_tilt_long` | microprice tilt 同向阈值。 |
| `mom_long_threshold` | micro momentum 同向阈值。 |
| `strong_signal_multiplier` | strong tape 倍数。 |
| `use_depth_thin_taker` | 默认 true，启用盘口对手盘变薄触发。 |
| `use_wall_break_taker` | 默认 true，启用墙被吃穿触发。 |
| `use_cancel_imbalance_taker` | 默认 true，启用对手盘撤单失衡触发。 |
| `use_price_breakout_taker` | 默认 true，启用短窗口价格突破触发。 |
| `use_vol_expansion_taker` | 默认 true，启用波动率扩张触发。 |
| `opposite_depth_ratio_max` | 默认 0.50，对手盘 best depth / 本方 best depth 的最大比例。 |
| `cancel_imbalance_ratio_min` | 默认 0.40，cancel imbalance 的最低撤单比例。 |
| `cancel_imbalance_extreme_ratio` | 默认 0.80，达到后阻止追单。 |
| `taker_burst_min` | 默认 0.0，depth / wall / cancel 触发需要的最小 burst 分数。 |

### 9.2 `SignalConfig`

| 参数 | 含义 |
| --- | --- |
| `book_depth_levels` | weighted book 使用的档位数。 |
| `book_decay` | 越远档位的衰减权重。 |
| `tape_window_seconds` | Tape OFI 统计窗口。 |
| `zscore_window` | z-score 滚动窗口；当前入场主要使用 raw signal。 |
| `mid_std_window` | mid 波动估计窗口。 |
| `min_best_volume` | LOB OFI 中最小有效量。 |
| `use_microprice_tilt` | 是否启用 microprice tilt。 |
| `kabu_bidask_reversed` | kabu 字段名反转时启用归一化。 |
| `auto_fix_negative_spread` | 自动修复 bid > ask 的异常快照。 |

### 9.3 `RiskConfig`

| 参数 | 含义 |
| --- | --- |
| `max_inventory_qty` | 最大持仓数量。 |
| `max_notional` | 最大名义金额。 |
| `max_spread_ticks` | 最大允许 spread。 |
| `stale_quote_ms` | 行情最大允许延迟。 |
| `enforce_session` | 是否启用交易时段过滤。 |
| `open_start_hhmm` / `open_end_hhmm` | 允许开仓时间窗口。 |

### 9.4 `LollipopConfig`

| 参数 | 含义 |
| --- | --- |
| `maker_tp_ticks` | maker 入场后的 TP 距离。 |
| `taker_tp_ticks` | taker 入场后的 TP 距离。 |
| `maker_max_hold_seconds` | maker 入场最大持仓时间。 |
| `taker_max_hold_seconds` | taker 入场最大持仓时间。 |
| `tp_delay_ms` | 入场成交后延迟多久提交 TP。 |
| `max_retries` | TP 重试次数预算。 |
| `stop_loss_ticks` | 固定 tick 止损；0 表示禁用。 |

## 10. 日志与复盘指标

Taker 不能只看胜率，必须按成本复盘。

每笔入场建议记录：

- `entry_mode`
- `setup_type`（`taker_depth_thin` / `taker_wall_break` / `taker_cancel_imbalance` / `taker_price_breakout` / `taker_vol_expansion`）
- `selection_reason`
- `entry_score`
- `required_confirm`
- `confirm_progress`
- `blocked_reason`
- `reference_price`
- `fill_price`
- `slippage_ticks`
- `spread_ticks`
- `signal.obi_raw`
- `signal.lob_ofi_raw`
- `signal.tape_ofi_raw`
- `signal.tape_ofi_1s`
- `signal.microprice_tilt_raw`
- `signal.micro_momentum_raw`
- `signal.integrated_ofi`
- `signal.trade_burst_score`
- `signal.wall_ask_consumed_ratio` / `signal.wall_bid_consumed_ratio`
- `signal.ask_cancel_ratio` / `signal.bid_cancel_ratio`

核心复盘指标：

| 指标 | 用途 |
| --- | --- |
| fill rate | 主动/被动意图的成交效率。 |
| average slippage | 主动吃单成本。 |
| win rate | 胜率，但不能单独作为优劣判断。 |
| avg win ticks / avg loss ticks | 盈亏结构。 |
| hold time | 是否符合 scalp 预期。 |
| adverse selection | 入场后是否立即反向。 |
| spread cost | 点差成本是否过高。 |
| signal decay time | 信号衰减速度。 |
| order latency / cancel latency | 是否适合继续启用 taker。 |
| PnL per trade / per 100 trades | 稳定性。 |
| entry score buckets | 不同分数段的真实收益。 |
| taker/maker fill count | maker/taker 路径贡献拆分。 |

复盘判定公式：

```text
avg_win_ticks * win_rate
- avg_loss_ticks * (1 - win_rate)
- spread_cost
- fee
- slippage
> 0
```

## 11. Taker 策略大全

### 11.0 分类速查表

| 编号 | 策略名 | 系统实现状态 | 适合场景 |
|-----|-------|------------|---------|
| T-01 | Weighted Book Imbalance | ✅ 已实现（OBI 组件） | 盘口单边失衡 |
| T-02 | Tape OFI Taker | ✅ 已实现 | 主动成交流强劲 |
| T-03 | Microprice Momentum | ✅ 已实现 | 微价格趋势延续 |
| T-04 | Wall Break Taker | ✅ 已实现，默认参与交易 | 大单墙被突破 |
| T-05 | Cancel Imbalance Taker | ✅ 已实现，默认参与交易；极端撤单比例会阻止追单 | 流动性突然消失 |
| T-06 | Breakout Taker | ✅ 已实现，默认参与交易 | 价格突破短期高低点 |
| T-07 | Cross-market Lead Taker | 📋 规格已定义 | 日经/期货领先个股 |
| T-08 | Event-driven Taker | 📋 规格已定义 | 财报、新闻、政策 |
| T-09 | Volatility Expansion Taker | ✅ 已实现，默认参与交易 | 波动率从压缩突然扩张 |
| T-10 | Pullback Re-entry Taker | 📋 规格已定义 | 趋势中第一次回调后追入 |

---

### 11.1 Tape OFI Taker（T-02）

当前已部分实现。继续优化重点：

- 将 `tape_window_seconds` 拆成 500ms / 1s / 3s 多窗口，各自独立打分。
- 记录连续主动买/卖笔数（`consecutive_buy_count`）。
- 区分单笔大单冲击（`single_large_trade`）与连续小单扫盘（`sweep`）：
  - 大单冲击：单笔 `size > N × avg_trade_size` 且打在 ask。
  - 连续扫盘：最近 M 笔成交 90% 以上打在 ask。

```text
大单冲击条件（做多）：
  single_trade_size >= avg_trade_size * large_trade_multiplier
  trade_price == best_ask
  price_after_1s 没有回落超过 0.5 tick

连续扫盘条件（做多）：
  recent_N_trades_on_ask_ratio >= 0.85
  ask 价格上移（不是原地成交）
  买盘继续补量
```

---

### 11.2 Weighted Book Imbalance Taker（T-01）

当前已实现基础 weighted book。继续优化重点：

- 单独追踪 bid1/bid2 快速补量（`bid_replenish_count`）和撤量（`bid_cancel_count`）。
- **假盘口过滤**：若厚侧在 500ms 内大幅撤单（`cancel_ratio > 0.5`），降低 OBI 打分权重：

```text
厚侧撤单检测（做多保护）：
  bid_size_delta < 0  且  abs(delta) > 0.4 × prev_bid_size
  不是因为成交减少（verified by trade_tape）
  → 降低 obi_score 贡献，或标记 lob_quality = “fake”
```

- 增加最小有效深度过滤：

```text
min_combined_depth = ask1 + ask2 >= min_ask_depth  （做多时）
```

---

### 11.3 Microprice Momentum Taker（T-03）

当前已实现 microprice tilt 和 momentum。继续优化重点：

- 统计最近 N 次 microprice 是否连续同向（`microprice_up_streak`）：

```text
up_streak >= 3  （连续 3 次盘口更新 microprice 上移）
AND bid 不断抬高
AND ask 不断被吃
→ microprice momentum 得分 +1 或 +2
```

- 将 micro momentum 从单次 EMA 差值扩展为短期 slope：

```text
momentum_slope = linreg_slope(microprice_history[-5:])
```

---

### 11.4 Wall Break Taker（T-04）规格

#### 核心逻辑

某价位出现显著大于平均水平的挂单量（”墙”），当这堵墙被主动成交快速吃穿时，说明主动方力量极强，可追入。

#### 实现方案

**步骤 1：墙识别**

```python
# 维护每档挂单量的滚动均值
ask1_avg = EMA(ask_size_history, alpha=0.1)
is_wall = ask1_size >= ask1_avg * wall_ratio_threshold  # e.g. 2.5×
```

**步骤 2：墙消耗追踪**

```python
# 区分成交消耗 vs 撤单消失
wall_consumed_by_trade = (ask1_size_prev - ask1_size_curr) > 0 and trade_at_ask
wall_vanished_by_cancel = (ask1_size_prev - ask1_size_curr) > 0 and not trade_at_ask
```

**步骤 3：入场条件（做多，突破卖单墙）**

```text
is_wall == True（识别到墙）
wall_consumed_ratio >= 0.60（墙被吃掉 60% 以上）
wall_consumed_by_trade == True（是成交，不是撤单）
trade_burst_score > strong_burst_threshold（burst 同方向强劲）
price_moved_above_wall == True（价格突破墙位）
ask2_size < ask1_avg_prev（后续卖压不大）
```

**做空（突破买单墙）**：镜像逻辑，bid1 被吃穿。

#### 配置参数

```json
“wall_break”: {
  “wall_ratio_threshold”: 2.5,
  “wall_consumed_ratio_min”: 0.60,
  “strong_burst_threshold”: 0.40,
  “look_back_bars”: 30
}
```

---

### 11.5 Cancel Imbalance Taker（T-05）规格

#### 核心逻辑

不看谁挂单多，看谁**撤单快**。流动性突然消失比静态盘口失衡更有预测力。

#### 入场条件（做空，买盘撤单）

```text
bid_cancel_qty = max(0, bid_size_prev - bid_size_curr - trade_fill_at_bid)
bid_cancel_ratio = bid_cancel_qty / bid_size_prev

bid_cancel_ratio >= 0.40   （买盘撤单超过 40%）
ask_size 稳定或增加         （卖盘未同步减少）
last_price 靠近 bid         （接近撤单价位）
tape_ofi_raw < 0           （成交流同步偏空）
→ 允许做空 taker
```

**入场条件（做多，卖盘撤单）**：镜像逻辑。

#### 注意

- 纯撤单策略容易被”诱空”。必须配合 tape_ofi 同向确认。
- 在撤单率超高（`bid_cancel_ratio > 0.80`）时反而要小心，可能是异常行情清场信号而非方向信号。

---

### 11.6 Breakout Taker（T-06）规格

#### 核心逻辑

价格突破最近 N 秒内的高/低点，配合成交量放大和盘口确认。

#### 突破定义

```python
# 做多：突破短期高点
recent_high = max(mid_price_history[-breakout_lookback_bars:])
breakout_long = current_mid > recent_high + breakout_buffer_ticks * tick_size

# 做空：跌破短期低点
recent_low = min(mid_price_history[-breakout_lookback_bars:])
breakout_short = current_mid < recent_low - breakout_buffer_ticks * tick_size
```

#### 入场条件（做多）

```text
breakout_long == True
成交量放大：recent_trade_volume > volume_avg * volume_expansion_ratio  (e.g. 1.5×)
ask 被连续吃掉：ask_eaten_count_5s >= 3
突破后 3 次盘口更新没有回落到突破位下方
obi_raw > 0
tape_ofi_raw > tape_imbalance_long
```

#### 适合时段

| 时段 | 适合度 |
|-----|-------|
| 开盘后 30 分钟 | 最高 |
| 午后重开 | 高 |
| 财报/新闻发布后 | 高 |
| 午盘低波动 | 低 |

---

### 11.7 Cross-market Lead Taker（T-07）规格

#### 核心逻辑

日经225 micro / TOPIX futures 通常比个股反应快 1–10 秒。当期货明显上涨但目标股票盘口还未完全跟随时，主动入场。

#### 数据要求

| 外部变量 | 用途 |
|---------|------|
| 日经225 micro 期货（N225M） | 日本大盘方向领先指标 |
| TOPIX futures（TOPIXF） | 大盘动量确认 |
| Nikkei ETF（1321.T 等） | 成分股联动确认 |
| USDJPY | 出口股汇率影响（可选） |
| Nasdaq futures（NQc1） | 日本半导体的海外领先信号（开盘前） |

#### 入场逻辑（做多）

```text
# 外部领先
nikkei_futures_return_5s > lead_threshold      （期货 5s 涨幅超过阈值）
nikkei_futures_return_5s > target_stock_return_5s * lag_ratio  （期货涨幅明显大于个股）

# 本地盘口确认（必须有，不能只看期货）
target_stock_obi_raw > 0
target_stock_tape_ofi_raw >= 0
target_stock_ask_not_thick: ask1 + ask2 < ask_depth_limit
spread <= max_spread_ticks * tick_size

# 充分反应检测（避免已经追高）
target_stock_return_5s < nikkei_return_5s * reaction_cap  （个股跟随未超过期货涨幅的 80%）
```

#### 退出

沿用 lollipop TP。跨市场 lead 信号衰减比盘口信号快，建议：
- `taker_tp_ticks` 保持 3.0（期货领先提供动力）
- `taker_max_hold_seconds` 收缩到 20s（lead-lag 窗口短）

#### 配置扩展

```json
“cross_market”: {
  “futures_lead_threshold”: 0.0005,
  “lag_ratio”: 0.5,
  “reaction_cap”: 0.80,
  “max_hold_cross_market_seconds”: 20
}
```

---

### 11.8 Event-driven Taker（T-08）规格

#### 适用事件类型

| 事件 | 交易方向 | 适合性 |
|-----|---------|-------|
| 财报超预期（营收+利润） | 做多 | 高 |
| 财报低于预期 | 做空 | 高 |
| 全年业绩指引上调 | 做多 | 高 |
| 指引下修/毛利率恶化 | 做空 | 高 |
| 大型回购公告 | 做多 | 中高 |
| 并购（被收购方） | 做多 | 高 |
| 政府补贴/政策利好 | 做多（受惠行业） | 中 |
| 出口/制裁限制 | 做空（受限品种） | 中 |
| 汇率急剧变动 | 影响出口股 | 中 |

#### 入场过滤（关键：避免追入已反应的价格）

```text
# 时效性检查
news_age_seconds <= news_stale_threshold  （新闻发布时间在 N 秒内）
price_move_already < price_fully_reacted_threshold  （价格尚未充分反应）

# 方向确认
tape_ofi_raw 方向与事件预期一致
盘口未出现明显大卖墙

# 流动性确认
spread <= max_spread_ticks * tick_size
成交量放大 >= volume_expansion_ratio
```

#### 退出策略

事件驱动行情通常是”一次性冲击”，不宜持仓过长：

- `taker_max_hold_seconds = 15`（财报后流动性快速稳定）
- 若 tape_ofi 在持仓期间快速归零，提前 lollipop timeout exit

---

### 11.9 Volatility Expansion Taker（T-09）规格

#### 核心逻辑

市场从低波动突然进入高波动时，价格容易出现单边冲击。

#### 波动率扩张检测

```python
# 基于 mid_std_ticks（当前信号引擎已计算）
current_vol = signal.mid_std_ticks
vol_avg = EMA(mid_std_ticks_history, alpha=0.05)
vol_expansion = current_vol >= vol_avg * vol_expansion_ratio  # e.g. 2.0×
```

#### 入场条件（做多）

```text
vol_expansion == True
price_direction_up: mid > mid_prev_N + expansion_buffer * tick_size
obi_raw > obi_threshold
tape_ofi_raw > tape_threshold
spread <= max_spread_ticks * tick_size  # 波动扩张时 spread 可能扩大，需要严格过滤
```

#### 禁止入场：压缩后爆发 vs 噪音扩张

必须区分”波动率压缩后突破”（真信号）和”随机噪音波动”（假信号）：

```text
# 压缩后突破特征
vol_compressed_N_bars_before = all(mid_std_ticks[-N:] < low_vol_threshold)  （之前一直低波动）
vol_direction_consistent = True  （扩张方向与盘口/tape 一致）

# 纯噪音特征（拒绝）
spread 同步扩大 > 2× 正常水平
bid/ask 价格跳动无规律
trade_volume 没有同步放大
```

---

### 11.10 Pullback Re-entry Taker（T-10）规格

#### 核心逻辑

直接追突破容易追高。更稳的方式：等第一次小回调后再吃单。

#### 入场条件（做多，趋势中回调）

```text
# 第一阶段：确认上涨趋势
phase1_return >= trend_threshold  （过去 N 秒涨幅足够）

# 第二阶段：等待回调
pullback_depth = max_price_5s - current_mid
pullback_ticks = pullback_depth / tick_size
pullback_ticks in [min_pullback, max_pullback]  （回调幅度适中，不是趋势反转）

# 第三阶段：恢复信号
obi_raw >= obi_threshold  （买盘重新补量）
tape_ofi_raw >= tape_threshold  （主动买成交恢复）
microprice > mid  （微价格重新偏多）
ask_eaten_recently: ask_size 开始减少
```

**做空镜像**：反弹回调后重新做空。

---

## 12. IOC 执行规格

当前 `OrderIntent` 仍以 taker intent 表达入场意图，实盘适配器按以下规则映射执行方式：

### 12.1 标准 Taker → IOC Aggressive Limit

```text
# 做多
buy_limit_price = best_ask + max_slip_ticks * tick_size
time_in_force = IOC

# 做空
sell_limit_price = best_bid - max_slip_ticks * tick_size
time_in_force = IOC
```

优势：
- 限制最大滑点。
- 部分成交比全不成交更好（IOC 允许部分）。

建议 `max_slip_ticks` 配置：

| 场景 | 建议值 |
|-----|-------|
| 正常盘口，spread = 1 tick | 2 ticks（当前默认，用 IOC 控制上限） |
| 波动扩张，spread = 2 ticks | 2 ticks |
| 突破扫盘场景 | 2–3 ticks |
| 不允许滑点 | 0（退化为 Maker） |

### 12.2 Aggressive Taker → 市价单

当 entry_score 极高（满足 `aggressive_taker` 等级）且时效极短时，使用真正的市价单。

```text
is_market = True
price = 0.0
```

**注意**：市价单在 kabu Station API 中的行为需要单独验证——确认是否支持纯市价单，还是需要用前端报价（当前 ask ± N ticks）模拟。

### 12.3 分段 Taker（可选）

目标成交量较大时，不一次性吃完，分 2–3 笔：

```text
total_qty = 300
order_1 = 100  （先试探）
order_2 = 100  （确认信号延续后）
order_3 = 100  （仍有 breakout 条件时）
```

限制：每笔之间间隔 ≥ `min_order_interval_ms`，且下一笔发送前 re-check breakout 条件。

---

## 13. Adverse Selection 防护

Adverse selection 是 Taker 策略最大的隐性成本：入场后价格立刻反向。

### 13.1 识别信号

```text
adverse_selection_indicator = True if:
  入场后 500ms 内价格向不利方向移动 > 0.5 tick
  AND tape_ofi 在入场后快速反向
  AND obi_raw 在入场后迅速降低
```

### 13.2 实时防护措施

**防护 1：信号时效过滤**

```text
signal_age_ns = now_ns - signal.ts_ns
if signal_age_ns > signal_expire_ns:
    拒绝入场  # 信号已过期，追单无意义
```

**防护 2：入场后快速评估**

持仓后每次 `on_board()` 检查：

```text
if (
    tape_ofi_raw < -tape_flip_threshold      # 成交流反向
    OR obi_raw < obi_min_threshold           # 盘口失衡消失
    OR microprice < mid - tilt_min           # microprice 反向
):
    → lollipop.reschedule() 降低 TP 目标
    → 或 lollipop timeout 提前触发
```

**防护 3：成交量确认**

```text
# 只有在成交量真实放大的情况下才允许追单
if recent_volume < volume_confirmation_threshold * avg_volume:
    降低 taker_score，退回 maker 或不开仓
```

**防护 4：连续亏损熔断（推荐后续加入 RiskManager）**

```text
consecutive_losses >= 3:
    cooling_seconds = 120  # 连续亏损后冷静期
    block_taker_entry = True
```

### 13.3 事后复盘标准

| 指标 | 正常范围 | 异常阈值 |
|-----|---------|---------|
| adverse_selection_rate（入场后 500ms 反向比例） | < 35% | > 50% 需检查信号 |
| avg_markout_100ms | > 0 ticks | < -0.3 ticks 需检查 |
| avg_markout_500ms | > 0.5 ticks | < 0 ticks 需检查 |
| hold_time_avg | 5–20 秒 | > 40 秒说明 TP 太远 |
| force_exit_ratio | < 20% | > 40% 说明持仓时间或 TP 设置不合理 |

---

## 14. 参数调优速查

### 14.1 提高入场频率（降低精度）

```json
{
  “taker_score_threshold”: 8,
  “taker_confirm_ticks”: 1,
  “strong_signal_multiplier”: 1.2,
  “book_imbalance_long”: 0.15,
  “tape_imbalance_long”: 0.08
}
```

**风险**：adverse selection 上升，需收紧止损。

### 14.2 提高入场精度（降低频率）

```json
{
  “taker_score_threshold”: 10,
  “taker_confirm_ticks”: 2,
  “strong_signal_multiplier”: 2.0,
  “tape_imbalance_long”: 0.15,
  “book_imbalance_long”: 0.22
}
```

**风险**：错过短暂窗口，特别是 wall break 和 event-driven 场景。

### 14.3 TP 距离参考

| 场景 | 建议 `taker_tp_ticks` |
|-----|---------------------|
| 一档 spread = 1 tick，低波动 | 1.5–2.0 |
| 一档 spread = 1 tick，正常波动 | 2.0–3.0 |
| 一档 spread = 2 ticks | 3.0–4.0 |
| 跨市场 lead 场景 | 2.5–3.5（快速获利） |
| 事件驱动场景 | 3.0–5.0（冲击后延续） |

**经验法则**：`taker_tp_ticks ≥ spread_ticks × 1.5 + fee_ticks`

### 14.4 持仓时间参考

| 信号类型 | 建议 `taker_max_hold_seconds` |
|---------|------------------------------|
| 盘口失衡 scalp | 15–25 |
| Wall break | 20–30 |
| Cross-market lead | 15–20 |
| Event-driven | 10–20 |
| 开盘突破 | 30–60 |

---

## 15. 后续扩展路线

建议按以下顺序推进：

1. ✅ **执行适配器**：taker `OrderIntent` 已映射为 IOC aggressive limit（见第 12 节规格）。
2. ✅ **滑点字段**：记录 `reference_price`、实际成交价、`slippage_ticks`。
3. ✅ **多窗口 tape**：已接入 `tape_ofi_1s`、burst 和 sustained flow 字段。
4. **lollipop 回报**：TP 活跃单撤单后自动 reschedule 或 force exit。
5. ✅ **Wall Break（T-04）**：已进入交易触发，`setup_type=taker_wall_break`。
6. ✅ **Cancel Imbalance（T-05）**：已进入交易触发，`setup_type=taker_cancel_imbalance`，极端比例默认阻断追单。
7. **Cross-market Lead（T-07）**：先做开盘和权重股回放，验证 lead-lag 延迟后再实盘。
8. **连续亏损熔断**：在 `RiskManager` 中加入 `consecutive_loss_cooling`。
9. **Adverse selection markout**：在 journal 中按入场后 100ms / 500ms / 1s / 3s 记录 PnL。
10. **Event-driven / Volatility（T-08、T-09）**：T-09 已基于 `vol_expansion` 默认启用；T-08 仍需新闻/事件源后再开启。

当前 v1 的可实盘最小闭环是：

```text
强盘口 + 强成交流 + microprice 同向
=> taker entry intent
=> entry fill
=> lollipop maker TP
=> timeout / stop taker exit
=> trade journal + markout review
```
