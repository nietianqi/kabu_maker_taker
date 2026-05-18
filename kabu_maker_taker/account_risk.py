"""Cross-symbol account-level risk aggregation.

In the current multi-process architecture each symbol runs as an independent
subprocess with no shared memory.  AccountRiskController bridges that gap via
small JSON *exposure files* written atomically by each symbol process under
``exposure_dir`` (e.g. ``logs/shared/exposure_9984.json``).

Usage (per symbol, inside CombinedMakerTakerStrategy):

    controller = AccountRiskController(config.account_risk)
    ...
    # After every broker fill — update our own exposure file:
    controller.write_exposure(exposure_dir, exposure)

    # Before entry — aggregate peers and gate:
    ok, reason = controller.evaluate_from_dir(
        exposure_dir, this_symbol=symbol,
        own_inventory_qty=position.qty, own_inventory_price=position.avg_price,
        own_daily_pnl=risk.daily_pnl,
        additional_qty=trade_qty, additional_price=expected_price,
    )

Derived from kabu_micro_edge_c risk.hpp AccountRiskController.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AccountRiskConfig:
    """Cross-symbol account risk limits.  0 disables each individual limit."""

    enabled: bool = False
    max_total_long_inventory: int = 0   # total shares across all symbols
    max_total_notional: float = 0.0     # JPY
    max_daily_loss: float = 0.0         # JPY positive value = cap
    exposure_dir: str = "logs/shared"

    @classmethod
    def from_dict(cls, payload: dict | None) -> "AccountRiskConfig":
        payload = payload or {}
        return cls(
            enabled=bool(payload.get("enabled", False)),
            max_total_long_inventory=int(payload.get("max_total_long_inventory", 0)),
            max_total_notional=float(payload.get("max_total_notional", 0.0)),
            max_daily_loss=float(payload.get("max_daily_loss", 0.0)),
            exposure_dir=str(payload.get("exposure_dir", "logs/shared")),
        )


@dataclass
class AccountExposure:
    symbol: str
    inventory_qty: int = 0
    inventory_price: float = 0.0
    daily_pnl: float = 0.0
    ts_ns: int = 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "inventory_qty": self.inventory_qty,
            "inventory_price": self.inventory_price,
            "daily_pnl": self.daily_pnl,
            "ts_ns": self.ts_ns,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AccountExposure":
        return cls(
            symbol=str(data.get("symbol", "")),
            inventory_qty=int(data.get("inventory_qty", 0)),
            inventory_price=float(data.get("inventory_price", 0.0)),
            daily_pnl=float(data.get("daily_pnl", 0.0)),
            ts_ns=int(data.get("ts_ns", 0)),
        )


class AccountRiskController:
    """Aggregate cross-symbol inventory / notional / PnL and gate new entries."""

    def __init__(self, config: AccountRiskConfig) -> None:
        self.config = config

    def write_exposure(self, exposure_dir: Path, exposure: AccountExposure) -> None:
        """Atomically write this symbol's exposure file."""
        if not self.config.enabled:
            return
        try:
            exposure_dir.mkdir(parents=True, exist_ok=True)
            tmp = exposure_dir / f"exposure_{exposure.symbol}.tmp"
            dst = exposure_dir / f"exposure_{exposure.symbol}.json"
            tmp.write_text(json.dumps(exposure.to_dict()), encoding="utf-8")
            os.replace(tmp, dst)
        except Exception:
            pass  # never crash the strategy on telemetry failure

    def load_peer_exposures(
        self,
        exposure_dir: Path,
        this_symbol: str,
    ) -> list[AccountExposure]:
        """Load all exposure files *except* this symbol's own file."""
        exposures: list[AccountExposure] = []
        try:
            for path in exposure_dir.glob("exposure_*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("symbol", "") != this_symbol:
                        exposures.append(AccountExposure.from_dict(data))
                except Exception:
                    pass
        except Exception:
            pass
        return exposures

    def evaluate(
        self,
        peer_exposures: list[AccountExposure],
        own_inventory_qty: int,
        own_inventory_price: float,
        own_daily_pnl: float,
        additional_qty: int = 0,
        additional_price: float = 0.0,
    ) -> tuple[bool, str]:
        """Return (allowed, reason).  Includes own position + proposed new qty."""
        if not self.config.enabled:
            return True, ""

        total_qty = own_inventory_qty + sum(e.inventory_qty for e in peer_exposures)
        total_notional = (
            own_inventory_qty * own_inventory_price
            + sum(e.inventory_qty * e.inventory_price for e in peer_exposures)
        )
        total_daily_pnl = own_daily_pnl + sum(e.daily_pnl for e in peer_exposures)

        # Include the proposed new entry in the inventory / notional check.
        projected_qty = total_qty + additional_qty
        projected_notional = total_notional + additional_qty * additional_price

        if (
            self.config.max_total_long_inventory > 0
            and projected_qty > self.config.max_total_long_inventory
        ):
            return False, "account_max_inventory"
        if (
            self.config.max_total_notional > 0.0
            and projected_notional > self.config.max_total_notional
        ):
            return False, "account_max_notional"
        if (
            self.config.max_daily_loss > 0.0
            and total_daily_pnl <= -self.config.max_daily_loss
        ):
            return False, "account_daily_loss"

        return True, ""

    def evaluate_from_dir(
        self,
        exposure_dir: Path,
        this_symbol: str,
        own_inventory_qty: int,
        own_inventory_price: float,
        own_daily_pnl: float,
        additional_qty: int = 0,
        additional_price: float = 0.0,
    ) -> tuple[bool, str]:
        """Load peers from disk then evaluate.  Returns (True, '') when disabled."""
        if not self.config.enabled:
            return True, ""
        peers = self.load_peer_exposures(exposure_dir, this_symbol)
        return self.evaluate(
            peers,
            own_inventory_qty=own_inventory_qty,
            own_inventory_price=own_inventory_price,
            own_daily_pnl=own_daily_pnl,
            additional_qty=additional_qty,
            additional_price=additional_price,
        )
