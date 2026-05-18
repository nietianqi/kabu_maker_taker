from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MULTI_CONFIG = "config.live.multi.json"
VALID_MODES = {"real", "shadow", "preflight"}


@dataclass(frozen=True, slots=True)
class ChildRunSpec:
    symbol: str
    config_path: Path
    args: tuple[str, ...]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch one or more kabu maker/taker live workers from a single multi-symbol JSON."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_MULTI_CONFIG,
        help=f"Multi-symbol live config JSON. Default: {DEFAULT_MULTI_CONFIG}",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(VALID_MODES),
        help="Override config.execution_mode. Defaults to config value, then real.",
    )
    parser.add_argument(
        "--legacy-app",
        action="store_true",
        help="Bypass the multi-symbol launcher and call the original single-symbol CLI.",
    )
    args, passthrough = parser.parse_known_args(argv)
    if args.legacy_app:
        from .app import main as app_main

        return app_main(["--config", args.config, *passthrough])

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            json.dumps(
                {
                    "status": "launcher_config_missing",
                    "path": str(config_path),
                    "hint": f"copy config.live.multi.example.json to {DEFAULT_MULTI_CONFIG}, then run python main.py",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2

    try:
        payload = _load_json(config_path)
        mode = _resolve_mode(payload, args.mode)
        specs = build_child_specs(payload, mode=mode, config_path=config_path)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "launcher_config_error", "reason": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 2

    if mode == "preflight":
        return _run_preflight(specs)
    if mode == "real" and bool(payload.get("preflight_before_real", True)):
        preflight_specs = build_child_specs(payload, mode="preflight", config_path=config_path)
        preflight_code = _run_preflight(preflight_specs)
        if preflight_code != 0:
            return preflight_code
    return _run_workers(specs)


def build_child_specs(payload: dict[str, Any], *, mode: str, config_path: Path) -> list[ChildRunSpec]:
    if mode not in VALID_MODES:
        raise ValueError(f"execution_mode must be one of {sorted(VALID_MODES)}")
    base = payload.get("base_config")
    if not isinstance(base, dict):
        raise ValueError("base_config object is required")
    stocks = payload.get("stocks", payload.get("symbols"))
    if not isinstance(stocks, list) or not stocks:
        raise ValueError("stocks array is required")
    generated_dir = Path(
        str(payload.get("generated_config_dir", Path(base.get("log_dir", "logs")) / "generated_live_configs"))
    )
    if not generated_dir.is_absolute():
        generated_dir = config_path.parent / generated_dir
    generated_dir.mkdir(parents=True, exist_ok=True)

    specs: list[ChildRunSpec] = []
    seen_symbols: set[str] = set()
    for item in stocks:
        if not isinstance(item, dict):
            raise ValueError("each stocks[] item must be an object")
        child = materialize_symbol_config(base, item)
        symbol = str(child.get("symbol", "")).strip()
        if not symbol:
            raise ValueError("stocks[] item missing symbol")
        if symbol in seen_symbols:
            raise ValueError(f"duplicate symbol in stocks: {symbol}")
        seen_symbols.add(symbol)
        child_path = generated_dir / f"config.{symbol}.json"
        child_path.write_text(json.dumps(child, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        specs.append(ChildRunSpec(symbol=symbol, config_path=child_path, args=tuple(_app_args(child_path, mode))))
    return specs


def materialize_symbol_config(base: dict[str, Any], stock: dict[str, Any]) -> dict[str, Any]:
    child = copy.deepcopy(base)
    stock_overrides = copy.deepcopy(stock)
    _deep_update(child, stock_overrides)
    symbol = str(child.get("symbol", "")).strip()
    if not symbol:
        raise ValueError("stock config requires symbol")
    child["symbol"] = symbol
    child["exchange"] = int(child.get("exchange", 27))
    child["dry_run"] = False

    if "log_dir" not in stock:
        base_log_dir = Path(str(base.get("log_dir", "logs/live")))
        child["log_dir"] = str(base_log_dir / symbol)
    if "kill_switch_path" not in stock:
        child["kill_switch_path"] = f"halt_{symbol}.txt"
    if "kill_switch_hard_path" not in stock:
        child["kill_switch_hard_path"] = f"halt_hard_{symbol}.txt"

    kabu = child.setdefault("kabu", {})
    if not isinstance(kabu, dict):
        raise ValueError(f"{symbol}: kabu config must be an object")
    stock_kabu = stock.get("kabu") if isinstance(stock.get("kabu"), dict) else {}
    if "live_arm_path" not in stock_kabu:
        kabu["live_arm_path"] = f"live_arm_{symbol}.txt"
    return child


def _app_args(config_path: Path, mode: str) -> list[str]:
    if mode == "preflight":
        return ["-m", "kabu_maker_taker.app", "--config", str(config_path), "--preflight-live"]
    if mode == "shadow":
        return ["-m", "kabu_maker_taker.app", "--config", str(config_path), "--live", "--shadow"]
    return ["-m", "kabu_maker_taker.app", "--config", str(config_path), "--live", "--allow-real-orders"]


def _run_preflight(specs: list[ChildRunSpec]) -> int:
    for spec in specs:
        print(json.dumps({"status": "launcher_preflight_start", "symbol": spec.symbol}, ensure_ascii=False))
        completed = subprocess.run([sys.executable, *spec.args], check=False)
        if completed.returncode != 0:
            print(
                json.dumps(
                    {"status": "launcher_preflight_failed", "symbol": spec.symbol, "returncode": completed.returncode},
                    ensure_ascii=False,
                )
            )
            return completed.returncode or 2
    print(json.dumps({"status": "launcher_preflight_ok", "symbols": [spec.symbol for spec in specs]}, ensure_ascii=False))
    return 0


def _run_workers(specs: list[ChildRunSpec]) -> int:
    processes: list[tuple[ChildRunSpec, subprocess.Popen]] = []
    try:
        for spec in specs:
            print(json.dumps({"status": "launcher_worker_start", "symbol": spec.symbol}, ensure_ascii=False))
            processes.append((spec, subprocess.Popen([sys.executable, *spec.args])))
        while processes:
            for spec, proc in list(processes):
                code = proc.poll()
                if code is None:
                    continue
                processes.remove((spec, proc))
                print(
                    json.dumps(
                        {"status": "launcher_worker_exit", "symbol": spec.symbol, "returncode": code},
                        ensure_ascii=False,
                    )
                )
                if code != 0:
                    _terminate_remaining(processes)
                    return code
            time.sleep(0.25)
        return 0
    except KeyboardInterrupt:
        _terminate_remaining(processes)
        return 130


def _terminate_remaining(processes: list[tuple[ChildRunSpec, subprocess.Popen]]) -> None:
    for _, proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for _, proc in processes:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _resolve_mode(payload: dict[str, Any], override: str | None) -> str:
    mode = str(override or payload.get("execution_mode", "real")).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"execution_mode must be one of {sorted(VALID_MODES)}")
    return mode


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("launcher config must be a JSON object")
    return payload


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
