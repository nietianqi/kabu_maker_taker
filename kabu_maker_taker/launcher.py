from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .execution import KabuApiError, KabuRestClient
from .execution.client import SHARED_KABU_TOKEN_ENABLED_ENV, SHARED_KABU_TOKEN_ENV
from .live_runtime import validate_live_preflight_stamp


DEFAULT_MULTI_CONFIG = "config.live.multi.json"
VALID_MODES = {"real", "shadow", "preflight"}


@dataclass(frozen=True, slots=True)
class ChildRunSpec:
    symbol: str
    config_path: Path
    args: tuple[str, ...]
    live_arm_path: Path | None = None
    app_config: AppConfig | None = None
    raw_config: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LauncherArmFile:
    symbol: str
    path: Path
    created: bool


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
        validate_account_risk(payload, specs)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "launcher_config_error", "reason": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 2

    if mode == "preflight":
        return _run_preflight(specs)
    arm_files: list[LauncherArmFile] = []
    if mode == "real" and bool(payload.get("preflight_before_real", True)):
        preflight_specs = build_child_specs(payload, mode="preflight", config_path=config_path)
        preflight_code = _run_preflight(preflight_specs)
        if preflight_code != 0:
            return preflight_code
        if bool(payload.get("auto_arm_after_preflight", True)):
            try:
                arm_files = _arm_real_workers(specs)
            except OSError as exc:
                print(
                    json.dumps(
                        {"status": "launcher_arm_failed", "reason": str(exc)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return 2
    try:
        worker_envs = _prepare_shared_worker_envs(specs, mode=mode)
        return _run_workers(specs, worker_envs=worker_envs)
    except (KabuApiError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "launcher_shared_token_failed", "reason": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2
    finally:
        _cleanup_launcher_arm_files(arm_files)


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
        app_config = AppConfig.from_dict(child)
        child_path = generated_dir / f"config.{symbol}.json"
        child_path.write_text(json.dumps(child, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        live_arm_path = _live_arm_path(child)
        specs.append(
            ChildRunSpec(
                symbol=symbol,
                config_path=child_path,
                args=tuple(_app_args(child_path, mode)),
                live_arm_path=live_arm_path,
                app_config=app_config,
                raw_config=copy.deepcopy(child),
            )
        )
    return specs


def validate_account_risk(payload: dict[str, Any], specs: list[ChildRunSpec]) -> None:
    """Static launcher-level cap for the sum of per-worker live risk limits."""
    account_risk = payload.get("account_risk")
    if account_risk is None:
        return
    if not isinstance(account_risk, dict):
        raise ValueError("account_risk must be an object")
    if not bool(account_risk.get("enabled", False)):
        return
    max_total_inventory_qty = int(account_risk.get("max_total_inventory_qty", 0) or 0)
    max_total_notional = float(account_risk.get("max_total_notional", 0.0) or 0.0)
    if max_total_inventory_qty < 0:
        raise ValueError("account_risk.max_total_inventory_qty must be >= 0")
    if max_total_notional < 0:
        raise ValueError("account_risk.max_total_notional must be >= 0")

    total_inventory_qty = 0
    total_notional = 0.0
    details: list[dict[str, object]] = []
    for spec in specs:
        if spec.app_config is None:
            raise ValueError(f"{spec.symbol}: missing app config")
        risk = spec.app_config.risk
        qty = max(int(risk.max_inventory_qty), 0)
        notional = max(float(risk.max_notional), 0.0)
        total_inventory_qty += qty
        total_notional += notional
        details.append({"symbol": spec.symbol, "max_inventory_qty": qty, "max_notional": notional})

    if max_total_inventory_qty > 0 and total_inventory_qty > max_total_inventory_qty:
        raise ValueError(
            "account_risk.max_total_inventory_qty exceeded "
            f"configured={total_inventory_qty} limit={max_total_inventory_qty} details={details}"
        )
    if max_total_notional > 0 and total_notional > max_total_notional:
        raise ValueError(
            "account_risk.max_total_notional exceeded "
            f"configured={total_notional:.0f} limit={max_total_notional:.0f} details={details}"
        )


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


def _arm_real_workers(specs: list[ChildRunSpec]) -> list[LauncherArmFile]:
    arm_files: list[LauncherArmFile] = []
    for spec in specs:
        if spec.live_arm_path is None:
            raise OSError(f"{spec.symbol}: missing kabu.live_arm_path")
        path = spec.live_arm_path
        existed = path.exists()
        if not existed:
            if path.parent != Path("."):
                path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": "armed_by_launcher",
                "symbol": spec.symbol,
                "config": str(spec.config_path),
                "ts_utc": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        arm_file = LauncherArmFile(symbol=spec.symbol, path=path, created=not existed)
        arm_files.append(arm_file)
        print(
            json.dumps(
                {
                    "status": "launcher_arm_ready",
                    "symbol": spec.symbol,
                    "path": str(path),
                    "created": arm_file.created,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return arm_files


def _prepare_shared_worker_envs(specs: list[ChildRunSpec], *, mode: str) -> dict[str, dict[str, str]]:
    if len(specs) <= 1:
        return {}
    _validate_worker_start_gates(specs, mode=mode)

    password_by_base_url: dict[str, str] = {}
    specs_by_base_url: dict[str, list[ChildRunSpec]] = {}
    for spec in specs:
        if spec.app_config is None:
            raise ValueError(f"{spec.symbol}: missing app config")
        kabu = spec.app_config.kabu
        base_url = KabuRestClient(kabu.base_url).base_url
        if not kabu.api_password:
            raise ValueError(f"{spec.symbol}: kabu.api_password is required for shared token")
        previous_password = password_by_base_url.setdefault(base_url, kabu.api_password)
        if previous_password != kabu.api_password:
            raise ValueError(f"{base_url}: all workers must use the same kabu.api_password")
        specs_by_base_url.setdefault(base_url, []).append(spec)

    envs: dict[str, dict[str, str]] = {}
    for base_url, group in specs_by_base_url.items():
        assert group[0].app_config is not None
        kabu = group[0].app_config.kabu
        client = KabuRestClient(
            base_url,
            order_rate_per_sec=kabu.order_rate_per_sec,
            poll_rate_per_sec=kabu.poll_rate_per_sec,
        )
        token = client.get_token(kabu.api_password)
        for spec in group:
            envs[spec.symbol] = {
                SHARED_KABU_TOKEN_ENABLED_ENV: "1",
                SHARED_KABU_TOKEN_ENV: token,
            }
        print(
            json.dumps(
                {
                    "status": "launcher_shared_token_ready",
                    "base_url": base_url,
                    "symbols": [spec.symbol for spec in group],
                    "token_sha256_8": _token_fingerprint(token),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return envs


def _token_fingerprint(token: str) -> str:
    token = str(token or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8] if token else ""


def _validate_worker_start_gates(specs: list[ChildRunSpec], *, mode: str) -> None:
    from .app import _validate_live_config

    for spec in specs:
        if spec.app_config is None or spec.raw_config is None:
            raise ValueError(f"{spec.symbol}: missing generated config")
        live_config_errors = _validate_live_config(spec.app_config, raw_config=spec.raw_config)
        if live_config_errors:
            raise ValueError(
                f"{spec.symbol}: --live safety config incomplete: " + ", ".join(live_config_errors)
            )
        stamp_error = validate_live_preflight_stamp(spec.app_config, now_ns=time.time_ns())
        if stamp_error:
            raise ValueError(f"{spec.symbol}: --live requires fresh preflight: {stamp_error}")
        if mode == "real":
            arm_path = Path(spec.app_config.kabu.live_arm_path)
            if not arm_path.exists():
                raise ValueError(f"{spec.symbol}: --live real orders require arm file: {arm_path}")


def _run_workers(
    specs: list[ChildRunSpec],
    *,
    worker_envs: dict[str, dict[str, str]] | None = None,
) -> int:
    processes: list[tuple[ChildRunSpec, subprocess.Popen]] = []
    try:
        for spec in specs:
            print(json.dumps({"status": "launcher_worker_start", "symbol": spec.symbol}, ensure_ascii=False))
            env = None
            if worker_envs and spec.symbol in worker_envs:
                env = os.environ.copy()
                env.update(worker_envs[spec.symbol])
            processes.append((spec, subprocess.Popen([sys.executable, *spec.args], env=env)))
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


def _cleanup_launcher_arm_files(arm_files: list[LauncherArmFile]) -> None:
    for arm_file in arm_files:
        if not arm_file.created:
            continue
        try:
            arm_file.path.unlink(missing_ok=True)
            print(
                json.dumps(
                    {"status": "launcher_arm_removed", "symbol": arm_file.symbol, "path": str(arm_file.path)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "status": "launcher_arm_remove_failed",
                        "symbol": arm_file.symbol,
                        "path": str(arm_file.path),
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )


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


def _live_arm_path(child: dict[str, Any]) -> Path | None:
    kabu = child.get("kabu")
    if not isinstance(kabu, dict):
        return None
    value = str(kabu.get("live_arm_path", "")).strip()
    return Path(value) if value else None


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
