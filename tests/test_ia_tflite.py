from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from ai_model import AIModel
from ia.dataset_builder import FEATURE_COLUMNS, LABEL_NAMES, build_multi_symbol_dataset, build_supervised_dataset_from_frame, prepare_feature_frame, save_dataset


class TensorFlowLiteDatasetTests(unittest.TestCase):
    def _build_frame(self) -> pd.DataFrame:
        rows = []
        base_time = pd.Timestamp("2026-01-01T00:00:00+00:00")
        price = 0.10
        for index in range(120):
            drift = 0.0005 if index % 9 < 5 else -0.0003
            open_price = price
            close_price = price + drift + ((index % 4) * 0.0001)
            high_price = max(open_price, close_price) + 0.0004
            low_price = min(open_price, close_price) - 0.0004
            volume = 1000 + (index * 5)
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * index),
                    "open": round(open_price, 6),
                    "high": round(high_price, 6),
                    "low": round(low_price, 6),
                    "close": round(close_price, 6),
                    "volume": float(volume),
                }
            )
            price = close_price
        return pd.DataFrame(rows)

    def test_build_supervised_dataset_from_frame_outputs_expected_shapes(self):
        dataset = build_supervised_dataset_from_frame(
            self._build_frame(),
            label_mode="target_window",
            horizon_candles=4,
            target_pct=0.15,
            risk_buffer_pct=0.15,
        )

        self.assertGreater(dataset["features"].shape[0], 0)
        self.assertEqual(dataset["features"].shape[1], len(FEATURE_COLUMNS))
        self.assertEqual(dataset["labels"].shape[0], dataset["features"].shape[0])
        self.assertEqual(dataset["metadata"]["rows"], dataset["features"].shape[0])
        self.assertTrue(set(np.unique(dataset["labels"])).issubset({0, 1, 2}))

    def test_feature_columns_include_explicit_indicator_blocks(self):
        expected_columns = {
            "macd_line_pct",
            "macd_signal_pct",
            "macd_hist_pct",
            "bollinger_width_pct",
            "bollinger_position",
            "stoch_k",
            "stoch_d",
            "rolling_volatility_12_pct",
            "rolling_volatility_24_pct",
            "rolling_vwap_gap_pct",
            "rolling_vwap_slow_gap_pct",
            "volume_ratio_fast",
            "signed_volume_flow",
            "ema_regime_bias",
            "distance_to_rolling_high_96_pct",
            "distance_to_rolling_low_96_pct",
            "channel_width_32_pct",
            "channel_width_96_pct",
            "channel_position_32",
            "channel_position_96",
            "resistance_pressure_score",
            "support_pressure_score",
            "range_regime_score",
            "trend_regime_score",
        }

        self.assertTrue(expected_columns.issubset(set(FEATURE_COLUMNS)))

    def test_save_dataset_writes_npz_payload(self):
        dataset = build_supervised_dataset_from_frame(
            self._build_frame(),
            label_mode="target_window",
            horizon_candles=4,
            target_pct=0.15,
            risk_buffer_pct=0.15,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "sample_dataset.npz"
            save_dataset(dataset, output_path)

            self.assertTrue(output_path.exists())
            with np.load(output_path, allow_pickle=False) as payload:
                self.assertEqual(payload["features"].shape[1], len(FEATURE_COLUMNS))
                self.assertEqual(payload["label_names"].tolist(), LABEL_NAMES)

    def test_build_multi_symbol_dataset_merges_sources(self):
        source_a = self._build_frame()
        source_b = self._build_frame().copy()
        source_b["close"] = source_b["close"] * 1.2
        frames = {
            ("BTC/USDT", "15m"): source_a,
            ("XLM/USDT", "15m"): source_b,
        }

        def fake_loader(symbol, timeframe, **kwargs):
            return frames[(symbol, timeframe)].copy()

        with mock.patch("ia.dataset_builder.load_market_frame", side_effect=fake_loader):
            dataset = build_multi_symbol_dataset(
                [
                    {"symbol": "BTC/USDT", "timeframe": "15m", "total_limit": 0},
                    {"symbol": "XLM/USDT", "timeframe": "15m", "total_limit": 0},
                ],
                label_mode="target_window",
                horizon_candles=4,
                target_pct=0.15,
                risk_buffer_pct=0.15,
            )

        merged_frame = dataset["frame"]
        self.assertGreater(len(merged_frame), 0)
        self.assertIn("source_symbol", merged_frame.columns)
        self.assertIn("source_is_btc", merged_frame.columns)
        self.assertIn("source_is_xlm", merged_frame.columns)
        self.assertIn("BTC/USDT", set(merged_frame["source_symbol"]))
        self.assertIn("XLM/USDT", set(merged_frame["source_symbol"]))

    def test_trade_outcome_label_mode_generates_real_trade_fields(self):
        dataset = build_supervised_dataset_from_frame(
            self._build_frame(),
            label_mode="trade_outcome",
            max_holding_candles=8,
            min_trade_net_pct=0.05,
            decision_edge_pct=0.01,
        )

        self.assertGreater(dataset["features"].shape[0], 0)
        self.assertEqual(dataset["metadata"]["label_mode"], "trade_outcome")
        self.assertIn("long_trade_net_pct", dataset["frame"].columns)
        self.assertIn("short_trade_net_pct", dataset["frame"].columns)
        self.assertTrue(set(np.unique(dataset["labels"])).issubset({0, 1, 2}))

    def test_trade_journal_label_mode_marks_profitable_trade_signals(self):
        frame = self._build_frame()
        feature_frame = prepare_feature_frame(frame.copy())
        signal_timestamp = feature_frame.iloc[-20]["timestamp"]

        fake_trades = [
            {
                "signal_timestamp": signal_timestamp.isoformat(),
                "side": "long",
                "net_pct": 0.42,
                "reason": "take_profit",
            }
        ]
        fake_summary = {
            "trades": 1,
            "wins": 1,
            "losses": 0,
            "profit_factor": 99.0,
            "avg_trade_pct": 0.42,
        }

        with mock.patch("backtest.run_backtest", return_value=(fake_trades, fake_summary)):
            dataset = build_supervised_dataset_from_frame(
                frame,
                label_mode="trade_journal",
                source_symbol="BTC/USDT",
                timeframe="15m",
                min_trade_net_pct=0.10,
            )

        matching = dataset["frame"][
            pd.to_datetime(dataset["frame"]["timestamp"], utc=True) == pd.Timestamp(signal_timestamp).tz_convert("UTC")
        ]
        self.assertGreater(len(matching), 0)
        self.assertEqual(matching.iloc[0]["label_name"], "long")
        self.assertEqual(dataset["metadata"]["label_mode"], "trade_journal")
        self.assertEqual(dataset["metadata"]["journal_summary"]["wins"], 1)

    def test_ai_model_injects_symbol_source_flags_at_inference_time(self):
        model = object.__new__(AIModel)

        class _StubRuntimeModel:
            model_loaded = True
            metadata = {"model_version": "stub"}
            feature_names = ["source_is_btc", "source_is_xlm", "rsi"]
            label_names = ["short", "hold", "long"]

            def __init__(self):
                self.last_vector = None

            def predict(self, feature_vector):
                self.last_vector = np.asarray(feature_vector, dtype="float32").tolist()
                return np.array([0.2, 0.3, 0.5], dtype="float32")

        runtime = _StubRuntimeModel()
        model.runtime_model = runtime
        feature_row = pd.Series({"rsi": 61.5})
        result = model.score_feature_row(feature_row, symbol="BTC/USDT", timeframe="15m", market_context=None)

        self.assertEqual(runtime.last_vector[:2], [1.0, 0.0])
        self.assertEqual(result["signal"], "buy")


if __name__ == "__main__":
    unittest.main()
