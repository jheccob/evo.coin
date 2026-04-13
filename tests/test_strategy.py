import unittest
from unittest import mock

import pandas as pd

import bot_runner
import config
from position_manager import create_position, evaluate_open_position
from strategy import analyze_prepared_candle, prepare_candle_features


class StrategyTests(unittest.TestCase):
    @staticmethod
    def _build_trend_df(start_price: float, step: float, length: int = 80) -> pd.DataFrame:
        rows = []
        offset_cycle = [0.0, 0.7, 1.2, 1.6, -0.8, -1.4]
        for i in range(length):
            close_price = start_price + (step * i) + (offset_cycle[i % len(offset_cycle)] * abs(step))
            candle_range = max(abs(step) * 1.2, 1.2)
            rows.append(
                {
                    "timestamp": i,
                    "open": close_price - (step * 0.25),
                    "high": close_price + candle_range,
                    "low": close_price - candle_range,
                    "close": close_price,
                    "volume": 1000 + (i * 10),
                }
            )
        return pd.DataFrame(rows)

    def test_prepare_candle_features_adds_expected_columns(self):
        df = self._build_trend_df(start_price=100.0, step=1.0, length=80)
        features = prepare_candle_features(df)
        expected = {"ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "atr_pct", "is_closed"}
        self.assertTrue(expected.issubset(set(features.columns)))

    def test_analyze_prepared_candle_hold_when_data_is_insufficient(self):
        df = pd.DataFrame(
            [
                {"close": 100.0, "ema_fast": 99.5, "ema_slow": 99.0, "ema_trend": 98.0, "rsi": 53.0},
                {"close": 101.0, "ema_fast": 100.5, "ema_slow": 100.0, "ema_trend": 99.0, "rsi": 54.0},
            ]
        )
        result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "hold")

    def test_analyze_prepared_candle_buy_signal(self):
        df = self._build_trend_df(start_price=100.0, step=1.4, length=90)
        result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "buy")

    def test_analyze_prepared_candle_sell_signal(self):
        df = self._build_trend_df(start_price=220.0, step=-1.5, length=90)
        result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "sell")

    def test_analyze_prepared_candle_hold_without_trigger(self):
        df = self._build_trend_df(start_price=100.0, step=0.9, length=90)
        result = analyze_prepared_candle(
            df,
            buy_rsi_threshold=float(config.BUY_RSI_SIGNAL) + 20.0,
            sell_rsi_threshold=float(config.SELL_RSI_SIGNAL) - 10.0,
        )
        self.assertEqual(result["signal"], "hold")

    def test_long_position_partial_then_stop_or_trailing_close(self):
        position = create_position("buy", 100.0, 1, atr=1.0)
        partial_result = evaluate_open_position(position, 105.0, 2)
        self.assertEqual(partial_result["action"], "partial")

        close_result = evaluate_open_position(partial_result["position"], 1.0, 3)
        self.assertEqual(close_result["action"], "close")
        self.assertEqual(close_result["closed_position"]["reason"], "stop_or_trailing")

    def test_short_position_partial_then_stop_or_trailing_close(self):
        position = create_position("sell", 100.0, 1, atr=1.0)
        partial_result = evaluate_open_position(position, 95.0, 2)
        self.assertEqual(partial_result["action"], "partial")

        close_result = evaluate_open_position(partial_result["position"], 10_000.0, 3)
        self.assertEqual(close_result["action"], "close")
        self.assertEqual(close_result["closed_position"]["reason"], "stop_or_trailing")

    def test_runtime_recovery_restores_last_candle_position_and_risk_state(self):
        snapshot = config.build_runtime_strategy_snapshot()
        persisted_position = create_position("buy", 100.0, "2026-04-12T00:00:00+00:00", atr=1.0)
        persisted_position["best_price"] = 103.5
        persisted_position["current_stop"] = 100.0
        persisted_position["partial_taken"] = True
        persisted_position["break_even_active"] = True

        runtime_row = {
            "runtime_key": "primary:BTC/USDT:15m",
            "strategy_version": snapshot["strategy_version"],
            "status": "position_open",
            "last_candle_timestamp": "2026-04-12T01:15:00+00:00",
            "state_payload": {
                "risk_state": {
                    "day": "2026-04-12",
                    "daily_realized_pct": -0.4,
                    "consecutive_losses": 2,
                    "blocked": True,
                    "block_reason": "circuit breaker",
                },
                "position": bot_runner._serialize_position(persisted_position),
            },
        }

        with mock.patch.object(bot_runner.db, "get_bot_runtime_state", return_value=[runtime_row]):
            restored_timestamp, restored_position, restored_risk_state = bot_runner._load_runtime_recovery_state(snapshot)

        self.assertIsNotNone(restored_timestamp)
        self.assertEqual(str(restored_timestamp.isoformat()), "2026-04-12T01:15:00+00:00")
        self.assertIsNotNone(restored_position)
        self.assertEqual(restored_position["side"], "long")
        self.assertEqual(restored_position["entry_price"], 100.0)
        self.assertEqual(restored_position["best_price"], 103.5)
        self.assertTrue(restored_position["partial_taken"])
        self.assertTrue(restored_risk_state["blocked"])
        self.assertEqual(restored_risk_state["consecutive_losses"], 2)

    def test_runtime_recovery_returns_clean_defaults_when_no_state_exists(self):
        snapshot = config.build_runtime_strategy_snapshot()
        with mock.patch.object(bot_runner.db, "get_bot_runtime_state", return_value=[]):
            restored_timestamp, restored_position, restored_risk_state = bot_runner._load_runtime_recovery_state(snapshot)

        self.assertIsNone(restored_timestamp)
        self.assertIsNone(restored_position)
        self.assertFalse(restored_risk_state["blocked"])
        self.assertEqual(restored_risk_state["daily_realized_pct"], 0.0)

    def test_runtime_feed_validation_blocks_missing_websocket_dependency(self):
        with self.assertRaises(RuntimeError):
            bot_runner._validate_stream_runtime_ready(
                {
                    "provider": "rest_fallback:none",
                    "connected": False,
                    "last_error": "Pacote websockets nao instalado; usando REST.",
                }
            )

    def test_get_pending_candle_indexes_returns_all_candles_after_last_processed(self):
        df = pd.DataFrame(
            [
                {"timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:30:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:45:00+00:00")},
            ]
        )

        pending_indexes = bot_runner._get_pending_candle_indexes(
            df,
            pd.Timestamp("2026-04-12T00:15:00+00:00"),
        )

        self.assertEqual(pending_indexes, [2, 3])

    def test_get_pending_candle_indexes_returns_empty_without_previous_timestamp(self):
        df = pd.DataFrame(
            [
                {"timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00")},
            ]
        )

        pending_indexes = bot_runner._get_pending_candle_indexes(df, None)

        self.assertEqual(pending_indexes, [])

    def test_single_user_execution_context_uses_config_defaults(self):
        with mock.patch.object(config, "SINGLE_USER_RUNTIME_USER_ID", 9):
            with mock.patch.object(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "env-main"):
                with mock.patch.object(config, "SINGLE_USER_RUNTIME_ACCOUNT_ALIAS", "Conta Runner"):
                    with mock.patch.object(config, "SINGLE_USER_RUNTIME_EXCHANGE", "binanceusdm"):
                        context = bot_runner._build_single_user_execution_context()

        self.assertEqual(context["user_id"], 9)
        self.assertEqual(context["account_id"], "env-main")
        self.assertEqual(context["account_alias"], "Conta Runner")
        self.assertEqual(context["exchange_name"], "binanceusdm")
        self.assertTrue(context["use_env_credentials"])

    def test_prepare_live_execution_runtime_clears_stale_local_position_when_exchange_has_none(self):
        snapshot = config.build_runtime_strategy_snapshot()
        recovered_position = create_position("buy", 100.0, "2026-04-12T00:00:00+00:00", atr=1.0)
        service = mock.Mock()
        service.validate_account_connection.return_value = {"ok": True}
        service.reconcile_account_state.return_value = {"ok": True}
        user_stream = mock.Mock()
        user_stream.wait_until_ready.return_value = True
        service.start_user_data_stream.return_value = user_stream
        context = {
            "user_id": 0,
            "account_id": "env-primary",
            "account_alias": "Runner",
            "exchange_name": "binanceusdm",
            "exchange": "binanceusdm",
            "use_env_credentials": True,
            "credential_source": "env",
        }

        with mock.patch.object(bot_runner, "_build_single_user_execution_context", return_value=context):
            with mock.patch.object(bot_runner.db, "get_user_live_positions", return_value=[]):
                with mock.patch.object(bot_runner.db, "save_user_execution_event", return_value=1):
                    resolved_context, resolved_position, resolved_stream = bot_runner._prepare_live_execution_runtime(
                        snapshot=snapshot,
                        execution_service=service,
                        recovered_position=recovered_position,
                    )

        self.assertEqual(resolved_context["account_id"], "env-primary")
        self.assertIsNone(resolved_position)
        self.assertIs(resolved_stream, user_stream)

    def test_prepare_live_execution_runtime_blocks_unknown_exchange_position(self):
        snapshot = config.build_runtime_strategy_snapshot()
        service = mock.Mock()
        service.validate_account_connection.return_value = {"ok": True}
        service.reconcile_account_state.return_value = {"ok": True}
        context = {
            "user_id": 0,
            "account_id": "env-primary",
            "account_alias": "Runner",
            "exchange_name": "binanceusdm",
            "exchange": "binanceusdm",
            "use_env_credentials": True,
            "credential_source": "env",
        }
        open_exchange_position = [
            {
                "symbol": config.SYMBOL,
                "exchange": "binanceusdm",
                "side": "long",
                "quantity": 0.01,
                "status": "open",
            }
        ]

        with mock.patch.object(bot_runner, "_build_single_user_execution_context", return_value=context):
            with mock.patch.object(bot_runner.db, "get_user_live_positions", return_value=open_exchange_position):
                with self.assertRaises(RuntimeError):
                    bot_runner._prepare_live_execution_runtime(
                        snapshot=snapshot,
                        execution_service=service,
                        recovered_position=None,
                    )


if __name__ == "__main__":
    unittest.main()
