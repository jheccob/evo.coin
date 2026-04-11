import unittest

import pandas as pd

from position_manager import create_position, evaluate_open_position
from strategy import analyze_prepared_candle, prepare_candle_features


class StrategyTests(unittest.TestCase):
    def test_prepare_candle_features_adds_expected_columns(self):
        df = pd.DataFrame(
            [
                {"timestamp": i, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 1000}
                for i in range(40)
            ]
        )
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
        df = pd.DataFrame(
            [
                {"close": 100.0, "ema_fast": 99.0, "ema_slow": 98.0, "ema_trend": 97.0, "rsi": 50.0},
                {"close": 101.0, "ema_fast": 100.0, "ema_slow": 99.0, "ema_trend": 98.0, "rsi": 53.5},
                {"close": 103.0, "ema_fast": 102.0, "ema_slow": 101.0, "ema_trend": 100.0, "rsi": 56.0},
            ]
        )
        result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "buy")

    def test_analyze_prepared_candle_sell_signal(self):
        df = pd.DataFrame(
            [
                {"close": 100.0, "ema_fast": 101.0, "ema_slow": 102.0, "ema_trend": 103.0, "rsi": 50.0},
                {"close": 99.0, "ema_fast": 100.0, "ema_slow": 101.0, "ema_trend": 102.0, "rsi": 48.0},
                {"close": 97.0, "ema_fast": 98.0, "ema_slow": 99.0, "ema_trend": 100.0, "rsi": 44.0},
            ]
        )
        result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "sell")

    def test_analyze_prepared_candle_hold_without_trigger(self):
        df = pd.DataFrame(
            [
                {"close": 100.0, "ema_fast": 99.0, "ema_slow": 98.0, "ema_trend": 97.0, "rsi": 55.5},
                {"close": 101.0, "ema_fast": 100.0, "ema_slow": 99.0, "ema_trend": 98.0, "rsi": 56.0},
                {"close": 102.0, "ema_fast": 101.0, "ema_slow": 100.0, "ema_trend": 99.0, "rsi": 57.0},
            ]
        )
        result = analyze_prepared_candle(df)
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


if __name__ == "__main__":
    unittest.main()
