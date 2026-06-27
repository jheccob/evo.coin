import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from database.database import TradingDatabase


class RuntimeControlDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "runtime-controls.sqlite"
        self.env_patcher = mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.database = TradingDatabase(db_path=str(self.db_path))

    def test_runtime_control_roundtrip_and_tracking(self):
        row = self.database.set_user_runtime_control(
            user_id=7,
            account_id="acct-7",
            exchange="binanceusdm",
            symbol="BTC/USDT",
            timeframe="15m",
            desired_state="ligar",
            requested_mode="live",
            requested_by_user_id=7,
            requested_by_scope="workspace",
            requested_reason="initial_start",
        )

        self.assertEqual(row["desired_state"], "running")
        self.assertEqual(row["requested_mode"], "real")
        self.assertEqual(int(row["command_revision"]), 1)

        self.assertTrue(
            self.database.update_user_runtime_control_tracking(
                user_id=7,
                account_id="acct-7",
                exchange="binanceusdm",
                symbol="BTC/USDT",
                timeframe="15m",
                last_start_attempt_at="2026-06-27T00:00:00+00:00",
                last_started_at="2026-06-27T00:00:02+00:00",
                last_error="",
            )
        )

        tracked_row = self.database.get_user_runtime_control(
            user_id=7,
            account_id="acct-7",
            exchange="binanceusdm",
            symbol="BTC/USDT",
            timeframe="15m",
        )
        self.assertEqual(tracked_row["last_start_attempt_at"], "2026-06-27T00:00:00+00:00")
        self.assertEqual(tracked_row["last_started_at"], "2026-06-27T00:00:02+00:00")
        self.assertEqual(tracked_row["last_error"], "")

        stopped_row = self.database.set_user_runtime_control(
            user_id=7,
            account_id="acct-7",
            exchange="binanceusdm",
            symbol="BTC/USDT",
            timeframe="15m",
            desired_state="desligar",
            requested_mode="testnet",
            requested_by_user_id=7,
            requested_by_scope="workspace",
            requested_reason="manual_stop",
        )
        self.assertEqual(stopped_row["desired_state"], "stopped")
        self.assertEqual(stopped_row["requested_mode"], "testnet")
        self.assertEqual(int(stopped_row["command_revision"]), 2)

        stopped_controls = self.database.list_user_runtime_controls(
            user_id=7,
            desired_state="stopped",
            limit=10,
        )
        self.assertEqual(len(stopped_controls), 1)
        self.assertEqual(stopped_controls[0]["account_id"], "acct-7")


if __name__ == "__main__":
    unittest.main()
