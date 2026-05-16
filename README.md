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
- `CombinedMakerTakerStrategy` gives taker priority, falls back to maker, applies confirmation
  counters, position sizing, and risk gates, then emits one `OrderIntent`.

By default this project emits dry-run order intents. The optional live kabu REST adapter sits at
the `OrderIntent` boundary, so the strategy layer stays testable.

## Run

```powershell
cd D:\kabu_maker_taker
python main.py --config config.example.json --sample
python -m unittest discover -s tests -p "test_*.py"
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

## Live kabu REST execution

Live mode is explicit and guarded:

```powershell
python main.py --config config.json --events events.jsonl --live
```

Requirements:

- `config.json` must set `"dry_run": false`.
- `config.json` must include `kabu.api_password`.
- `risk.api_error_limit` must be greater than `0`, so live REST failures can trip the API circuit breaker.
- `risk.order_latency_limit_ms`, `risk.cancel_latency_limit_ms`, `risk.poll_latency_limit_ms`,
  and `risk.latency_breach_limit` protect live mode from slow REST responses. The default is
  `3000ms` for each request class and `3` consecutive breaches.
- `--live --sample` is rejected so embedded sample events cannot place real orders.

The live adapter starts by fetching a kabu Station token, checking broker positions/orders, and
reconciling positions into the strategy. It refuses to start if kabu Station already has active
orders that this process cannot safely own.

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
provide a full WebSocket market-data loop or unattended live-trading runtime.
