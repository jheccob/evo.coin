import unittest
from unittest import mock

import config
from services.live_execution_service import LiveExecutionService
from services.multiuser_runtime_service import MultiUserRuntimeService


class LiveExecutionServiceTests(unittest.TestCase):
    def setUp(self):
        self.database = mock.Mock()
        self.vault = mock.Mock()
        self.vault.is_configured.return_value = True
        self.vault.load_exchange_credentials.return_value = {
            "user_id": 7,
            "account_id": "acct-1",
            "exchange": "binanceusdm",
            "api_key": "k",
            "api_secret": "s",
            "credential_alias": "acct-1-binance",
        }
        self.context = {
            "user_id": 7,
            "account_id": "acct-1",
            "exchange_name": "binanceusdm",
            "risk_profile": {"leverage_cap": 5},
        }
        self.service = LiveExecutionService(database=self.database, credential_vault=self.vault)

    def test_validate_account_connection_success_updates_status(self):
        exchange = mock.Mock()
        exchange.id = "binanceusdm"
        exchange.load_markets.return_value = {"BTC/USDT:USDT": {}}
        exchange.fetch_balance.return_value = {"USDT": {"free": 1000}}

        with mock.patch.object(self.service, "_build_authenticated_exchange", return_value=(exchange, {"credential_alias": "acct"})):
            result = self.service.validate_account_connection(self.context, testnet=True)

        self.assertTrue(result["ok"])
        self.database.update_user_exchange_credential_status.assert_called()
        self.database.save_user_execution_event.assert_called()

    def test_submit_market_order_persists_order_and_reconcile(self):
        exchange = mock.Mock()
        exchange.id = "binanceusdm"
        exchange.load_markets.return_value = {"BTC/USDT:USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT"}}
        exchange.amount_to_precision.return_value = "0.015"
        exchange.create_order.return_value = {
            "id": "12345",
            "clientOrderId": "abc",
            "side": "buy",
            "type": "market",
            "amount": 0.015,
            "average": 71000.0,
            "status": "open",
            "info": {"orderId": "12345", "clientOrderId": "abc"},
        }
        self.database.upsert_user_live_order.return_value = 99
        self.database.save_user_execution_event.return_value = 501

        with mock.patch.object(self.service, "_build_authenticated_exchange", return_value=(exchange, {})):
            with mock.patch.object(self.service, "reconcile_account_state", return_value={"ok": True, "orders_open": 1, "positions_open": 1}):
                result = self.service.submit_market_order(
                    context=self.context,
                    symbol="BTC/USDT",
                    timeframe="15m",
                    strategy_version="test-v1",
                    signal_side="buy",
                    quantity=0.015,
                    testnet=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], 99)
        exchange.create_order.assert_called_once()
        self.database.upsert_user_live_order.assert_called_once()

    def test_reconcile_account_state_syncs_open_orders_and_positions(self):
        exchange = mock.Mock()
        exchange.id = "binanceusdm"
        exchange.has = {"fetchPositions": True}
        exchange.load_markets.return_value = {"BTC/USDT:USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT"}}
        exchange.fetch_open_orders.return_value = [
            {
                "id": "111",
                "clientOrderId": "cli-111",
                "side": "sell",
                "type": "limit",
                "amount": 0.01,
                "price": 72000.0,
                "status": "open",
                "info": {"orderId": "111", "clientOrderId": "cli-111"},
            }
        ]
        exchange.fetch_positions.return_value = [
            {
                "side": "long",
                "contracts": 0.01,
                "entryPrice": 71000.0,
                "markPrice": 71200.0,
                "unrealizedPnl": 2.5,
                "info": {"positionAmt": "0.01", "entryPrice": "71000", "markPrice": "71200"},
            }
        ]
        self.database.sync_user_live_orders_snapshot.return_value = [1]
        self.database.sync_user_live_positions_snapshot.return_value = [2]
        self.database.save_user_execution_event.return_value = 700

        with mock.patch.object(self.service, "_build_authenticated_exchange", return_value=(exchange, {})):
            result = self.service.reconcile_account_state(
                context=self.context,
                symbol="BTC/USDT",
                timeframe="15m",
                strategy_version="test-v1",
                testnet=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["orders_open"], 1)
        self.assertEqual(result["positions_open"], 1)
        self.database.sync_user_live_orders_snapshot.assert_called_once()
        self.database.sync_user_live_positions_snapshot.assert_called_once()

    def test_env_credentials_path_builds_authenticated_exchange(self):
        env_context = {
            "user_id": 0,
            "account_id": "env-primary",
            "account_alias": "Runner",
            "exchange_name": "binanceusdm",
            "credential_source": "env",
        }
        with mock.patch.dict(
            "os.environ",
            {"BINANCE_API_KEY": "env-key", "BINANCE_SECRET_KEY": "env-secret"},
            clear=False,
        ):
            with mock.patch(
                "services.live_execution_service.ExchangeConfig.get_exchange_instance_with_credentials",
                return_value=mock.Mock(id="binanceusdm"),
            ) as exchange_factory:
                exchange, credentials = self.service._build_authenticated_exchange(env_context, testnet=True)

        self.assertEqual(credentials["credential_source"], "env")
        self.assertEqual(credentials["api_key"], "env-key")
        self.assertEqual(credentials["api_secret"], "env-secret")
        self.assertEqual(exchange.id, "binanceusdm")
        exchange_factory.assert_called_once()


class MultiUserRuntimeExecutionTests(unittest.TestCase):
    def test_multiuser_runtime_executes_when_auto_order_is_enabled(self):
        database = mock.Mock()
        risk_service = mock.Mock()
        risk_service.evaluate_risk_engine.return_value = {
            "allowed": True,
            "risk_mode": "normal",
            "risk_amount": 25.0,
            "position_notional": 500.0,
            "quantity": 0.007,
        }
        live_execution_service = mock.Mock()
        live_execution_service.submit_market_order.return_value = {
            "event_id": 12,
            "order_id": 34,
            "exchange_order_id": "ex-1",
            "reconciliation": {"ok": True},
        }
        service = MultiUserRuntimeService(
            database=database,
            risk_management_service=risk_service,
            live_execution_service=live_execution_service,
        )
        context = {
            "user_id": 1,
            "account_id": "acct-1",
            "account_alias": "main",
            "exchange_name": "binanceusdm",
            "live_enabled": True,
            "token_status": "valid",
            "permission_status": "valid",
            "reconciliation_status": "ok",
            "governance_mode": "normal",
            "governance_blocked": False,
            "risk_profile": {"is_valid": True, "leverage_cap": 5},
            "capital_base": 10000.0,
            "allowed_symbols": ["BTC/USDT"],
            "allowed_timeframes": ["15m"],
        }

        with mock.patch.object(config.ProductionConfig, "ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION", True):
            result = service.run_account_cycle(
                context=context,
                symbol="BTC/USDT",
                timeframe="15m",
                strategy_version="test-v1",
                entry_price=71000.0,
                stop_loss_pct=0.8,
                signal_side="buy",
            )

        self.assertEqual(result["status"], "executed")
        live_execution_service.submit_market_order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
