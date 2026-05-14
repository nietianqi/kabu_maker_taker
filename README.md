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

This project currently emits dry-run order intents. The live kabu REST adapter should be added at
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

