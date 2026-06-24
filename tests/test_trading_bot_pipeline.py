import unittest
from unittest import mock

import pandas as pd

from trading_bot import TradingBot


class TradingBotPipelineTests(unittest.TestCase):
    @staticmethod
    def _build_market_df(length: int = 80) -> pd.DataFrame:
        base_time = pd.Timestamp("2026-05-01T00:00:00+00:00")
        rows = []
        for index in range(length):
            price = 100.0 + (index * 0.4)
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * index),
                    "open": price - 0.2,
                    "high": price + 0.5,
                    "low": price - 0.5,
                    "close": price,
                    "volume": 1000.0 + (index * 5.0),
                }
            )
        return pd.DataFrame(rows)

    def _build_snapshot(
        self,
        *,
        signal: str = "COMPRA",
        market_pattern: str = "pullback_long",
        confidence: float = 7.2,
    ):
        signal_direction = signal if signal in {"COMPRA", "VENDA"} else "NEUTRO"
        objective_passed = signal in {"COMPRA", "VENDA"}
        return {
            "analysis": {
                "signal": signal,
                "reason": "snapshot",
            },
            "context_evaluation": {
                "market_bias": "bullish" if signal == "COMPRA" else "bearish" if signal == "VENDA" else "neutral",
                "bias": "bullish" if signal == "COMPRA" else "bearish" if signal == "VENDA" else "neutral",
                "context_strength": 7.0,
            },
            "regime_evaluation": {
                "regime": "trend",
                "market_bias": "bullish" if signal == "COMPRA" else "bearish" if signal == "VENDA" else "neutral",
            },
            "structure_evaluation": {
                "structure_state": "trend_resume",
                "structure_quality": 7.0,
            },
            "confirmation_evaluation": {
                "confirmation_state": "confirmed",
                "confirmation_score": 7.0,
            },
            "entry_evaluation": {
                "objective_passed": objective_passed,
                "objective_quality": "strong" if objective_passed else "bad",
                "signal_direction": signal_direction,
                "context_bias": "bullish" if signal == "COMPRA" else "bearish" if signal == "VENDA" else "neutral",
                "context_aligned": True,
                "context_tradeable": objective_passed,
                "passes_score_floor": objective_passed,
                "failed_flags": [],
                "critical_failed_flags": [],
                "rejection_reason": None if objective_passed else "sem setup",
                "entry_quality": "good" if objective_passed else "bad",
                "entry_score": 7.0 if objective_passed else 0.0,
                "market_pattern": market_pattern,
                "setup_type": market_pattern,
                "entry_reason": "snapshot-entry" if objective_passed else None,
                "invalid_if": None,
            },
            "scenario_evaluation": {
                "scenario_score": 7.0 if objective_passed else 0.0,
                "scenario_grade": "B" if objective_passed else "D",
            },
            "market_state_evaluation": {
                "market_state": "trend_bullish" if signal == "COMPRA" else "trend_bearish" if signal == "VENDA" else "neutral_chop",
                "execution_mode": "ready" if objective_passed else "standby",
            },
            "trade_decision": {
                "action": "buy" if signal == "COMPRA" else "sell" if signal == "VENDA" else "wait",
                "confidence": confidence,
                "entry_reason": "snapshot-entry" if objective_passed else None,
                "block_reason": None if objective_passed else "sem setup",
            },
        }

    def test_evaluate_signal_pipeline_blocks_low_confidence_candidate(self):
        bot = TradingBot()
        snapshot = self._build_snapshot(signal="COMPRA", confidence=5.5)

        with mock.patch.object(bot, "_build_resume_snapshot", return_value=snapshot):
            result = bot.evaluate_signal_pipeline(df=object(), min_confidence=60)

        self.assertEqual(result["candidate_signal"], "COMPRA")
        self.assertEqual(result["analytical_signal"], "NEUTRO")
        self.assertEqual(result["blocked_signal"], "COMPRA")
        self.assertEqual(result["block_source"], "confidence_filter")

    def test_evaluate_signal_pipeline_respects_setup_allowlist(self):
        bot = TradingBot()
        snapshot = self._build_snapshot(signal="COMPRA", market_pattern="pullback_long", confidence=7.2)

        with mock.patch.object(bot, "_build_resume_snapshot", return_value=snapshot):
            result = bot.evaluate_signal_pipeline(
                df=object(),
                min_confidence=60,
                allowed_execution_setups=["trend_resume_long"],
            )

        self.assertEqual(result["candidate_signal"], "COMPRA")
        self.assertEqual(result["approved_signal"], "NEUTRO")
        self.assertEqual(result["blocked_signal"], "COMPRA")
        self.assertEqual(result["block_source"], "setup_allowlist")

    def test_get_signal_with_confidence_uses_pipeline_decision(self):
        bot = TradingBot()
        snapshot = self._build_snapshot(signal="VENDA", confidence=7.2)

        with mock.patch.object(bot, "_build_resume_snapshot", return_value=snapshot):
            result = bot.get_signal_with_confidence(df=object())

        self.assertEqual(result["signal"], "VENDA")
        self.assertAlmostEqual(result["confidence"], 72.0, places=4)

    def test_calculate_indicators_keeps_dashboard_contract(self):
        bot = TradingBot()
        result = bot.calculate_indicators(self._build_market_df())

        expected_columns = {
            "ema_fast",
            "ema_slow",
            "ema_trend",
            "rsi",
            "atr",
            "atr_pct",
            "macd",
            "macd_signal",
            "volume_ma",
            "sma_21",
            "market_regime",
            "signal_confidence",
            "is_closed",
        }
        self.assertTrue(expected_columns.issubset(set(result.columns)))
        self.assertTrue(bool(result["is_closed"].iloc[-1]))

    def test_build_resume_snapshot_returns_runtime_sections(self):
        bot = TradingBot()

        result = bot._build_resume_snapshot(
            df=self._build_market_df(),
            timeframe="15m",
        )

        expected_sections = {
            "analysis",
            "context_evaluation",
            "regime_evaluation",
            "structure_evaluation",
            "confirmation_evaluation",
            "entry_evaluation",
            "scenario_evaluation",
            "market_state_evaluation",
            "trade_decision",
        }
        self.assertTrue(expected_sections.issubset(set(result.keys())))
        self.assertIn(result["analysis"]["signal"], {"COMPRA", "VENDA", "NEUTRO"})
        self.assertIn(result["trade_decision"]["action"], {"buy", "sell", "wait"})


if __name__ == "__main__":
    unittest.main()
