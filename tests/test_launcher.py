from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kabu_maker_taker.execution.client import SHARED_KABU_TOKEN_ENABLED_ENV, SHARED_KABU_TOKEN_ENV
from kabu_maker_taker.launcher import (
    ChildRunSpec,
    _arm_real_workers,
    _cleanup_launcher_arm_files,
    _prepare_shared_worker_envs,
    _run_workers,
    build_child_specs,
    materialize_symbol_config,
    validate_account_risk,
)


def _payload() -> dict:
    return {
        "base_config": {
            "dry_run": False,
            "log_dir": "logs/live",
            "kabu": {
                "api_password": "pw",
                "live_arm_path": "live_arm.txt",
                "register_exchange": 0,
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
        "auto_arm_after_preflight": True,
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
        self.assertEqual(child["kabu"]["register_exchange"], 0)
        self.assertEqual(child["kabu"]["startup_open_order_policy"], "ignore")

    def test_materialize_symbol_config_preserves_trading_exchange(self) -> None:
        child = materialize_symbol_config(_payload()["base_config"], {"symbol": "9984", "exchange": 9})

        self.assertEqual(child["exchange"], 9)
        self.assertEqual(child["kabu"]["register_exchange"], 0)

    def test_materialize_symbol_config_preserves_live_loss_guard_and_400_share_limit(self) -> None:
        payload = _payload()
        payload["base_config"]["risk"].update(
            {"max_inventory_qty": 400, "max_notional": 450000, "prevent_loss_exit": True}
        )
        payload["base_config"]["signals"] = {"kabu_bidask_reversed": True, "auto_fix_negative_spread": True}

        child = materialize_symbol_config(payload["base_config"], {"symbol": "9984", "exchange": 9})

        self.assertEqual(child["risk"]["max_inventory_qty"], 400)
        self.assertEqual(child["risk"]["max_notional"], 450000)
        self.assertTrue(child["risk"]["prevent_loss_exit"])
        self.assertTrue(child["signals"]["kabu_bidask_reversed"])

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

    def test_account_risk_disabled_keeps_legacy_launcher_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload()
            payload["generated_config_dir"] = str(Path(tmp) / "generated")
            payload["account_risk"] = {
                "enabled": False,
                "max_total_inventory_qty": 1,
                "max_total_notional": 1,
            }
            specs = build_child_specs(payload, mode="real", config_path=Path(tmp) / "config.live.multi.json")

            validate_account_risk(payload, specs)

    def test_account_risk_rejects_total_inventory_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload()
            payload["generated_config_dir"] = str(Path(tmp) / "generated")
            payload["account_risk"] = {
                "enabled": True,
                "max_total_inventory_qty": 100,
                "max_total_notional": 0,
            }
            specs = build_child_specs(payload, mode="real", config_path=Path(tmp) / "config.live.multi.json")

            with self.assertRaises(ValueError) as ctx:
                validate_account_risk(payload, specs)

        self.assertIn("max_total_inventory_qty exceeded", str(ctx.exception))

    def test_account_risk_rejects_total_notional_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload()
            payload["generated_config_dir"] = str(Path(tmp) / "generated")
            payload["account_risk"] = {
                "enabled": True,
                "max_total_inventory_qty": 0,
                "max_total_notional": 400000,
            }
            specs = build_child_specs(payload, mode="real", config_path=Path(tmp) / "config.live.multi.json")

            with self.assertRaises(ValueError) as ctx:
                validate_account_risk(payload, specs)

        self.assertIn("max_total_notional exceeded", str(ctx.exception))

    def test_arm_real_workers_creates_and_cleans_temporary_arm_files(self) -> None:
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            try:
                os.chdir(tmp_path)
                payload = _payload()
                payload["generated_config_dir"] = "generated"
                specs = build_child_specs(payload, mode="real", config_path=tmp_path / "config.live.multi.json")

                arm_files = _arm_real_workers(specs)

                self.assertEqual([arm.symbol for arm in arm_files], ["9984", "7203"])
                self.assertTrue(all(arm.created for arm in arm_files))
                self.assertTrue(Path("live_arm_9984.txt").exists())
                self.assertTrue(Path("live_arm_7203.txt").exists())

                _cleanup_launcher_arm_files(arm_files)

                self.assertFalse(Path("live_arm_9984.txt").exists())
                self.assertFalse(Path("live_arm_7203.txt").exists())
            finally:
                os.chdir(old_cwd)

    def test_cleanup_leaves_preexisting_arm_files(self) -> None:
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            try:
                os.chdir(tmp_path)
                Path("live_arm_9984.txt").write_text("manual\n", encoding="utf-8")
                payload = _payload()
                payload["stocks"] = [{"symbol": "9984", "exchange": 27}]
                payload["generated_config_dir"] = "generated"
                specs = build_child_specs(payload, mode="real", config_path=tmp_path / "config.live.multi.json")

                arm_files = _arm_real_workers(specs)
                _cleanup_launcher_arm_files(arm_files)

                self.assertTrue(Path("live_arm_9984.txt").exists())
                self.assertEqual(Path("live_arm_9984.txt").read_text(encoding="utf-8"), "manual\n")
            finally:
                os.chdir(old_cwd)

    def test_prepare_shared_worker_envs_issues_one_token_per_kabu_base_url(self) -> None:
        test_case = self

        class FakeClient:
            token_calls = 0

            def __init__(self, base_url: str, **_kwargs) -> None:
                self.base_url = base_url.rstrip("/").removesuffix("/kabusapi")

            def get_token(self, password: str) -> str:
                test_case.assertEqual(password, "pw")
                FakeClient.token_calls += 1
                return "SHARED-TOKEN"

        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload()
            payload["generated_config_dir"] = str(Path(tmp) / "generated")
            specs = build_child_specs(payload, mode="real", config_path=Path(tmp) / "config.live.multi.json")

            with (
                patch("kabu_maker_taker.launcher._validate_worker_start_gates", return_value=None),
                patch("kabu_maker_taker.launcher.KabuRestClient", FakeClient),
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    envs = _prepare_shared_worker_envs(specs, mode="real")

        self.assertEqual(FakeClient.token_calls, 1)
        self.assertEqual(envs["9984"][SHARED_KABU_TOKEN_ENABLED_ENV], "1")
        self.assertEqual(envs["9984"][SHARED_KABU_TOKEN_ENV], "SHARED-TOKEN")
        self.assertEqual(envs["7203"][SHARED_KABU_TOKEN_ENV], "SHARED-TOKEN")
        output = buffer.getvalue()
        self.assertNotIn("SHARED-TOKEN", output)
        payload = json.loads(output.strip())
        self.assertEqual(payload["status"], "launcher_shared_token_ready")
        self.assertIn("token_sha256_8", payload)

    def test_run_workers_passes_per_symbol_shared_token_env(self) -> None:
        captured_envs: list[dict[str, str] | None] = []

        class FakeProcess:
            def __init__(self, _args, *, env=None) -> None:
                captured_envs.append(env)

            def poll(self) -> int:
                return 0

        specs = [
            ChildRunSpec(symbol="9984", config_path=Path("config.9984.json"), args=("--noop",)),
            ChildRunSpec(symbol="7203", config_path=Path("config.7203.json"), args=("--noop",)),
        ]
        worker_envs = {
            "9984": {SHARED_KABU_TOKEN_ENABLED_ENV: "1", SHARED_KABU_TOKEN_ENV: "TOKEN-1"},
            "7203": {SHARED_KABU_TOKEN_ENABLED_ENV: "1", SHARED_KABU_TOKEN_ENV: "TOKEN-2"},
        }

        with patch("kabu_maker_taker.launcher.subprocess.Popen", FakeProcess):
            code = _run_workers(specs, worker_envs=worker_envs)

        self.assertEqual(code, 0)
        self.assertEqual(captured_envs[0][SHARED_KABU_TOKEN_ENABLED_ENV], "1")
        self.assertEqual(captured_envs[0][SHARED_KABU_TOKEN_ENV], "TOKEN-1")
        self.assertEqual(captured_envs[1][SHARED_KABU_TOKEN_ENV], "TOKEN-2")


if __name__ == "__main__":
    unittest.main()
