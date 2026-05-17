from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from kabu_maker_taker.app import _handle_live_execution
from kabu_maker_taker.combined import CombinedMakerTakerStrategy
from kabu_maker_taker.config import AppConfig, KabuConfig, OrderProfile, RiskConfig, StrategyConfig
from kabu_maker_taker.kabu_rest import (
    KabuApiError,
    KabuRestClient,
    KabuRestExecutor,
    order_snapshot,
)
from kabu_maker_taker.models import BrokerFillEvent, BrokerOrderEvent, OrderIntent, OrderState, OrderStatus


def _intent(
    *,
    is_market: bool = False,
    side: int = 1,
    client_order_id: str = "entry-1",
    reference_price: float = 1000.0,
    max_slip_ticks: float = 0.0,
) -> OrderIntent:
    return OrderIntent(
        symbol="9984",
        exchange=27,
        side=side,
        qty=100,
        price=1000.0,
        is_market=is_market,
        strategy="taker" if is_market else "maker",
        reason="test",
        score=1,
        reference_price=reference_price,
        max_slip_ticks=max_slip_ticks,
        client_order_id=client_order_id,
    )


class KabuConfigTests(unittest.TestCase):
    def test_kabu_config_defaults_to_margin_profile(self) -> None:
        config = AppConfig.from_dict({"symbol": "9984"})

        self.assertTrue(config.dry_run)
        self.assertEqual(config.kabu.base_url, "http://localhost:18080")
        self.assertEqual(config.kabu.order_profile.mode, "margin")
        self.assertEqual(config.kabu.order_profile.account_type, 4)

    def test_kabu_config_parses_profile_overrides(self) -> None:
        config = AppConfig.from_dict(
            {
                "dry_run": False,
                "kabu": {
                    "base_url": "http://localhost:18081/kabusapi",
                    "api_password": "secret",
                    "poll_interval_ms": 0,
                    "order_profile": {
                        "mode": "cash",
                        "front_order_type_market": 120,
                        "front_order_type_ioc_limit": 127,
                    },
                },
            }
        )

        self.assertFalse(config.dry_run)
        self.assertEqual(config.kabu.base_url, "http://localhost:18081/kabusapi")
        self.assertEqual(config.kabu.api_password, "secret")
        self.assertEqual(config.kabu.order_profile.mode, "cash")
        self.assertEqual(config.kabu.order_profile.front_order_type_market, 120)
        self.assertEqual(config.kabu.order_profile.front_order_type_ioc_limit, 127)

    def test_order_profile_defaults_ioc_limit_type(self) -> None:
        config = AppConfig.from_dict({"symbol": "9984"})

        self.assertEqual(config.kabu.order_profile.front_order_type_ioc_limit, 27)


class KabuRestClientRequestTests(unittest.TestCase):
    def test_base_url_accepts_kabusapi_suffix(self) -> None:
        client = KabuRestClient("http://localhost:18080/kabusapi")

        self.assertEqual(client.base_url, "http://localhost:18080")

    def test_margin_entry_market_order_body(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(method: str, path: str, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json_body"] = kwargs["json_body"]
            return {"OrderId": "B-ENTRY"}

        client = KabuRestClient("http://localhost:18080")
        client._password = "pw"  # type: ignore[attr-defined]
        client._request_json = fake_request  # type: ignore[method-assign]

        client.send_entry_order(
            symbol="9984",
            exchange=27,
            side=1,
            qty=100,
            price=1000.0,
            is_market=True,
            profile=OrderProfile(),
        )

        body = captured["json_body"]
        assert isinstance(body, dict)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/kabusapi/sendorder")
        self.assertEqual(body["Password"], "pw")
        self.assertEqual(body["Exchange"], 9)
        self.assertEqual(body["Side"], "2")
        self.assertEqual(body["CashMargin"], 2)
        self.assertEqual(body["Price"], 0)
        self.assertEqual(body["FrontOrderType"], 10)

    def test_margin_entry_ioc_limit_order_body(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(method: str, path: str, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json_body"] = kwargs["json_body"]
            return {"OrderId": "B-ENTRY"}

        client = KabuRestClient("http://localhost:18080")
        client._password = "pw"  # type: ignore[attr-defined]
        client._request_json = fake_request  # type: ignore[method-assign]

        client.send_entry_order(
            symbol="9984",
            exchange=27,
            side=1,
            qty=100,
            price=1002.0,
            is_market=False,
            profile=OrderProfile(front_order_type_ioc_limit=27),
            front_order_type=27,
        )

        body = captured["json_body"]
        assert isinstance(body, dict)
        self.assertEqual(body["Price"], 1002.0)
        self.assertEqual(body["FrontOrderType"], 27)

    def test_margin_exit_uses_close_positions_and_reverse_side(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(_method: str, _path: str, **kwargs):
            captured["json_body"] = kwargs["json_body"]
            return {"OrderId": "B-EXIT"}

        def fake_positions(symbol=None, product=2, lane="poll"):
            self.assertEqual(symbol, "9984")
            self.assertEqual(product, 2)
            self.assertEqual(lane, "order")
            return [
                {
                    "HoldID": "HOLD-1",
                    "Symbol": "9984",
                    "Exchange": 27,
                    "Side": "2",
                    "LeavesQty": 100,
                    "HoldQty": 0,
                    "Price": 1000.0,
                    "MarginTradeType": 1,
                }
            ]

        client = KabuRestClient("http://localhost:18080")
        client._password = "pw"  # type: ignore[attr-defined]
        client._request_json = fake_request  # type: ignore[method-assign]
        client.get_positions = fake_positions  # type: ignore[method-assign]

        client.send_exit_order(
            symbol="9984",
            exchange=27,
            position_side=1,
            qty=100,
            price=1002.0,
            is_market=False,
            profile=OrderProfile(),
        )

        body = captured["json_body"]
        assert isinstance(body, dict)
        self.assertEqual(body["Exchange"], 9)
        self.assertEqual(body["Side"], "1")
        self.assertEqual(body["CashMargin"], 3)
        self.assertEqual(body["ClosePositions"], [{"HoldID": "HOLD-1", "Qty": 100}])


class KabuRestExecutorTests(unittest.TestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.sent_entry = False

        def get_token(self, password: str) -> str:
            if not password:
                raise KabuApiError("missing password")
            return "TOKEN"

        def send_entry_order(self, **_kwargs):
            self.sent_entry = True
            return {"OrderId": "B-ENTRY"}

        def send_exit_order(self, **_kwargs):
            return {"OrderId": "B-EXIT"}

        def cancel_order(self, _order_id: str):
            return {"ResultCode": 0}

        def get_orders(self, order_id=None, product=0, lane="poll"):
            _ = (product, lane)
            if order_id == "B-ENTRY":
                return [
                    {
                        "ID": "B-ENTRY",
                        "State": 3,
                        "OrderState": 3,
                        "Side": "2",
                        "OrderQty": 100,
                        "CumQty": 50,
                        "Price": 1000.0,
                        "Details": [
                            {
                                "RecType": 8,
                                "Qty": 50,
                                "Price": 1001.0,
                                "ExecutionID": "E1",
                                "ExecutionDay": "2026-03-13T10:00:00+09:00",
                            }
                        ],
                    }
                ]
            return []

        def get_positions(self, symbol=None, product=2, lane="poll"):
            _ = (symbol, product, lane)
            return []

    def test_submit_entry_returns_working_ack(self) -> None:
        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        client = self.FakeClient()
        executor = KabuRestExecutor(config, client=client)  # type: ignore[arg-type]

        result = executor.submit(_intent(), role="entry", now_ns=123)
        events = result.events

        self.assertTrue(client.sent_entry)
        self.assertTrue(result.api_success)
        self.assertFalse(result.api_error)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, OrderStatus.WORKING)
        self.assertEqual(events[0].broker_order_id, "B-ENTRY")
        self.assertEqual(result.request_kind, "submit")
        self.assertGreaterEqual(result.latency_ms, 0.0)

    def test_submit_taker_entry_maps_to_ioc_limit(self) -> None:
        class CapturingClient(self.FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.kwargs = {}

            def send_entry_order(self, **kwargs):
                self.kwargs = kwargs
                return {"OrderId": "B-ENTRY"}

        config = AppConfig(
            tick_size=1.0,
            strategy=StrategyConfig(max_slip_ticks=1.0),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        client = CapturingClient()
        executor = KabuRestExecutor(config, client=client)  # type: ignore[arg-type]

        result = executor.submit(
            _intent(is_market=True, reference_price=1000.0, max_slip_ticks=2.0),
            role="entry",
            now_ns=123,
        )

        self.assertTrue(result.api_success)
        self.assertEqual(client.kwargs["front_order_type"], 27)
        self.assertFalse(client.kwargs["is_market"])
        self.assertEqual(client.kwargs["price"], 1002.0)

    def test_submit_force_exit_maps_to_ioc_limit_sell(self) -> None:
        class CapturingClient(self.FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.kwargs = {}

            def send_exit_order(self, **kwargs):
                self.kwargs = kwargs
                return {"OrderId": "B-EXIT"}

        config = AppConfig(
            tick_size=1.0,
            strategy=StrategyConfig(max_slip_ticks=1.0),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        client = CapturingClient()
        executor = KabuRestExecutor(config, client=client)  # type: ignore[arg-type]

        result = executor.submit(
            _intent(is_market=True, side=-1, reference_price=1000.0, max_slip_ticks=2.0),
            role="exit",
            now_ns=123,
        )

        self.assertTrue(result.api_success)
        self.assertEqual(client.kwargs["front_order_type"], 27)
        self.assertFalse(client.kwargs["is_market"])
        self.assertEqual(client.kwargs["position_side"], 1)
        self.assertEqual(client.kwargs["price"], 998.0)

    def test_submit_taker_uses_config_slip_when_intent_slip_zero(self) -> None:
        class CapturingClient(self.FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.kwargs = {}

            def send_entry_order(self, **kwargs):
                self.kwargs = kwargs
                return {"OrderId": "B-ENTRY"}

        config = AppConfig(
            tick_size=0.5,
            strategy=StrategyConfig(max_slip_ticks=3.0),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        client = CapturingClient()
        executor = KabuRestExecutor(config, client=client)  # type: ignore[arg-type]

        result = executor.submit(
            _intent(is_market=True, reference_price=1000.0, max_slip_ticks=0.0),
            role="entry",
            now_ns=123,
        )

        self.assertTrue(result.api_success)
        self.assertEqual(client.kwargs["price"], 1001.5)

    def test_submit_taker_missing_reference_rejects_locally(self) -> None:
        client = self.FakeClient()
        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=client)  # type: ignore[arg-type]

        result = executor.submit(
            _intent(is_market=True, reference_price=0.0, max_slip_ticks=1.0),
            role="entry",
            now_ns=123,
        )
        event = result.events[0]

        self.assertFalse(client.sent_entry)
        self.assertEqual(result.halt_reason, "local_reject")
        self.assertEqual(event.status, OrderStatus.REJECTED)
        self.assertIn("reference_price", event.reason)

    def test_submit_api_error_returns_unknown_not_rejected(self) -> None:
        class FailingClient(self.FakeClient):
            def send_entry_order(self, **_kwargs):
                raise KabuApiError("send failed")

        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=FailingClient())  # type: ignore[arg-type]

        result = executor.submit(_intent(), role="entry", now_ns=123)
        event = result.events[0]

        self.assertTrue(result.api_error)
        self.assertEqual(result.halt_reason, "submit_unknown")
        self.assertEqual(event.status, OrderStatus.UNKNOWN)
        self.assertIn("send failed", event.reason)

    def test_submit_success_missing_order_id_returns_unknown_and_halts(self) -> None:
        class MissingOrderIdClient(self.FakeClient):
            def send_entry_order(self, **_kwargs):
                return {"ResultCode": 0}

        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=MissingOrderIdClient())  # type: ignore[arg-type]

        result = executor.submit(_intent(), role="entry", now_ns=123)
        event = result.events[0]

        self.assertTrue(result.api_error)
        self.assertEqual(result.halt_reason, "submit_unknown")
        self.assertEqual(event.status, OrderStatus.UNKNOWN)
        self.assertIn("missing OrderId", event.reason)

    def test_submit_local_parameter_error_rejects_and_halts(self) -> None:
        class LocalRejectClient(self.FakeClient):
            def send_entry_order(self, **_kwargs):
                raise ValueError("bad local params")

        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=LocalRejectClient())  # type: ignore[arg-type]

        result = executor.submit(_intent(), role="entry", now_ns=123)
        event = result.events[0]

        self.assertFalse(result.api_error)
        self.assertEqual(result.halt_reason, "local_reject")
        self.assertEqual(event.status, OrderStatus.REJECTED)
        self.assertEqual(event.reason, "bad local params")

    def test_cancel_api_error_returns_unknown_and_halts(self) -> None:
        class CancelFailClient(self.FakeClient):
            def cancel_order(self, _order_id: str):
                raise KabuApiError("cancel failed")

        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=CancelFailClient())  # type: ignore[arg-type]
        order = OrderState(
            client_order_id="entry-1",
            intent=_intent(),
            role="entry",
            broker_order_id="B-ENTRY",
        )

        result = executor.cancel(order, now_ns=123)
        event = result.events[0]

        self.assertTrue(result.api_error)
        self.assertEqual(result.halt_reason, "cancel_unknown")
        self.assertEqual(event.status, OrderStatus.UNKNOWN)
        self.assertIn("cancel failed", event.reason)
        self.assertEqual(result.request_kind, "cancel")
        self.assertGreaterEqual(result.latency_ms, 0.0)

    def test_poll_active_order_missing_broker_id_halts(self) -> None:
        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=self.FakeClient())  # type: ignore[arg-type]
        order = OrderState(client_order_id="entry-1", intent=_intent(), role="entry")

        result = executor.poll_order_events([order], now_ns=456)
        event = result.events[0]

        self.assertEqual(result.halt_reason, "missing_broker_order_id")
        self.assertEqual(event.status, OrderStatus.UNKNOWN)
        self.assertEqual(event.reason, "missing_broker_order_id")

    def test_poll_api_error_opens_circuit_in_live_handler(self) -> None:
        class PollFailClient(self.FakeClient):
            def get_orders(self, order_id=None, product=0, lane="poll"):
                _ = (order_id, product, lane)
                raise KabuApiError("poll failed")

        config = AppConfig(
            risk=RiskConfig(api_error_limit=1, max_spread_ticks=5.0),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        executor = KabuRestExecutor(config, client=PollFailClient())  # type: ignore[arg-type]
        strategy = CombinedMakerTakerStrategy(config)
        order = OrderState(
            client_order_id="entry-1",
            intent=_intent(),
            role="entry",
            broker_order_id="B-ENTRY",
        )

        result = executor.poll_order_events([order], now_ns=456)
        halt_reason = _handle_live_execution(strategy, result, now_ns=456)

        self.assertTrue(result.api_error)
        self.assertEqual(result.halt_reason, "")
        self.assertEqual(halt_reason, "api_circuit_open")
        self.assertEqual(result.request_kind, "poll")

    def test_poll_order_events_emits_fill_and_cumulative_status(self) -> None:
        config = AppConfig(kabu=KabuConfig(api_password="pw", poll_interval_ms=0))
        executor = KabuRestExecutor(config, client=self.FakeClient())  # type: ignore[arg-type]
        order = OrderState(
            client_order_id="entry-1",
            intent=_intent(),
            role="entry",
            broker_order_id="B-ENTRY",
        )

        result = executor.poll_order_events([order], now_ns=456)
        events = result.events

        self.assertTrue(result.api_success)
        self.assertFalse(result.api_error)
        self.assertIsInstance(events[0], BrokerFillEvent)
        self.assertIsInstance(events[1], BrokerOrderEvent)
        fill = events[0]
        assert isinstance(fill, BrokerFillEvent)
        self.assertEqual(fill.trade_id, "E1")
        self.assertEqual(fill.qty, 50)
        status = events[1]
        assert isinstance(status, BrokerOrderEvent)
        self.assertEqual(status.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(status.cum_qty, 50)
        self.assertEqual(result.request_kind, "poll")
        self.assertGreaterEqual(result.latency_ms, 0.0)

    def test_slow_submit_latency_is_recorded_without_hiding_ack(self) -> None:
        class SlowSubmitClient(self.FakeClient):
            def send_entry_order(self, **_kwargs):
                time.sleep(0.005)
                return {"OrderId": "B-ENTRY"}

        config = AppConfig(
            risk=RiskConfig(
                api_error_limit=5,
                order_latency_limit_ms=1,
                latency_breach_limit=2,
            ),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        executor = KabuRestExecutor(config, client=SlowSubmitClient())  # type: ignore[arg-type]
        strategy = CombinedMakerTakerStrategy(config)

        first = executor.submit(_intent(), role="entry", now_ns=100)
        first_halt = _handle_live_execution(strategy, first, now_ns=100)
        second = executor.submit(_intent(client_order_id="entry-2"), role="entry", now_ns=101)
        second_halt = _handle_live_execution(strategy, second, now_ns=101)

        self.assertTrue(first.api_success)
        self.assertEqual(first.events[0].status, OrderStatus.WORKING)
        self.assertEqual(first.request_kind, "submit")
        self.assertGreater(first.latency_ms, 1.0)
        self.assertEqual(first_halt, "")
        self.assertEqual(second_halt, "latency_circuit_open")
        self.assertEqual(strategy.metrics.latency_circuit_opens, 1)
        self.assertEqual(strategy.metrics.to_dict()["submit_latency_ms_count"], 2)

    def test_slow_cancel_and_poll_latency_are_recorded_without_hiding_results(self) -> None:
        class SlowClient(self.FakeClient):
            def cancel_order(self, _order_id: str):
                time.sleep(0.005)
                return {"ResultCode": 0}

            def get_orders(self, order_id=None, product=0, lane="poll"):
                time.sleep(0.005)
                return super().get_orders(order_id=order_id, product=product, lane=lane)

        config = AppConfig(
            risk=RiskConfig(
                api_error_limit=5,
                cancel_latency_limit_ms=1,
                poll_latency_limit_ms=1,
                latency_breach_limit=3,
            ),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        executor = KabuRestExecutor(config, client=SlowClient())  # type: ignore[arg-type]
        strategy = CombinedMakerTakerStrategy(config)
        order = OrderState(
            client_order_id="entry-1",
            intent=_intent(),
            role="entry",
            broker_order_id="B-ENTRY",
        )

        cancel_result = executor.cancel(order, now_ns=200)
        cancel_halt = _handle_live_execution(strategy, cancel_result, now_ns=200)
        poll_result = executor.poll_order_events([order], now_ns=201)
        poll_halt = _handle_live_execution(strategy, poll_result, now_ns=201)

        self.assertTrue(cancel_result.api_success)
        self.assertEqual(cancel_result.events[0].status, OrderStatus.CANCEL_PENDING)
        self.assertEqual(cancel_result.request_kind, "cancel")
        self.assertGreater(cancel_result.latency_ms, 1.0)
        self.assertEqual(cancel_halt, "")
        self.assertTrue(poll_result.api_success)
        self.assertEqual(poll_result.request_kind, "poll")
        self.assertGreater(poll_result.latency_ms, 1.0)
        self.assertEqual(poll_halt, "")
        metrics = strategy.metrics.to_dict()
        self.assertEqual(metrics["cancel_latency_ms_count"], 1)
        self.assertEqual(metrics["poll_latency_ms_count"], 1)

    def test_slow_api_error_records_both_api_and_latency_circuits_independently(self) -> None:
        class SlowPollFailClient(self.FakeClient):
            def get_orders(self, order_id=None, product=0, lane="poll"):
                _ = (order_id, product, lane)
                time.sleep(0.005)
                raise KabuApiError("poll failed")

        config = AppConfig(
            risk=RiskConfig(
                api_error_limit=1,
                poll_latency_limit_ms=1,
                latency_breach_limit=1,
            ),
            kabu=KabuConfig(api_password="pw", poll_interval_ms=0),
        )
        executor = KabuRestExecutor(config, client=SlowPollFailClient())  # type: ignore[arg-type]
        strategy = CombinedMakerTakerStrategy(config)
        order = OrderState(
            client_order_id="entry-1",
            intent=_intent(),
            role="entry",
            broker_order_id="B-ENTRY",
        )

        result = executor.poll_order_events([order], now_ns=456)
        halt_reason = _handle_live_execution(strategy, result, now_ns=456)

        self.assertTrue(result.api_error)
        self.assertEqual(result.request_kind, "poll")
        self.assertGreater(result.latency_ms, 1.0)
        self.assertEqual(halt_reason, "latency_circuit_open")
        self.assertEqual(strategy.metrics.api_circuit_opens, 1)
        self.assertEqual(strategy.metrics.latency_circuit_opens, 1)

    def test_order_snapshot_treats_rectype_3_as_canceled_not_fill(self) -> None:
        snapshot = order_snapshot(
            {
                "ID": "B-CANCEL",
                "State": 5,
                "OrderState": 5,
                "Side": "2",
                "OrderQty": 100,
                "CumQty": 0,
                "Price": 1000.0,
                "Details": [
                    {
                        "RecType": 3,
                        "Qty": 100,
                        "Price": 1001.0,
                        "ExecutionID": "E-SHOULD-NOT-FILL",
                    }
                ],
            }
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.status, OrderStatus.CANCELED)
        self.assertEqual(snapshot.fills, ())

    def test_order_snapshot_maps_final_failure_detail_to_rejected(self) -> None:
        snapshot = order_snapshot(
            {
                "ID": "B-REJECT",
                "State": 5,
                "OrderState": 5,
                "Side": "2",
                "OrderQty": 100,
                "CumQty": 0,
                "Price": 1000.0,
                "Details": [
                    {
                        "RecType": 7,
                        "State": 4,
                        "Qty": 100,
                        "Price": 1000.0,
                        "ExecutionID": "E-SHOULD-NOT-FILL",
                    }
                ],
            }
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.status, OrderStatus.REJECTED)
        self.assertEqual(snapshot.fills, ())


class KabuLiveCliSafetyTests(unittest.TestCase):
    @staticmethod
    def _live_safe_config(**overrides) -> dict:
        config = {
            "dry_run": False,
            "log_dir": str(Path(tempfile.gettempdir()) / "kabu_maker_taker_test_logs"),
            "enable_journal": True,
            "enable_decision_trace": True,
            "market_state": {"enabled": True},
            "risk": {
                "enforce_session": True,
                "daily_loss_limit": 10_000,
                "max_entry_orders_per_minute": 5,
                "max_cancel_requests_per_minute": 10,
                "stale_quote_ms": 2_000,
                "stale_board_ms": 5_000,
                "api_error_limit": 1,
                "max_inventory_qty": 300,
                "max_notional": 3_000_000,
                "max_spread_ticks": 5.0,
                "latency_breach_limit": 3,
                "order_latency_limit_ms": 3000,
                "cancel_latency_limit_ms": 3000,
                "poll_latency_limit_ms": 3000,
            },
            "kabu": {"api_password": "pw"},
        }
        config.update(overrides)
        return config

    def test_live_sample_is_rejected_before_any_network_call(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "kabu_maker_taker.app",
                "--config",
                "config.example.json",
                "--sample",
                "--live",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--live cannot be used with --sample", completed.stderr)

    def test_live_requires_dry_run_false(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "kabu_maker_taker.app", "--config", "config.example.json", "--live"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--live requires config.dry_run=false", completed.stderr)

    def test_live_requires_api_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp).joinpath("config.json")
            config = self._live_safe_config(kabu={"api_password": ""})
            config_path.write_text(json.dumps(config), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kabu_maker_taker.app",
                    "--config",
                    str(config_path),
                    "--live",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("kabu.api_password is required", completed.stdout)

    def test_live_requires_enabled_api_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp).joinpath("config.json")
            config = self._live_safe_config()
            config["risk"]["api_error_limit"] = 0
            config_path.write_text(json.dumps(config), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kabu_maker_taker.app",
                    "--config",
                    str(config_path),
                    "--live",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--live safety config incomplete", completed.stderr)
        self.assertIn("risk.api_error_limit>0", completed.stderr)

    def test_live_safety_validator_reports_multiple_missing_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp).joinpath("config.json")
            config_path.write_text(
                json.dumps({"dry_run": False, "risk": {"api_error_limit": 1}, "kabu": {"api_password": "pw"}}),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-m", "kabu_maker_taker.app", "--config", str(config_path), "--live"],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("risk.enforce_session=true", completed.stderr)
        self.assertIn("enable_journal=true", completed.stderr)
        self.assertIn("market_state.enabled=true", completed.stderr)

    def test_live_events_reject_stale_timestamp_before_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            events_path = tmp_path / "events.jsonl"
            config_path.write_text(json.dumps(self._live_safe_config()), encoding="utf-8")
            events_path.write_text(
                json.dumps({"type": "board", "symbol": "9984", "ts_ns": 1, "bid": 100, "ask": 101}) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kabu_maker_taker.app",
                    "--config",
                    str(config_path),
                    "--events",
                    str(events_path),
                    "--live",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--live --events requires fresh events", completed.stderr)
        self.assertIn("stale event ts_ns", completed.stderr)

    def test_live_events_reject_future_timestamp_before_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            events_path = tmp_path / "events.jsonl"
            config_path.write_text(json.dumps(self._live_safe_config()), encoding="utf-8")
            future_ns = time.time_ns() + 60_000_000_000
            events_path.write_text(
                json.dumps({"type": "trade", "symbol": "9984", "ts_ns": future_ns, "price": 100, "size": 100, "side": 1}) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kabu_maker_taker.app",
                    "--config",
                    str(config_path),
                    "--events",
                    str(events_path),
                    "--live",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("future event ts_ns", completed.stderr)

    def test_live_events_reject_missing_timestamp_before_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            events_path = tmp_path / "events.jsonl"
            config_path.write_text(json.dumps(self._live_safe_config()), encoding="utf-8")
            events_path.write_text(
                json.dumps({"type": "board", "symbol": "9984", "bid": 100, "ask": 101}) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kabu_maker_taker.app",
                    "--config",
                    str(config_path),
                    "--events",
                    str(events_path),
                    "--live",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("missing or invalid ts_ns", completed.stderr)


if __name__ == "__main__":
    unittest.main()
