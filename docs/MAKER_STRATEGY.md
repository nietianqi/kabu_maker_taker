# Micro Maker Hybrid 策略规格

本文档定义 `kabu_maker_taker` 的 Maker 策略规格。这里的 Maker 指挂限价单提供流动性，核心目标是：

```text
quote quality 足够好
+ adverse selection 可控
+ 库存风险可承受
=> 才允许被动挂单
```

核心闭环：

```text
market quality filter
=> fair_price + reservation_price + inventory_skew
=> passive quote / queue defense / close_only
=> maker entry
=> maker take profit
=> taker escape / emergency exit
=> journal + markout review
```

> 注意：本文是工程规格和复盘标准，不是投资建议。Maker 实盘前必须验证队列成交、撤单延迟、拒单率、库存和异常行情退出。

---

## 1. 策略目标

`Micro Maker Hybrid` 只在盘口质量正常、短期 alpha 不反向、库存限制允许时挂被动限价单。

Maker 策略赚的钱主要来自：

```text
1. 买在 bid，卖在 ask，赚点差（spread capture）
2. 利用队列位置，比别人更早成交（queue priority）
3. 利用库存控制，低位多买，高位多卖（inventory skew benefit）
4. 利用盘口失衡，只在有利方向挂单（alpha-filtered quoting）
```

Maker 最大的风险是被"毒性订单"选中（adverse selection）：

```text
你挂单成交，说明别人主动打你。
如果对方是聪明资金，成交后价格立刻朝不利方向移动。
```

所以 Maker 策略的核心不是"挂得越多越好"，而是：

> **只在有优势的位置挂单，被打了以后不能马上反向亏损。**

核心目标：

- 赚取 spread：尽量在 bid 买入、在 ask 或更优价卖出。
- 避免毒性成交：不在强 taker flow 反向冲击时被动接单。
- 管理队列位置：一档队列太薄或行情异常时退后一档或停止挂单。
- 控制库存：根据库存偏斜调整 reservation price，库存过大时减少继续加仓。
- 与 Taker 配合：Maker 入场后优先 Maker TP，信号恶化时 Taker 逃生。
- 保持可复盘：每次挂单、撤单、成交都能解释信号、价格、队列、风控原因。

当前 v1 默认只做多。做空是 `allow_short=true` 后的镜像扩展，实盘前必须单独验证卖空权限、保证金、返还路径和 kabu 下单参数。

---

## 2. 系统映射

### 2.1 `kabu_hft_new` 参考实现

`kabu_hft_new` 是更完整的被动 maker 系统，当前 Python 项目以它为升级参考。

| 模块 | 职责 |
| --- | --- |
| `HFTStrategy` | 根据 signal、market state、risk 和 inventory 生成入场/退出决策。 |
| `PriceSelector` | 选择 passive_fair_value、queue_defense 或 close_only 报价模式。 |
| `MarketStateDetector` | 将市场分为 `NORMAL`、`QUEUE`、`ABNORMAL`。 |
| `RiskGuard` | 过滤 stale quote、spread、session、daily loss、consecutive loss、cooling。 |
| `ExecutionController` | 管理 working order 生命周期：min_lifetime、max_pending、requote budget、partial fill。 |
| `TradeJournal` | 记录 round trip 和 markout，用于复盘 maker 是否被 adverse selection。 |

关键设计原则：

- Strategy 只判断方向、强度和报价模式。
- Execution 负责订单价格、撤单、重挂、成交和 OMS 状态同步。
- MarketState 在每个有效 board 上更新；异常检测不能被节流。

### 2.2 当前 Python 项目映射

| 模块 | 当前职责 |
| --- | --- |
| `MicrostructureSignalEngine` | 计算 weighted book、LOB OFI、Tape OFI、microprice、momentum、burst。 |
| `MakerStrategy` | 使用 13 分 entry score 判断是否允许被动挂单，生成 maker `OrderIntent`。 |
| `TakerStrategy` | breakout 强度更高时抢先走 taker。 |
| `CombinedMakerTakerStrategy` | taker 优先、maker 兜底；统一确认计数、风控、working-entry 和 lollipop 状态。 |
| `RiskManager` | 行情质量和仓位过滤；v2 目标加入 fair_price、reservation_price、dynamic spread。 |
| `LollipopTPManager` | 入场成交后挂 maker TP；超时或止损时输出 taker force-exit。 |

### 2.3 接口约定

`OrderIntent` 策略层输出：

- maker entry：`is_market=false`、`strategy="maker"`、`reason="maker_passive_edge"`。
- maker TP：`is_market=false`、`strategy="lollipop_tp"`、`reason="limit_tp"`。
- taker escape：`is_market=true`、`strategy="lollipop_tp"`、`reason="timeout_exit"`。

`StrategyResult` 每个 board tick 的结果：

- `intent`：新的入场意图（maker 或 taker）。
- `exit_intent`：lollipop 产生的退出意图。
- `blocked_reason`：本 tick 未挂单的主要原因。
- `signal`：当时的微观结构信号，用于复盘。

---

## 3. 信号定义

Maker 使用与 Taker 共用的微观结构信号层，但解释不同：Taker 追求马上成交后的延续，Maker 追求"挂单被打后不会立刻亏"。

### 3.1 Weighted Book Imbalance (OBI)

```text
weighted_bid = sum(bid_size[i] * decay^i)   decay=0.70~0.75，levels=5
weighted_ask = sum(ask_size[i] * decay^i)
obi_raw = (weighted_bid - weighted_ask) / (weighted_bid + weighted_ask)
```

Maker 用法：

- 做多挂 bid 时，希望 `obi_raw >= book_imbalance_long`，说明 bid 被打后不容易继续下跌。
- 如果 `obi_raw` 快速反向，应撤掉同方向挂单。
- OBI 也可以检测"假盘口"：若厚侧在 500ms 内大幅撤单（`cancel_ratio > 0.50`），降低 OBI 置信度。

### 3.2 LOB OFI

LOB OFI 衡量盘口挂单变化中的主动方向压力。

Maker 用法：

- bid 侧挂单前，要求 LOB OFI 不明显偏空。
- 若 working order 期间 LOB OFI 反向，考虑撤单或退后一档。

### 3.3 Tape OFI（多窗口）

```text
tape_ofi_raw   = (buy_qty - sell_qty) / total_qty   [15s 窗口]
tape_ofi_1s    = 1 秒内的成交方向压力                 [T-02 增强]
trade_burst_score = 500ms 内的成交方向压力
```

Maker 用法：

- Maker 最怕被强主动流打穿。
- 做多挂 bid 时，不能在 `tape_ofi_raw` 强烈偏空时接单：

```text
tape_ofi_not_against_long = tape_ofi_raw > -tape_imbalance_long
```

- 若 working order 期间 tape_ofi 方向反转，触发撤单或逃生。

### 3.4 Microprice Tilt

```text
mid = (bid + ask) / 2
microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
microprice_tilt_raw = (microprice - mid) / tick_size
```

Maker 用法：

- `microprice > mid`：偏多，join bid 更积极，不轻易挂 ask。
- `microprice < mid`：偏空，join ask 更积极，不轻易挂 bid。
- 若 microprice 穿回 mid 另一侧，说明 working order 的 quote quality 下降。

### 3.5 Micro Momentum

```text
micro_momentum_raw = (microprice - micro_ema_prev) / tick_size
```

Maker 用法：

- 顺势 Maker：只在 momentum 同向时挂单，不在动量为 0 或反向时进。
- 均值回归 Maker：等待 momentum 冲击后衰竭再挂单。

### 3.6 Composite Alpha（Maker v2 参考）

`kabu_hft_new` 使用在线 z-score 后的加权组合：

```text
composite =
    w_lob * lob_ofi_z
  + w_obi * obi_z
  + w_tape * tape_ofi_z
  + w_mom * micro_momentum_z
  + w_tilt * microprice_tilt_z
  [+ w_whale * whale_z]   # 大单压力，默认 weight=0

默认权重：LOB-OFI(0.30) > OBI(0.25) > Tape-OFI(0.20) > Momentum(0.15) > Tilt(0.10)
entry_threshold = 0.40   strong_threshold = 0.75
```

Maker 用法：

- alpha > 0：偏多方向。
- alpha >= strong_threshold (0.75)：考虑 tick improvement（提高成交率）。
- alpha 反向 >= exit_threshold (0.15)：撤销 working order。

当前 `kabu_maker_taker` 的 maker 入场使用 13 分 raw score；v2 目标是引入 composite z-score 用于 fair-value 动态报价。

### 3.7 Market State（`kabu_hft_new` 参考）

| 状态 | 含义 | Maker 行为 |
| --- | --- | --- |
| `NORMAL` | 正常盘口和事件频率 | 允许 passive fair-value quote。 |
| `QUEUE` | spread ≤ 1 tick 的一档队列场景 | 允许 queue defense；队列太薄时退后一档。 |
| `ABNORMAL` | stale/spread blowout/event burst/price jump/special quote | 禁止新开仓，撤所有 working order，只允许风险退出。 |

检测逻辑：

| 条件 | 状态 | 理由 |
| --- | --- | --- |
| quote 无效 | ABNORMAL | invalid_quote |
| 行情超 2s 未更新 | ABNORMAL | stale_quote |
| TSE 特殊行情标志（0102/0103/0107） | ABNORMAL | special_quote_sign |
| spread >= 6 ticks | ABNORMAL | spread_blowout |
| event_rate >= 160 Hz（3s 内 >6 事件） | ABNORMAL | event_burst |
| mid 跳动 >= 4 ticks | ABNORMAL | price_jump |
| spread <= 1 tick | QUEUE | one_tick_queue |
| 其他 | NORMAL | normal_flow |

当前 Python 版本已经接入 `MarketStateDetector`；`market_state.enabled=true` 时，ABNORMAL 会阻止 maker 开仓，QUEUE 可用于报价模式和复盘分桶。

---

## 4. 报价模型

### 4.1 Fair Price

`kabu_hft_new` 的 fair price：

```text
fair_shift_ticks = clamp(fair_value_beta * alpha, -max_fair_shift_ticks, max_fair_shift_ticks)
fair_price = mid + fair_shift_ticks * tick_size

默认参数：
fair_value_beta = 0.75        # alpha=1.0 → fair 向 mid 偏移 0.75 tick
max_fair_shift_ticks = 3.0    # 最大偏移上限
```

解释：alpha 偏多时，fair price 高于 mid；alpha 偏空时，fair price 低于 mid。

### 4.2 Reservation Price（库存偏斜）

库存偏斜是 Maker 策略最重要的部分之一。

```text
inventory_ratio = signed_inventory / max_inventory_qty   # 范围 [-1, 1]
skew_multiplier = 1.5 if abs(inventory_ratio) >= 0.66 else 1.0
skew_ticks = inventory_skew_ticks * skew_multiplier * inventory_ratio
reservation_price = fair_price - skew_ticks * tick_size

默认参数：
inventory_skew_ticks = 1.0
```

解释：

- 多头库存 50%：reservation = fair - 0.5 tick（不太想继续买，想卖出）
- 多头库存 70%：reservation = fair - 1.05 tick（1.5x 加速；想尽快卖出）
- 空头库存 100%：reservation = fair + 1.5 tick（想买回覆盖）

**关键：** Reservation price 决定 bid/ask 是否还有足够 edge。如果：

```text
做多：reservation_price <= best_bid - tick_size  → 退后一档
做空：reservation_price >= best_ask + tick_size  → 退后一档
```

### 4.3 Dynamic Spread

`kabu_hft_new` 使用 ATR 估算波动、结合 mid_std_ticks 动态调整 half spread：

```text
if mid_std_ticks > volatility_high_threshold:
    half_spread = max_spread_ticks       # 高波动挂远
elif mid_std_ticks < volatility_low_threshold:
    half_spread = min_spread_ticks       # 低波动挂近
else:
    half_spread = mid_spread_ticks

bid_price = reservation_price - half_spread * tick_size
ask_price = reservation_price + half_spread * tick_size
```

场景适配（日本市场参考）：

| 场景 | half spread |
| --- | --- |
| 低波动横盘 | 1 tick |
| 正常盘口 | 1~2 ticks |
| 波动扩张（`vol_expansion=True`） | 2~3 ticks |
| 财报/新闻后 | 暂停或 3~5 ticks |

### 4.4 Join / Improve / Retreat

| 动作 | 含义 | 触发条件 |
| --- | --- | --- |
| join best | 挂在 best_bid / best_ask | 默认模式（`maker_join_best=true`） |
| improve one tick | bid + 1 tick 或 ask - 1 tick 成为新的最优价 | alpha >= strong_threshold(0.75) 且 spread >= 2 ticks |
| retreat one tick | bid - 1 tick 或 ask + 1 tick 退后一档 | 队列太薄、库存偏斜不利、fair price 远离当前价 |

当前 Python v1：

- `maker_join_best=true`：做多挂 `snapshot.bid`，做空挂 `snapshot.ask`。
- `maker_join_best=false`：按 `maker_retreat_ticks` 退后。

---

## 5. 入场模型

### 5.1 当前 13 分 Entry Score

当前 Python `MakerStrategy` 共用 13 分评分体系：

| 层 | 条件 | 分数 |
| --- | --- | ---: |
| Direction | book imbalance 同向 | +2 |
| Direction | microprice tilt 同向 | +2 |
| Confirmation | LOB OFI 同向 | +2 |
| Confirmation | Tape OFI 同向 | +3 |
| Trigger | micro momentum 同向 | +2 |
| Filter | 对手盘不比本方盘厚 | +1 |
| Filter | integrated OFI 同向 | +1 |

Primary checks 必须通过：

```text
(book 或 microprice_tilt)
AND (lob_ofi 或 tape)
AND micro_momentum
```

Maker 入场条件：

```text
entry_score >= maker_score_threshold (默认 6)
confirm_count >= maker_confirm_ticks (默认 2)
risk.can_enter == true
entry_order_active == false
lollipop.is_busy == false
```

Maker `OrderIntent`：

```text
is_market = false
price = best_bid（做多）或 best_ask（做空）
strategy = "maker"
reason = "maker_passive_edge"
reference_price = best_ask（做多）或 best_bid（做空）
max_slip_ticks = 0.0（maker 不允许滑点）
```

### 5.2 做多 Maker 完整条件

```text
市场质量：
  spread <= max_spread_ticks
  not stale_quote
  market_state != ABNORMAL

信号层：
  entry_score >= maker_score_threshold
  confirm_count >= maker_confirm_ticks
  book_imbalance > 0 (obi_raw >= book_imbalance_long)
  microprice >= mid (microprice_tilt_raw >= microprice_tilt_long)
  tape_ofi 不强烈偏空（不触发强 taker sell flow）
  micro_momentum >= 0

库存层：
  position.qty < max_inventory_qty
  allow_short 或 position.side 不为空头
```

### 5.3 报价方式选择

```text
if alpha >= strong_threshold AND spread >= 2 ticks:
    price = best_bid + 1 tick   # Tick Improvement，成为新 best_bid
elif reservation_price <= best_bid - tick_size:
    price = best_bid - 1 tick   # Step-back Maker，退后安全
else:
    price = best_bid             # Join Best（默认）
```

---

## 6. 撤单与重挂

Maker 的核心不是挂单，而是知道什么时候撤。

### 6.1 撤单触发条件

| 原因 | 含义 | 参数 |
| --- | --- | --- |
| `stale_quote` | 行情时间戳超过 stale_quote_ms | `stale_quote_ms=2000` |
| `spread_expanded` | spread 超出允许范围 | `max_spread_ticks=3.0` |
| `alpha_decay` | 信号强度降低到阈值以下 | `signal_strength < entry_threshold * 0.6 (≈0.24)` |
| `alpha_flip` | alpha 方向反转，且强度 >= exit_threshold | `exit_threshold=0.15` |
| `requote` | 目标报价相对当前 working price 偏移 >= 0.5 tick | 价格变化触发 |
| `fair_drift` | fair price 与 working price 偏离过大 | `max_fair_drift_ticks=1.5` |
| `pending_timeout` | working order 超过最大存活时间 | `max_pending_ms=2500` |
| `abnormal_*` | 市场进入异常状态 | MarketState=ABNORMAL |
| `ofi_flip` | OFI 方向反转（信号层撤单） | tape_ofi 反向 |
| `book_imbalance_flip` | 盘口失衡反向 | obi_raw 转向不利 |
| `microprice_flip` | microprice 穿越 mid 到反向 | microprice 反向 |

### 6.2 不应撤单的情况

- 订单存活时间 < `min_order_lifetime_ms`（250ms），除非市场进入 ABNORMAL 或风险退出。
- lollipop 正在管理退出（active TP 或 force exit 进行中）。
- 有反向 active order。

### 6.3 重挂预算

频繁撤单带来 API 风险、队列位置损失和监管风险：

```text
max_requotes_per_minute = 30     # 滑动 60s 窗口
min_order_lifetime_ms = 250      # 挂单最小存活时间
max_pending_ms = 2500            # 未成交最大存活时间
```

### 6.4 订单年龄控制

不同市场的刷新周期参考：

| 市场 | 刷新周期 |
| --- | --- |
| 高频期货（日经 micro） | 100ms ~ 500ms |
| 日本大盘股 | 500ms ~ 3s |
| 低频挂单 | 5s ~ 30s |

---

## 7. 出场模型

### 7.1 Maker Take Profit

入场成交后调用：

```text
apply_fill(side, qty, price, now_ns, entry_mode="maker")
```

`LollipopTPManager.on_entry_fill()` 进入 `SCHEDULED`：

```text
submit_after_ns = entry_ts_ns + tp_delay_ms
tp_price = avg_price + maker_tp_ticks * tick_size
```

下一次 board tick 满足 delay 后输出 maker TP：

```text
exit_intent.is_market = false
exit_intent.strategy = "lollipop_tp"
exit_intent.reason = "limit_tp"
```

这就是 "Maker 入场 + Maker 止盈" 的完整闭环。

### 7.2 Taker Escape

如果 TP 长时间没有成交，或触发止损，lollipop 进入 `TIMEOUT` 并输出 force exit：

```text
exit_intent.is_market = true
exit_intent.strategy = "lollipop_tp"
exit_intent.reason = "timeout_exit"
```

### 7.3 信号恶化主动逃生

以下任一触发时应撤掉 TP 单，用 taker 快速平仓：

```text
做多后如果：
  book_imbalance 跌破 obi 阈值（偏多→中性→偏空）
  tape_ofi 明显转空
  microprice 跌破 mid
  bid 深度突然消失（cancel_ratio > 0.60）
  vol_expansion = True（波动率异常扩张）
```

这是 lollipop 的信号敏感退出扩展；当前代码已有 flow-flip taker 逃生和 working-order 撤单保护，持仓后的主退出仍以 TP / timeout / stop 为核心。

### 7.4 Stranded Partial Fill

`kabu_hft_new` 对 entry 部分成交后取消/过期的情况：

- 残余库存保留为 open inventory。
- 不直接假设全撤无风险。
- 后续由正常 OPEN 状态维护 take-profit quote，或风险退出。

当前 Python 版本已有 broker reconciliation 基础流程；实盘前仍需要用券商订单快照回放验证部分成交、残余库存和重连同步。

---

## 8. 库存管理

### 8.1 库存偏斜与 Reservation Price

库存控制是 Maker 策略最重要的部分之一：

```text
多头库存 0%    → reservation = fair（中性）
多头库存 50%   → reservation = fair - 0.5 tick（减少继续买，想卖）
多头库存 70%+  → reservation = fair - 1.05 tick（1.5x 加速；强烈想卖）
多头库存 100%  → 停止挂 bid，只允许 ask / taker 减仓
```

### 8.2 库存上限

```text
position.qty >= max_inventory_qty
→ 停止挂 bid（做多方向）
→ 只保留 ask 和 taker 减仓
```

### 8.3 库存评分补充

Maker 评分中加入库存状态（Maker v2 扩展）：

```text
score = 0
spread 正常             +1
盘口深度充足             +1
book imbalance 偏多      +2
microprice > mid         +2
tape OFI 不偏空          +2
短期波动率稳定            +1
库存允许继续买           +2   （inventory_ratio < 0.66）

score >= 8 → 允许挂 bid
```

---

## 9. 风控过滤

### 9.1 当前 Python v1 已有过滤

| 过滤 | 参数/状态 | 行为 |
| --- | --- | --- |
| spread | `max_spread_ticks=3.0` | spread 超过阈值拒绝入场。 |
| stale quote | `stale_quote_ms=2000` | 行情过期拒绝入场。 |
| inventory | `max_inventory_qty=300` | 超过最大持仓拒绝入场。 |
| notional | `max_notional` | 超过最大名义金额拒绝入场。 |
| session | `enforce_session` | 非允许时段拒绝入场。 |
| working entry | `entry_order_active` | 已有入场订单时拒绝重复挂单。 |
| lollipop active | `lollipop.is_busy` | 持仓退出管理中拒绝新开仓。 |
| consecutive loss | `consecutive_loss_limit` | 连续亏损后进入 cooling。 |

### 9.2 `kabu_hft_new` 已实现的扩展过滤

| 过滤 | 参数 | 行为 |
| --- | --- | --- |
| session windows | `open_start_hhmm`/`open_end_hhmm` | 开盘和午后重开窗口，精确到分钟。 |
| daily loss limit | `daily_loss_limit=-50000` | 日内亏损达到阈值停止开仓。 |
| consecutive loss cooling | `consecutive_loss_limit=3`, `cooling_seconds=300` | 连续 3 次亏损 → 5 分钟冷静期。 |
| ATR-aware sizing | `vol_threshold` | 波动扩大时下单量减半。 |
| drawdown cut | 50% daily loss 时 | 亏损过半时下单量减半。 |
| signal boost | score >= 1.0 时 | 强信号时允许下单量 1.5x。 |
| max hold timer | `max_hold_seconds=45` | 持仓超过时间上限后退出；硬上限 = 45s * 3 = 135s。 |
| hard stop-loss | `stop_loss_ticks=1.5` | 持仓亏损 >= 1.5 tick 立刻市价平仓。 |
| startup position check | 启动时 | 检查经纪商已有持仓，避免重复建仓。 |
| REST pacing | token bucket | 控制 REST 请求速率，避免 API 限速。 |

### 9.3 Maker 专属风险

- **adverse selection**：成交后价格立刻朝不利方向移动。markout 长期负 → 被毒性订单选择。
- **queue position**：排队太靠后，成交概率低但撤单成本高。
- **cancel latency**：行情变坏时撤单是否及时（kabu REST 延迟需要测量）。
- **event burst**：事件频率过高时信号失效，不能继续 maker。
- **hidden liquidity**：L2 深度不等于真实成交概率，需要 paper fill 模型验证。

### 9.4 高波动 Kill Switch

当盘口进入异常状态（任一触发）：

```text
spread 突然扩大 >= 6 ticks
盘口深度消失（cancel_ratio > 0.80）
价格跳动 >= 4 ticks
成交流单边冲击（event burst）
API 延迟变大
stale_quote > 2s
```

执行：

```text
撤所有 working maker 订单
暂停 maker entry
只允许 lollipop / taker 风控平仓
```

---

## 10. 参数说明

### 10.1 `StrategyConfig`

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `trade_qty` | 100 | 单次目标下单数量，最终按 `lot_size` 对齐。 |
| `allow_short` | false | 允许做空镜像逻辑（实盘前单独验证）。 |
| `maker_score_threshold` | 6 | maker 入场最低分（满分 13）。 |
| `taker_score_threshold` | 9 | taker 入场最低分；用于决定是否被 taker 抢先。 |
| `maker_confirm_ticks` | 2 | maker 连续确认次数（防抖）。 |
| `taker_confirm_ticks` | 1 | taker 连续确认次数。 |
| `book_imbalance_long` | 0.18 | OBI 同向阈值。 |
| `of_imbalance_long` | 0.10 | LOB OFI 同向阈值。 |
| `tape_imbalance_long` | 0.10 | Tape OFI 同向阈值。 |
| `microprice_tilt_long` | 0.25 | microprice tilt 同向阈值（ticks）。 |
| `mom_long_threshold` | 0.0 | micro momentum 同向阈值。 |
| `strong_signal_multiplier` | 1.5 | strong tape 倍数（taker breakout 用）。 |
| `maker_join_best` | true | true 时挂 best bid / best ask；false 时 retreat。 |
| `maker_retreat_ticks` | 1.0 | `maker_join_best=false` 时退后的 tick 数。 |
| `wall_consumed_ratio_min` | 0.60 | wall-break taker 的最低消耗比例。 |
| `use_depth_thin_taker` | true | 启用盘口对手盘变薄 taker 触发。 |
| `use_wall_break_taker` | true | 启用 wall-break taker 触发。 |
| `use_cancel_imbalance_taker` | true | 启用 cancel-imbalance taker 触发。 |
| `use_price_breakout_taker` | true | 启用 price-breakout taker 触发。 |
| `use_vol_expansion_taker` | true | 启用 volatility-expansion taker 触发。 |
| `opposite_depth_ratio_max` | 0.50 | 对手盘 best depth / 本方 best depth 的最大比例。 |
| `cancel_imbalance_ratio_min` | 0.40 | cancel imbalance 的最低撤单比例。 |
| `cancel_imbalance_extreme_ratio` | 0.80 | 极端撤单比例，达到后阻止追单。 |
| `taker_burst_min` | 0.0 | taker depth / wall / cancel 触发的最小 burst 分数。 |
| `maker_cancel_tape_1s_threshold` | 0.15 | working order 反向 1s tape 撤单阈值。 |
| `maker_cancel_burst_threshold` | 0.25 | working order 反向 burst 撤单阈值。 |
| `maker_cancel_cancel_ratio_min` | 0.60 | working order 本方队列撤单过快的撤单阈值。 |
| `signal_expire_ms` | 500 | 信号时效（adverse selection 防护）。 |
| `max_slip_ticks` | 2.0 | IOC taker 最大允许滑点 tick 数；实盘默认挂到对手价外 2 ticks。 |

### 10.2 `SignalConfig`

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `book_depth_levels` | 5 | weighted book 使用档位数。 |
| `book_decay` | 0.75 | 越远档位的衰减权重。 |
| `tape_window_seconds` | 15 | Tape OFI 主窗口。 |
| `zscore_window` | 120 | z-score 滚动窗口（`kabu_hft_new` 推荐 300）。 |
| `mid_std_window` | 60 | mid 波动估计窗口。 |
| `wall_ratio_threshold` | 2.5 | 识别"墙"所需的 size / EMA 倍数。 |
| `wall_ema_alpha` | 0.10 | 墙检测 EMA 衰减率。 |
| `breakout_lookback_bars` | 20 | 价格突破的回看 bar 数。 |
| `vol_expansion_ratio` | 2.0 | 波动率扩张检测倍数。 |

### 10.3 `RiskConfig`

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `max_inventory_qty` | 300 | 最大持仓数量。 |
| `max_notional` | 3_000_000 | 最大名义金额。 |
| `max_spread_ticks` | 3.0 | 最大允许 spread（ticks）。 |
| `stale_quote_ms` | 2000 | 行情最大允许延迟。 |
| `enforce_session` | false | 是否启用交易时段过滤。 |
| `consecutive_loss_limit` | 0 | 连续亏损冷却触发次数（0 = 禁用）。 |
| `cooling_seconds` | 120 | 冷静期时长（推荐 300s）。 |

### 10.4 `LollipopConfig`

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `maker_tp_ticks` | 2.0 | maker 入场后的 TP 距离（ticks）。 |
| `taker_tp_ticks` | 3.0 | taker 入场后的 TP 距离（ticks）。 |
| `maker_max_hold_seconds` | 45 | maker 入场最大持仓时间。 |
| `taker_max_hold_seconds` | 30 | taker 入场最大持仓时间。 |
| `tp_delay_ms` | 50 | 入场成交后延迟多久提交 TP。 |
| `max_retries` | 5 | TP 重试次数预算。 |
| `stop_loss_ticks` | 0.0 | 固定 tick 止损（0 = 禁用；实盘推荐 1.5~2.0）。 |

---

## 11. 策略扩展池（按优先级）

按日本股票 / 日经225 micro 的实际方向，推荐实现顺序如下：

| 优先级 | 策略 | 当前状态 | 推荐度 |
| --: | --- | --- | --- |
| 1 | Maker 入场 + Taker 逃生 | ✅ v1 已实现（lollipop） | 最高 |
| 2 | Book Imbalance Maker | ✅ v1 已实现（entry score） | 最高 |
| 3 | Microprice Maker | ✅ v1 已实现（entry score） | 最高 |
| 4 | OFI 过滤 Maker | ✅ v1 已实现（entry score） | 最高 |
| 5 | 动态点差 Maker | ✅ 已实现：基于 `mid_std_ticks` / `vol_expansion` 调整 half spread | 高 |
| 6 | 库存偏斜 Maker | ✅ 已实现：reservation_price + skew_ticks | 高 |
| 7 | Fair Price / Alpha Tilt | ✅ 已实现：composite 驱动 fair_price | 高 |
| 8 | Queue Defense Mode | ✅ 已实现：薄队列退后一档，working order 可按队列变薄撤单 | 高 |
| 9 | Working Order 撤单逻辑 | ✅ 已实现：alpha decay / flip / fair drift / tape / burst / cancel ratio | 高 |
| 10 | 多层挂单 Maker | 📋 v3：近端小单，远端大单 | 中高 |
| 11 | ATR-aware Position Sizing | ✅ 已实现：可按 score/vol 配置化缩放 | 高 |
| 12 | Avellaneda-Stoikov 参数化 | 📋 v3：reservation + optimal spread 公式化 | 中高 |
| 13 | GLFT 模型 | 📋 v3：order arrival intensity + half spread | 中高 |
| 14 | 跨市场对冲 Maker | 📋 v4：日经 micro + 个股联动 | 中 |
| 15 | RL Market Making | ❌ 优先级低 | 低 |

### 11.1 Dynamic Spread Maker（v2 扩展）

目标：

- 使用 `mid_std_ticks`、`vol_expansion` 和 `spread ticks` 动态决定 half spread。
- 低波动：join best 或 improve。
- 高波动 / vol_expansion=True：retreat 一档或停止挂单。

### 11.2 Book Imbalance Maker（当前已有基础）

目标：

- 盘口偏多时：允许 bid maker，抑制 ask maker（ask 挂远一点）。
- 盘口偏空时：允许 ask maker，抑制 bid maker。
- imbalance 快速反向 → 撤 working order。

### 11.3 Microprice Maker（当前已有基础）

目标：

- microprice > mid → join bid 更积极；ask 挂远一点。
- microprice 反向 → 撤 working order / 触发逃生。

### 11.4 OFI Filter Maker（当前已有基础）

目标：

```text
if tape_ofi_strong_buy:
    不要挂 ask（你的卖单被打后继续上涨）
if tape_ofi_strong_sell:
    不要挂 bid（你的买单被打后继续下跌）
```

当前代码已将 `tape_ofi_1s`、`trade_burst_score` 和 same-side cancel ratio 接入 working-order 实时撤单保护。

### 11.5 Hanging Orders（均值回归 Maker）

一边订单成交后，另一边保留一段时间等待价格回归：

```text
bid 成交后 → ask 保留，等反弹卖出
```

适合：震荡行情 / 盘口稳定
不适合：单边趋势 / 新闻冲击 / 流动性消失

---

## 12. 日志与复盘指标

Maker 复盘重点不是胜率，而是被动成交是否真的赚到 spread，且没有被毒性订单系统性选择。

每笔 maker intent 建议记录：

- `entry_mode`、`entry_score`、`confirm_progress`、`blocked_reason`
- `price`、`reference_price`、`spread_ticks`
- `queue_ahead_qty`（估算成交时前方队列）
- `working_age_ms`、`requote_count`、`cancel_reason`
- `fill_price`、`slippage_ticks`
- `markout_100ms`、`markout_500ms`、`markout_1s`、`markout_3s`
- `signal.obi_raw`、`signal.tape_ofi_raw`、`signal.microprice_tilt_raw`、`signal.integrated_ofi`

核心指标：

| 指标 | 用途 |
| --- | --- |
| maker fill rate | maker 意图真实成交率。 |
| queue ahead | 成交时前方队列估计。 |
| average working age | 挂单平均存活时间。 |
| cancel rate | 撤单压力和 API 负担。 |
| requote count | 报价稳定性。 |
| spread capture | 实际赚到的 spread。 |
| adverse selection | 成交后短期 markout 是否不利。 |
| markout buckets | 100ms / 500ms / 1s / 3s 成交后表现。 |
| maker/taker exit split | maker TP 出场比例 vs taker escape 比例。 |
| PnL per trade / per 100 trades | 策略稳定性。 |

Maker 质量判定：

```text
spread_capture
+ favorable_markout
- adverse_selection
- fee
- cancel_cost_proxy
> 0
```

markout 100ms / 500ms 长期为负 → 策略被毒性订单系统性选择 → 加宽报价、减少数量、提高阈值或暂停 maker。

---

## 13. 核心伪代码

```python
def on_board(book, trades, position, now_ns):
    # 1. 行情质量检查
    if market_state == ABNORMAL:
        cancel_all_working_orders()
        return

    # 2. 计算信号
    signal = engine.on_board(book)

    # 3. 报价中心价格
    fair_price = calc_fair_price(signal.composite, book.mid)
    reservation_price = calc_reservation(fair_price, position, max_inventory_qty)

    # 4. 动态点差
    half_spread = calc_dynamic_spread(signal.mid_std_ticks, signal.vol_expansion)

    # 5. 方向过滤
    bid_allowed = (
        signal.obi_raw >= book_imbalance_long
        and signal.tape_ofi_raw > -tape_imbalance_long
        and signal.microprice >= book.mid
        and position.qty < max_inventory_qty
    )
    ask_allowed = (
        signal.obi_raw <= -book_imbalance_long
        and signal.tape_ofi_raw < tape_imbalance_long
        and signal.microprice <= book.mid
        and position.qty > -max_inventory_qty
    )

    # 6. 报价价格计算
    bid_price = align_tick(reservation_price - half_spread * tick_size, side=+1)
    ask_price = align_tick(reservation_price + half_spread * tick_size, side=-1)

    # 7. Improve / Retreat
    if signal_strong and book.spread >= 2 ticks:
        bid_price = book.bid + tick_size   # Tick Improvement
    elif reservation_price <= book.bid - tick_size:
        bid_price = book.bid - tick_size   # Step-back

    # 8. Working order 管理
    if bid_allowed and no_working_entry:
        place_or_replace_bid(bid_price, qty)
    elif working_bid and should_cancel(signal, working_bid):
        cancel_bid()

    if ask_allowed and position.qty > 0:
        place_or_replace_ask(ask_price, position.qty)

    # 9. Lollipop 出场
    lollipop_action = lollipop.tick(book, position, now_ns)
    if lollipop_action.action != "none":
        submit_exit(lollipop_action.intent)
```

---

## 14. 参考资料

| 资料 | 参考价值 |
| --- | --- |
| [hftbacktest](https://github.com/nkaz001/hftbacktest) | 盘口回放、队列位置、延迟模拟、maker fill 概率估算；**Order Book Imbalance 教程**最直接。 |
| [Hummingbot Pure Market Making](https://hummingbot.org/strategies/v1-strategies/pure-market-making/) | 基础 maker 生命周期、order refresh、inventory skew、hanging orders。 |
| [Hummingbot Avellaneda Market Making](https://hummingbot.org/strategies/v1-strategies/avellaneda-market-making/) | reservation price、optimal spread、risk factor、order amount adjustment。 |
| [hftbacktest GLFT tutorial](https://hftbacktest.readthedocs.io/en/py-v2.0.0/tutorials/GLFT%20Market%20Making%20Model%20and%20Grid%20Trading.html) | GLFT、half spread、skew、grid 做市研究路径；适合日经 micro 回测。 |
| [fedecaccia/avellaneda-stoikov](https://github.com/fedecaccia/avellaneda-stoikov) | reservation price 和 optimal spread 最小 Python 实现，教学用途。 |
| [market-maker-rs](https://github.com/joaquinbejar/market-maker-rs) | Rust 做市库：Avellaneda-Stoikov / GLFT / Grid / Adaptive Spread / 库存限制 / 熔断。 |
| [jshellen/HFT](https://github.com/jshellen/HFT) | 随机控制做市、库存惩罚、microprice 决策、adverse selection 建模（研究用）。 |
| [NautilusTrader](https://nautilustrader.io/) | 事件驱动交易架构参考；未来 kabu / IBKR / Longbridge 统一框架可参考其架构。 |

---

## 15. 升级路线

建议按以下顺序从 v1 推进到 v2：

1. ✅ `MakerStrategy` 已从 join/retreat 扩展到 `fair_price` + `reservation_price` 动态报价。
2. ✅ `composite`、inventory skew 和 fair drift 已进入 maker price / 撤单判断。
3. ✅ working-order 撤单已覆盖 alpha decay、alpha flip、fair drift、spread expanded、stale quote、短窗口 tape、burst、queue/cancel 防守。
4. ✅ `MarketStateDetector`（NORMAL/QUEUE/ABNORMAL）已接入，ABNORMAL 时禁止 maker 开仓。
5. 🔧 queue ahead / paper fill model 仍建议继续加强，用于复盘 maker fill quality。
6. ✅ dynamic spread 已基于 `mid_std_ticks` 和 `vol_expansion` 接入。
7. 串联 lollipop active TP 的撤单/重挂/force-exit，避免 TP 和逃生单冲突。
8. 增加 maker markout 日志，按 entry score、spread、queue、market state 分桶。
9. 在 kabu 回放中验证 maker fill 后 100ms / 500ms / 1s markout，再考虑实盘上线。

当前 v1 最小可用闭环：

```text
市场质量过滤
+ book / OFI / microprice 同向（13 分 score >= 6）
+ maker confirm_count >= 2
=> maker passive entry intent
=> entry fill
=> lollipop maker TP（avg + 2 ticks）
=> timeout / stop taker escape
=> journal + markout review
```
