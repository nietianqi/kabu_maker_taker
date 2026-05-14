from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .combined import CombinedMakerTakerStrategy
from .config import AppConfig, load_config
from .models import BoardSnapshot, TradePrint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run combined maker/taker strategy in dry-run intent mode.")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--events", help="JSONL file containing board/trade events.")
    parser.add_argument("--sample", action="store_true", help="Run an embedded sample event sequence.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    strategy = CombinedMakerTakerStrategy(config)

    if args.events:
        events = _read_jsonl(Path(args.events))
    elif args.sample:
        events = _sample_events(config)
    else:
        print(json.dumps({"status": "no_events", "hint": "use --sample or --events events.jsonl"}))
        return 0

    for event in events:
        event_type = str(event.get("type", "board"))
        if event_type == "trade":
            strategy.on_trade(TradePrint.from_dict(event))
            continue
        snapshot = BoardSnapshot.from_dict(
            event,
            kabu_bidask_reversed=config.signals.kabu_bidask_reversed,
            auto_fix_negative_spread=config.signals.auto_fix_negative_spread,
        )
        result = strategy.on_board(snapshot, now_ns=snapshot.ts_ns)
        if result.intent is not None:
            print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))

    final = strategy.last_result.to_dict() if strategy.last_result else {}
    print(json.dumps({"status": "done", "last": final}, ensure_ascii=False, separators=(",", ":")))
    return 0


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _sample_events(config: AppConfig) -> list[dict[str, Any]]:
    base = time.time_ns()
    symbol = config.symbol
    return [
        {"type": "trade", "symbol": symbol, "ts_ns": base, "price": 100.8, "size": 300, "side": 1},
        {
            "type": "board",
            "symbol": symbol,
            "exchange": config.exchange,
            "ts_ns": base + 100_000_000,
            "bid": 100.0,
            "ask": 101.0,
            "bid_size": 900,
            "ask_size": 250,
            "bids": [{"price": 100.0, "size": 900}, {"price": 99.0, "size": 500}],
            "asks": [{"price": 101.0, "size": 250}, {"price": 102.0, "size": 300}],
        },
        {"type": "trade", "symbol": symbol, "ts_ns": base + 150_000_000, "price": 101.0, "size": 500, "side": 1},
        {
            "type": "board",
            "symbol": symbol,
            "exchange": config.exchange,
            "ts_ns": base + 200_000_000,
            "bid": 101.0,
            "ask": 102.0,
            "bid_size": 1200,
            "ask_size": 180,
            "bids": [{"price": 101.0, "size": 1200}, {"price": 100.0, "size": 700}],
            "asks": [{"price": 102.0, "size": 180}, {"price": 103.0, "size": 220}],
        },
    ]


if __name__ == "__main__":
    raise SystemExit(main())

