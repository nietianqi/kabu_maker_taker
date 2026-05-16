from __future__ import annotations

import unittest

from kabu_maker_taker.app import _handle_live_execution
from kabu_maker_taker.execution import KabuApiError, KabuRestClient, KabuRestExecutor, order_snapshot
from kabu_maker_taker.kabu_rest import KabuApiError as LegacyKabuApiError
from kabu_maker_taker.kabu_rest import KabuRestClient as LegacyKabuRestClient
from kabu_maker_taker.kabu_rest import KabuRestExecutor as LegacyKabuRestExecutor


class ExecutionStructureTests(unittest.TestCase):
    def test_new_execution_package_exports_live_surface(self) -> None:
        self.assertIs(KabuApiError, LegacyKabuApiError)
        self.assertIs(KabuRestClient, LegacyKabuRestClient)
        self.assertIs(KabuRestExecutor, LegacyKabuRestExecutor)
        self.assertTrue(callable(order_snapshot))

    def test_app_keeps_live_runtime_compat_symbol(self) -> None:
        self.assertTrue(callable(_handle_live_execution))


if __name__ == "__main__":
    unittest.main()

