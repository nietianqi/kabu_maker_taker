# kabu-maker-taker

Python scaffold that separates maker and taker entry policies, then combines them behind one
coordinator. It is intended to mirror the useful parts of `kabu_hft_new` and `kabu_micro_edge_c`
without coupling strategy logic to a broker gateway.

## Strategy shape

- Shared signal engine computes book imbalance, LOB OFI, tape pressure, micro-momentum,
  microprice tilt, integrated OFI, trade burst, and rolling z-scores.
- `MakerStrategy` looks for confirmed directional edge and returns a passive best-bid/best-ask
  limit order.
- `TakerStrategy` only fires on stronger breakout conditions and returns an aggressive market
  intent.
- `CombinedMakerTakerStrategy` uses adaptive maker/taker selection, applies confirmation counters,
  position sizing, and risk gates, then emits one `OrderIntent`.

By default this project emits dry-run order intents. The optional live kabu REST adapter sits at
the `OrderIntent` boundary, so the strategy layer stays testable.

## Run

```powershell
cd D:\kabu_maker_taker
python main.py --config config.example.json --sample
python -m unittest discover -s tests -p "test_*.py"
```

`python main.py` is now the multi-symbol live launcher. It reads
`config.live.multi.json` by default, generates one single-symbol worker config per stock, runs
preflight first when `preflight_before_real=true`, then starts the real live workers.

```powershell
copy config.live.multi.example.json config.live.multi.json
notepad config.live.multi.json
python main.py
```

For validation runs with the same multi-symbol JSON:

```powershell
python main.py --mode preflight
python main.py --mode shadow
```

The original single-symbol CLI is still available:

```powershell
python -m kabu_maker_taker.app --config config.example.json --sample
python main.py --legacy-app --config config.example.json --sample
```

## Event JSONL

The CLI can also read normalized JSONL events:

```json
{"type":"trade","symbol":"9984","ts_ns":1770000000000000000,"price":100.8,"size":300,"side":1}
{"type":"board","symbol":"9984","ts_ns":1770000000100000000,"bid":100.0,"ask":101.0,"bid_size":1000,"ask_size":200}
```

Then run:

```powershell
python main.py --config config.example.json --events events.jsonl
```

## Live kabu WebSocket + REST execution

Live mode is explicit and guarded:

For the first live day, use preflight + shadow mode. This connects to real kabu Station market
data and broker snapshots, but never submits or cancels real orders:

```powershell
copy config.live.shadow.example.json config.live.shadow.json
python main.py --config config.live.shadow.json --preflight-live
python main.py --config config.live.shadow.json --live --shadow
```

For multi-symbol preflight + shadow, use the launcher:

```powershell
python main.py --mode preflight
python main.py --mode shadow
```

Real order submission is locked behind two explicit controls and is not recommended for the
first validation day:

```powershell
python main.py --config config.json --live --allow-real-orders
```

Requirements:

- `config.json` must set `"dry_run": false`.
- `config.json` must include `kabu.api_password`.
- Live mode without `--events` registers the configured symbol with kabu Station and consumes the
  `/kabusapi/websocket` board stream. Set `kabu.websocket_url` only when the endpoint differs from
  the URL derived from `kabu.base_url`.
- Live mode requires the safety profile to be fully enabled: session enforcement, daily loss
  limit, entry/cancel rate limits, stale quote and stale board guards, API and latency circuits,
  decision trace, trade journal, and abnormal-market detection.
- Live mode requires `strategy.entry_selection_policy` to be explicit (`adaptive`,
  `taker_priority`, or `maker_priority`) so old configs do not silently change entry routing.
- `--preflight-live` validates token retrieval, broker flatness, log writability, symbol
  registration, WebSocket connectivity, and fresh board messages, then writes a same-day
  `live_preflight_stamp.json` in `log_dir`.
- `--live --shadow` requires a fresh preflight stamp and a flat broker account for the configured
  symbol. It records `shadow_would_submit` / `shadow_would_cancel` events and marks local orders
  as `shadow_not_sent`; it never calls kabu `sendorder` or `cancelorder`.
- `--live` without `--shadow` is rejected unless `--allow-real-orders` is provided, the preflight
  stamp is still fresh, and the configured `kabu.live_arm_path` file exists.
- `kabu.startup_open_order_policy` defaults to `reject`. Set it to `ignore` only when you want
  startup to skip existing manual/broker orders without adopting, cancelling, or tracking them.
  Ignored orders are written as `ignored_broker_open_orders` in startup/preflight output.
- The multi-symbol launcher keeps these same real-order gates. For each stock it derives separate
  `log_dir`, `halt_SYMBOL.txt`, `halt_hard_SYMBOL.txt`, and `live_arm_SYMBOL.txt` paths unless the
  stock item explicitly overrides them.
- `risk.order_latency_limit_ms`, `risk.cancel_latency_limit_ms`, `risk.poll_latency_limit_ms`,
  and `risk.latency_breach_limit` protect live mode from slow REST responses. The default is
  `3000ms` for each request class and `3` consecutive breaches.
- `--live --sample` is rejected so embedded sample events cannot place real orders.
- `--live --events` remains a validation mode only. Every event must include a fresh `ts_ns`
  within `risk.stale_quote_ms` of the local wall clock; stale or future JSONL events are rejected
  before live startup.

The live adapter starts by fetching a kabu Station token, checking broker positions/orders, and
reconciling positions into the strategy. It refuses to start if kabu Station already has active
orders that this process cannot safely own.

The WebSocket live loop treats disconnects and message timeouts as market-data faults. If the
strategy has any local exposure it halts and runs emergency flatten; if flat, it attempts the
configured number of reconnects and reconciles with the broker before resubscribing.

Live execution has two independent stop circuits. API failures trip the existing API circuit,
while slow `submit`, `cancel`, or order-poll REST calls trip the latency circuit after consecutive
breaches. Either circuit stops live execution with `status=live_halted`; during the latency cooling
window, new entries are blocked with `latency_circuit_open`. The latency cooling duration reuses
`risk.api_cooling_seconds`.

In live mode, aggressive taker intents and lollipop timeout/stop exits are mapped to kabu
`IOC指値` orders (`FrontOrderType=27`) with a limit price derived from `reference_price` plus
`strategy.max_slip_ticks` (or the intent's `max_slip_ticks` when set). This preserves the strategy
meaning of `OrderIntent.is_market=true` while avoiding unbounded live market-order slippage.

This remains a REST execution validation mode for controlled `--events` input; it does not yet
provide broker-side order push callbacks; order and fill state is still reconciled through guarded
REST polling.
