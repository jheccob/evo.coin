import unittest
from unittest import mock

import multi_client_runtime


class MultiClientRuntimeReconcileTests(unittest.TestCase):
    def test_reconcile_requests_start_for_running_control(self):
        control_row = {
            "user_id": 11,
            "account_id": "acct-11",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT",
            "timeframe": "15m",
            "desired_state": "running",
            "requested_mode": "testnet",
            "last_error": "",
            "last_start_attempt_at": None,
            "last_command_at": None,
        }
        execution_context = {
            "user_id": 11,
            "account_id": "acct-11",
            "account_alias": "alpha",
            "exchange_name": "binanceusdm",
        }

        db_mock = mock.Mock()
        db_mock.list_user_runtime_controls.return_value = [control_row]
        db_mock.build_account_execution_context.return_value = execution_context

        with mock.patch.object(multi_client_runtime, "db", db_mock), \
            mock.patch.object(multi_client_runtime, "read_runtime_process_state", return_value={}), \
            mock.patch.object(multi_client_runtime, "_is_process_running", return_value=False), \
            mock.patch.object(multi_client_runtime, "_fetch_account_record", return_value={"status": "active", "live_enabled": True}), \
            mock.patch.object(
                multi_client_runtime,
                "_start_account",
                return_value={
                    "runtime_key": "account:11:acct-11:binanceusdm:BTC/USDT:15m",
                    "user_id": 11,
                    "account_id": "acct-11",
                    "status": "started",
                    "pid": 4321,
                },
            ) as start_mock:
            results = multi_client_runtime.reconcile_runtime_controls(retry_cooldown_seconds=1.0)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "start")
        self.assertEqual(results[0]["status"], "started")
        start_mock.assert_called_once_with(
            execution_context,
            symbol="BTC/USDT",
            timeframe="15m",
            testnet=True,
            force=True,
        )
        db_mock.update_user_runtime_control_tracking.assert_any_call(
            user_id=11,
            account_id="acct-11",
            exchange="binanceusdm",
            symbol="BTC/USDT",
            timeframe="15m",
            last_start_attempt_at=mock.ANY,
        )
        db_mock.update_user_runtime_control_tracking.assert_any_call(
            user_id=11,
            account_id="acct-11",
            exchange="binanceusdm",
            symbol="BTC/USDT",
            timeframe="15m",
            last_started_at=mock.ANY,
            last_error="",
        )

    def test_reconcile_starts_env_single_user_control_without_account_row(self):
        control_row = {
            "user_id": 0,
            "account_id": "railway-primary-real",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT",
            "timeframe": "15m",
            "desired_state": "running",
            "requested_mode": "real",
            "last_error": "",
            "last_start_attempt_at": None,
            "last_command_at": None,
        }

        db_mock = mock.Mock()
        db_mock.list_user_runtime_controls.return_value = [control_row]

        with mock.patch.object(multi_client_runtime, "db", db_mock), \
            mock.patch.object(multi_client_runtime.config, "SINGLE_USER_RUNTIME_USER_ID", 0), \
            mock.patch.object(multi_client_runtime.config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "railway-primary"), \
            mock.patch.object(multi_client_runtime.config, "SINGLE_USER_RUNTIME_ACCOUNT_ALIAS", "Railway Primary"), \
            mock.patch.object(multi_client_runtime.config, "SINGLE_USER_RUNTIME_EXCHANGE", "binanceusdm"), \
            mock.patch.object(multi_client_runtime, "read_runtime_process_state", return_value={}), \
            mock.patch.object(multi_client_runtime, "_is_process_running", return_value=False), \
            mock.patch.object(multi_client_runtime, "_fetch_account_record", return_value=None), \
            mock.patch.object(
                multi_client_runtime,
                "_start_account",
                return_value={
                    "runtime_key": "account:0:railway-primary-real:binanceusdm:BTC/USDT:15m",
                    "user_id": 0,
                    "account_id": "railway-primary-real",
                    "status": "started",
                    "pid": 4321,
                },
            ) as start_mock:
            results = multi_client_runtime.reconcile_runtime_controls(retry_cooldown_seconds=1.0)

        self.assertEqual(results[0]["action"], "start")
        start_context = start_mock.call_args.args[0]
        self.assertTrue(start_context["use_env_credentials"])
        self.assertEqual(start_context["credential_source"], "env")
        self.assertEqual(start_context["account_id"], "railway-primary-real")
        db_mock.build_account_execution_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
