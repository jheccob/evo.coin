import unittest
from unittest import mock

import config
from services.risk_management_service import RiskManagementService
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

    def test_submit_market_order_blocks_zero_after_exchange_precision(self):
        exchange = mock.Mock()
        exchange.id = "binanceusdm"
        exchange.load_markets.return_value = {"BTC/USDT:USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT"}}
        exchange.amount_to_precision.return_value = "0.000"
        self.database.save_user_execution_event.return_value = 501

        with mock.patch.object(self.service, "_build_authenticated_exchange", return_value=(exchange, {})):
            with self.assertRaises(ValueError) as ctx:
                self.service.submit_market_order(
                    context=self.context,
                    symbol="BTC/USDT",
                    timeframe="15m",
                    strategy_version="test-v1",
                    signal_side="buy",
                    quantity=0.0004,
                    testnet=True,
                )

        self.assertIn("apos arredondamento", str(ctx.exception))
        exchange.create_order.assert_not_called()

    def test_replace_stop_market_order_cancels_before_submitting_new_stop(self):
        call_order = []

        with (
            mock.patch.object(
                self.service,
                "cancel_order",
                side_effect=lambda **kwargs: call_order.append("cancel_previous") or {"ok": True},
            ) as cancel_order,
            mock.patch.object(
                self.service,
                "cancel_open_stop_market_orders",
                side_effect=lambda **kwargs: call_order.append("sweep_stale") or {"ok": True, "cancelled": 0},
            ) as sweep_orders,
            mock.patch.object(
                self.service,
                "submit_stop_market_order",
                side_effect=lambda **kwargs: call_order.append("submit_new") or {"ok": True, "exchange_order_id": "stop-2"},
            ) as submit_stop,
        ):
            result = self.service.replace_stop_market_order(
                context=self.context,
                symbol="BTC/USDT",
                side="sell",
                stop_price=61000.0,
                quantity=0.001,
                previous_order_id="stop-1",
                testnet=True,
            )

        self.assertEqual(result["exchange_order_id"], "stop-2")
        self.assertEqual(call_order, ["cancel_previous", "sweep_stale", "submit_new"])
        cancel_order.assert_called_once()
        sweep_orders.assert_called_once()
        submit_stop.assert_called_once()

    def test_replace_stop_market_order_aborts_when_previous_cancel_fails(self):
        with (
            mock.patch.object(self.service, "cancel_order", side_effect=RuntimeError("network timeout")),
            mock.patch.object(self.service, "cancel_open_stop_market_orders") as sweep_orders,
            mock.patch.object(self.service, "submit_stop_market_order") as submit_stop,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.service.replace_stop_market_order(
                    context=self.context,
                    symbol="BTC/USDT",
                    side="sell",
                    stop_price=61000.0,
                    quantity=0.001,
                    previous_order_id="stop-1",
                    testnet=True,
                )

        self.assertIn("Substituicao de stop abortada", str(ctx.exception))
        sweep_orders.assert_not_called()
        submit_stop.assert_not_called()

    def test_replace_stop_market_order_replaces_when_previous_order_is_stale(self):
        call_order = []

        with (
            mock.patch.object(
                self.service,
                "cancel_order",
                side_effect=RuntimeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'),
            ),
            mock.patch.object(
                self.service,
                "cancel_open_stop_market_orders",
                side_effect=lambda **kwargs: call_order.append("sweep_stale") or {"ok": True, "cancelled": 0},
            ),
            mock.patch.object(
                self.service,
                "submit_stop_market_order",
                side_effect=lambda **kwargs: call_order.append("submit_new") or {"ok": True, "exchange_order_id": "stop-2"},
            ),
        ):
            result = self.service.replace_stop_market_order(
                context=self.context,
                symbol="BTC/USDT",
                side="sell",
                stop_price=61000.0,
                quantity=0.001,
                previous_order_id="missing-stop",
                testnet=True,
            )

        self.assertEqual(result["exchange_order_id"], "stop-2")
        self.assertEqual(result["previous_order_id"], "missing-stop")
        self.assertIn("Unknown order", result["previous_cancel_error"])
        self.assertEqual(call_order, ["sweep_stale", "submit_new"])

    def test_replace_stop_market_order_blocks_unknown_previous_when_sweep_unavailable(self):
        call_order = []

        with (
            mock.patch.object(
                self.service,
                "cancel_order",
                side_effect=RuntimeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'),
            ),
            mock.patch.object(
                self.service,
                "cancel_open_stop_market_orders",
                side_effect=lambda **kwargs: call_order.append("sweep_stale")
                or {"ok": True, "cancelled": 0, "skipped": "fetch_open_orders_unavailable"},
            ),
            mock.patch.object(
                self.service,
                "submit_stop_market_order",
                side_effect=lambda **kwargs: call_order.append("submit_new") or {"ok": True, "exchange_order_id": "stop-2"},
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.service.replace_stop_market_order(
                    context=self.context,
                    symbol="BTC/USDT",
                    side="sell",
                    stop_price=61000.0,
                    quantity=0.001,
                    previous_order_id="missing-stop",
                    testnet=True,
                )

        self.assertIn("Nenhum novo stop foi enviado", str(ctx.exception))
        self.assertEqual(call_order, ["sweep_stale"])

    def test_exchange_request_params_are_exchange_specific(self):
        binance_params = self.service._exchange_request_params(
            {"clientOrderId": "abc-1"},
            exchange_name="binanceusdm",
        )
        bybit_params = self.service._exchange_request_params(
            {"clientOrderId": "abc-2"},
            exchange_name="bybit",
        )

        self.assertEqual(binance_params["newClientOrderId"], "abc-1")
        self.assertIn("recvWindow", binance_params)
        self.assertEqual(bybit_params["clientOrderId"], "abc-2")
        self.assertNotIn("recvWindow", bybit_params)
        self.assertNotIn("newClientOrderId", bybit_params)

    def test_bybit_stop_market_uses_ccxt_stop_market_type(self):
        bybit_context = {**self.context, "exchange_name": "bybit", "exchange": "bybit"}
        with mock.patch.object(
            self.service,
            "_submit_conditional_market_order",
            return_value={"ok": True, "exchange_order_id": "bybit-stop"},
        ) as submit_conditional:
            result = self.service.submit_stop_market_order(
                context=bybit_context,
                symbol="BTC/USDT",
                side="sell",
                stop_price=61000.0,
                quantity=0.001,
                testnet=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(submit_conditional.call_args.kwargs["order_type"], "stopMarket")

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

    def test_reconcile_account_state_ignores_flat_position_amt_even_with_contract_size(self):
        exchange = mock.Mock()
        exchange.id = "binanceusdm"
        exchange.has = {"fetchPositions": True}
        exchange.load_markets.return_value = {"BTC/USDT:USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT"}}
        exchange.fetch_open_orders.return_value = []
        exchange.fetch_positions.return_value = [
            {
                "side": "long",
                "contracts": 0,
                "contractSize": 1,
                "entryPrice": 0.0,
                "markPrice": 0.0,
                "unrealizedPnl": 0.0,
                "info": {"positionAmt": "0", "entryPrice": "0", "markPrice": "0"},
            }
        ]
        self.database.sync_user_live_orders_snapshot.return_value = []
        self.database.sync_user_live_positions_snapshot.return_value = []
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
        self.assertEqual(result["positions_open"], 0)

    def test_live_circuit_breaker_uses_live_summary(self):
        database = mock.Mock()
        database.get_daily_live_guardrail_summary.return_value = {
            "closed_trades": 2,
            "realized_pnl": -25.0,
            "realized_pnl_pct": -2.5,
            "consecutive_losses": 1,
        }
        database.get_daily_paper_guardrail_summary.return_value = {
            "closed_trades": 99,
            "realized_pnl": 999.0,
            "realized_pnl_pct": 9.99,
            "consecutive_losses": 0,
        }
        risk_service = RiskManagementService(database=database)

        result = risk_service.evaluate_circuit_breaker(
            symbol="BTC/USDT",
            timeframe="15m",
            strategy_version="live-v1",
            execution_scope="live",
            live_context={"user_id": 7, "account_id": "acct-1"},
        )

        self.assertFalse(result["allowed"])
        database.get_daily_live_guardrail_summary.assert_called_once()
        database.get_daily_paper_guardrail_summary.assert_not_called()

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

    def test_fetch_symbol_trading_rules_reads_market_limits_and_filters(self):
        exchange = mock.Mock()
        exchange.id = "binanceusdm"
        exchange.load_markets.return_value = {
            "BTC/USDT:USDT": {
                "id": "BTCUSDT",
                "symbol": "BTC/USDT:USDT",
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
                "precision": {"amount": 3},
                "info": {
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    ]
                },
            }
        }

        with mock.patch.object(self.service, "_build_authenticated_exchange", return_value=(exchange, {})):
            rules = self.service.fetch_symbol_trading_rules(self.context, symbol="BTC/USDT", testnet=True)

        self.assertEqual(rules["exchange_symbol"], "BTC/USDT:USDT")
        self.assertEqual(rules["min_qty"], 0.001)
        self.assertEqual(rules["min_notional"], 5.0)
        self.assertEqual(rules["qty_step"], 0.001)
        self.assertEqual(rules["min_price_tick"], 0.1)

    def test_risk_operability_detects_bankroll_below_exchange_minimum(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=100.0,
            stop_loss_pct=1.5,
            risk_pct=0.25,
            quantity=0.01,
            position_notional=1.0,
            trading_rules={"min_qty": 0.05, "min_notional": 5.0},
        )

        self.assertFalse(result["allowed"])
        self.assertGreater(result["min_required_balance"], 0.0)

    def test_risk_operability_uses_rounded_quantity_after_step(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=100.0,
            stop_loss_pct=1.5,
            risk_pct=0.25,
            quantity=0.0109,
            position_notional=1.09,
            trading_rules={"min_qty": 0.011, "min_notional": 1.10, "qty_step": 0.001},
        )

        self.assertFalse(result["allowed"])
        self.assertEqual(result["rounded_quantity"], 0.01)

    def test_risk_operability_reports_min_balance_for_allocation_sizing(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=62500.0,
            stop_loss_pct=1.5,
            risk_pct=2.0,
            quantity=0.004,
            position_notional=250.0,
            trading_rules={"min_qty": 0.001, "min_notional": 100.0, "qty_step": 0.001},
            leverage=10,
            sizing_mode="allocation",
            margin_allocation_pct=100.0,
        )

        self.assertTrue(result["allowed"])
        self.assertAlmostEqual(result["min_required_balance"], 12.5, places=4)

    def test_risk_operability_reports_btc_exchange_minimum_for_micro_size(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=62500.0,
            stop_loss_pct=1.5,
            risk_pct=2.0,
            quantity=0.00035,
            position_notional=21.875,
            trading_rules={"min_qty": 0.001, "min_notional": 50.0, "qty_step": 0.001},
            leverage=10,
            sizing_mode="hybrid",
            account_balance=22.0,
            available_balance=22.0,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("minimo da exchange", result["reason"])
        self.assertEqual(result["rounded_quantity"], 0.0)
        self.assertEqual(result["required_quantity"], 0.001)
        self.assertAlmostEqual(result["required_notional"], 62.5, places=4)
        self.assertGreater(result["required_risk_pct"], 2.0)

    def test_risk_operability_accepts_sub_one_percent_stop_values(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=62500.0,
            stop_loss_pct=0.8,
            risk_pct=2.0,
            quantity=0.001,
            position_notional=62.5,
            trading_rules={"min_qty": 0.001, "min_notional": 50.0, "qty_step": 0.001},
            leverage=10,
            sizing_mode="hybrid",
            account_balance=25.0,
            available_balance=25.0,
        )

        self.assertTrue(result["allowed"])
        self.assertAlmostEqual(result["min_required_balance"], 25.0, places=4)
        self.assertAlmostEqual(result["required_risk_pct"], 2.0, places=4)

    def test_risk_operability_adjusts_btc_micro_size_when_risk_allows(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=62500.0,
            stop_loss_pct=1.5,
            risk_pct=5.0,
            quantity=0.00035,
            position_notional=21.875,
            trading_rules={"min_qty": 0.001, "min_notional": 50.0, "qty_step": 0.001},
            leverage=10,
            sizing_mode="hybrid",
            account_balance=22.0,
            available_balance=22.0,
            allow_exchange_minimum_adjustment=True,
        )

        self.assertTrue(result["allowed"])
        self.assertTrue(result["exchange_minimum_adjusted"])
        self.assertEqual(result["rounded_quantity"], 0.001)
        self.assertAlmostEqual(result["rounded_notional"], 62.5, places=4)
        self.assertAlmostEqual(result["required_margin"], 6.25, places=4)

    def test_risk_operability_blocks_exchange_minimum_when_risk_cap_is_exceeded(self):
        risk_service = RiskManagementService(database=mock.Mock())
        result = risk_service.evaluate_symbol_operability(
            entry_price=62500.0,
            stop_loss_pct=1.5,
            risk_pct=2.0,
            quantity=0.00035,
            position_notional=21.875,
            trading_rules={"min_qty": 0.001, "min_notional": 50.0, "qty_step": 0.001},
            leverage=10,
            sizing_mode="hybrid",
            account_balance=22.0,
            available_balance=22.0,
            allow_exchange_minimum_adjustment=True,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("violaria o limite de risco", result["reason"])
        self.assertAlmostEqual(result["required_risk_pct"], 4.2614, places=4)


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

    def test_multiuser_runtime_uses_strategy_stop_by_side_when_no_override_is_sent(self):
        database = mock.Mock()
        database.save_user_execution_event.return_value = 99
        risk_service = mock.Mock()
        risk_service.evaluate_risk_engine.return_value = {
            "allowed": True,
            "risk_mode": "normal",
            "risk_amount": 0.3,
            "position_notional": 20.0,
            "quantity": 0.0002,
        }
        service = MultiUserRuntimeService(
            database=database,
            risk_management_service=risk_service,
            live_execution_service=mock.Mock(),
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
            "risk_profile": {"is_valid": True, "leverage_cap": 10},
            "capital_base": 20.0,
            "allowed_symbols": ["BTC/USDT"],
            "allowed_timeframes": ["15m"],
        }

        with (
            mock.patch.object(config, "SHORT_STOP_LOSS_PCT", 1.2),
            mock.patch.object(config.ProductionConfig, "ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION", False),
        ):
            result = service.run_account_cycle(
                context=context,
                symbol="BTC/USDT",
                timeframe="15m",
                strategy_version="test-v1",
                entry_price=71000.0,
                stop_loss_pct=None,
                signal_side="sell",
            )

        self.assertEqual(result["status"], "ready_no_auto_order")
        self.assertAlmostEqual(
            risk_service.evaluate_risk_engine.call_args.kwargs["stop_loss_pct"],
            1.2,
        )

    def test_multiuser_runtime_respects_user_stop_override(self):
        database = mock.Mock()
        database.save_user_execution_event.return_value = 99
        risk_service = mock.Mock()
        risk_service.evaluate_risk_engine.return_value = {
            "allowed": True,
            "risk_mode": "normal",
            "risk_amount": 0.3,
            "position_notional": 20.0,
            "quantity": 0.0002,
        }
        service = MultiUserRuntimeService(
            database=database,
            risk_management_service=risk_service,
            live_execution_service=mock.Mock(),
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
            "risk_profile": {"is_valid": True, "leverage_cap": 10},
            "capital_base": 20.0,
            "allowed_symbols": ["BTC/USDT"],
            "allowed_timeframes": ["15m"],
        }

        with mock.patch.object(config.ProductionConfig, "ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION", False):
            service.run_account_cycle(
                context=context,
                symbol="BTC/USDT",
                timeframe="15m",
                strategy_version="test-v1",
                entry_price=71000.0,
                stop_loss_pct=1.8,
                signal_side="sell",
            )

        self.assertAlmostEqual(
            risk_service.evaluate_risk_engine.call_args.kwargs["stop_loss_pct"],
            1.8,
        )


if __name__ == "__main__":
    unittest.main()
