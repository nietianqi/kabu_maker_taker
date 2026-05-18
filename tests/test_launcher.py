from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kabu_maker_taker.launcher import build_child_specs, materialize_symbol_config


def _payload() -> dict:
    return {
        "base_config": {
            "dry_run": False,
            "log_dir": "logs/live",
            "kabu": {
                "api_password": "pw",
                "live_arm_path": "live_arm.txt",
                "startup_open_order_policy": "ignore",
            },
            "strategy": {
                "entry_selection_policy": "adaptive",
                "trade_qty": 100,
            },
            "risk": {
                "max_inventory_qty": 100,
                "max_notional": 300000,
            },
            "market_state": {"enabled": True},
        },
        "stocks": [
            {"symbol": "9984", "exchange": 27},
            {"symbol": "7203", "exchange": 27, "risk": {"max_notional": 200000}},
        ],
    }


class LauncherConfigTests(unittest.TestCase):
    def test_materialize_symbol_config_derives_per_symbol_paths(self) -> None:
        child = materialize_symbol_config(_payload()["base_config"], {"symbol": "9984", "exchange": 27})

        self.assertFalse(child["dry_run"])
        self.assertEqual(child["symbol"], "9984")
        self.assertEqual(child["log_dir"], str(Path("logs/live") / "9984"))
        self.assertEqual(child["kill_switch_path"], "halt_9984.txt")
        self.assertEqual(child["kill_switch_hard_path"], "halt_hard_9984.txt")
        self.assertEqual(child["kabu"]["live_arm_path"], "live_arm_9984.txt")
        self.assertEqual(child["kabu"]["startup_open_order_policy"], "ignore")

    def test_build_child_specs_writes_generated_configs_and_real_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload()
            payload["generated_config_dir"] = "generated"
            specs = build_child_specs(payload, mode="real", config_path=Path(tmp) / "config.live.multi.json")

            self.assertEqual([spec.symbol for spec in specs], ["9984", "7203"])
            self.assertTrue(specs[0].config_path.exists())
            self.assertIn("--live", specs[0].args)
            self.assertIn("--allow-real-orders", specs[0].args)

    def test_build_child_specs_shadow_and_preflight_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload()
            payload["generated_config_dir"] = str(Path(tmp) / "generated")

            shadow_specs = build_child_specs(payload, mode="shadow", config_path=Path(tmp) / "config.json")
            preflight_specs = build_child_specs(payload, mode="preflight", config_path=Path(tmp) / "config.json")

            self.assertIn("--shadow", shadow_specs[0].args)
            self.assertIn("--preflight-live", preflight_specs[0].args)


if __name__ == "__main__":
    unittest.main()
