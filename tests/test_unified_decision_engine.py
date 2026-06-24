from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import pandas as pd

from ai_model import AIModel
import bot_runner
import config
from services.adaptive_learning_service import AdaptiveLearningService
from services.unified_decision_engine import UnifiedDecisionEngine
from strategy_engine import StrategyParams


class _StubAIModel(AIModel):
    def __init__(self, signal: str, probabilities: dict[str, float], confidence: float):
        self._signal = signal
        self._probabilities = probabilities
        self._confidence = confidence

    def score_candle_slice(self, candle_slice, *, symbol: str, timeframe: str, market_context: dict | None = None) -> dict:  # noqa: ARG002
        return {
            "enabled": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "signal": self._signal,
            "label": "long" if self._signal == "buy" else ("short" if self._signal == "sell" else "hold"),
            "confidence": self._confidence,
            "reason": "stub",
            "probabilities": dict(self._probabilities),
            "raw_probabilities": dict(self._probabilities),
            "context": market_context or {},
        }


class _StubMarketContextService:
    def get_context(self, symbol: str) -> dict:  # noqa: ARG002
        return {
            "symbol": "XLM/USDT",
            "fear_greed": {"available": False},
            "news": {"available": False},
            "bias": {"long_bias": 0.0, "short_bias": 0.0, "caution_bias": 0.0, "reasons": []},
        }


class UnifiedDecisionEngineTests(unittest.TestCase):
    def _build_frame(self) -> pd.DataFrame:
        rows = []
        base_time = pd.Timestamp("2026-01-01T00:00:00+00:00")
        price = 0.11
        for index in range(260):
            drift = 0.0006 if index % 10 < 6 else -0.0002
            open_price = price
            close_price = price + drift + ((index % 3) * 0.0001)
            high_price = max(open_price, close_price) + 0.0004
            low_price = min(open_price, close_price) - 0.0004
            volume = 1000 + (index * 7)
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

    def test_learning_service_registers_bias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            learning = AdaptiveLearningService(
                f"{tmpdir}/memory.json",
                enabled=True,
                min_trades=2,
                max_bias=0.12,
            )
            learning.register_trade(symbol="XLM/USDT", timeframe="15m", side="long", setup_name="pullback_long", net_pct=1.2)
            learning.register_trade(symbol="XLM/USDT", timeframe="15m", side="long", setup_name="pullback_long", net_pct=0.8)
            bias = learning.get_bias(symbol="XLM/USDT", timeframe="15m", side="long", setup_name="pullback_long")
            self.assertGreater(bias.bias, 0.0)
            self.assertEqual(bias.trade_count, 2)

    def test_unified_engine_returns_hybrid_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            learning = AdaptiveLearningService(
                f"{tmpdir}/memory.json",
                enabled=True,
                min_trades=1,
                max_bias=0.12,
            )
            learning.register_trade(symbol="XLM/USDT", timeframe="15m", side="short", setup_name="unknown", net_pct=1.1)
            engine = UnifiedDecisionEngine(
                symbol="XLM/USDT",
                timeframe="15m",
                ai_model=_StubAIModel("sell", {"short": 0.61, "hold": 0.25, "long": 0.14}, 0.61),
                market_context_service=_StubMarketContextService(),
                learning_service=learning,
                use_live_context=False,
            )
            with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "hybrid"):
                result = engine.decide_entry(self._build_frame(), StrategyParams())
            self.assertIn("decision_source", result)
            self.assertIn("ai_decision", result)

    def test_setup_guard_blocks_after_consecutive_losses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            learning = AdaptiveLearningService(
                f"{tmpdir}/memory.json",
                enabled=True,
                min_trades=1,
                max_bias=0.12,
            )
            with mock.patch.object(config.ProductionConfig, "AI_SETUP_GUARD_ENABLED", True), mock.patch.object(
                config.ProductionConfig, "AI_SETUP_GUARD_MIN_TRADES", 1
            ), mock.patch.object(config.ProductionConfig, "AI_SETUP_GUARD_MAX_CONSECUTIVE_LOSSES", 3):
                for value in (-1.0, -0.8, -0.9):
                    learning.register_trade(
                        symbol="BTC/USDT",
                        timeframe="15m",
                        side="short",
                        setup_name="trend_resume_short",
                        net_pct=value,
                    )
            engine = UnifiedDecisionEngine(
                symbol="BTC/USDT",
                timeframe="15m",
                ai_model=_StubAIModel("sell", {"short": 0.61, "hold": 0.24, "long": 0.15}, 0.61),
                market_context_service=_StubMarketContextService(),
                learning_service=learning,
                use_live_context=False,
            )
            engine_result = {
                "signal": "sell",
                "reason": "resume short",
                "setup": {"setup": "trend_resume_short"},
                "score": 7,
                "atr": 0.4,
            }
            with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "hybrid"), mock.patch.object(
                config.ProductionConfig, "AI_SETUP_GUARD_ENABLED", True
            ), mock.patch.object(config.ProductionConfig, "AI_SETUP_GUARD_MIN_TRADES", 1), mock.patch.object(
                config.ProductionConfig, "AI_SETUP_GUARD_MAX_CONSECUTIVE_LOSSES", 3
            ), mock.patch("services.unified_decision_engine.generate_entry_signal", return_value=engine_result):
                result = engine.decide_entry(self._build_frame(), StrategyParams())

        self.assertEqual(result["signal"], "hold")
        self.assertIn("hybrid_blocked_setup_guard", str(result["reason"]))
        self.assertEqual(result["setup_guard"]["reason"], "cooldown_consecutive_losses")

    def test_structure_guard_blocks_pullback_long_in_weak_context(self):
        engine = UnifiedDecisionEngine(
            symbol="BTC/USDT",
            timeframe="15m",
            ai_model=_StubAIModel("buy", {"long": 0.62, "hold": 0.24, "short": 0.14}, 0.62),
            market_context_service=_StubMarketContextService(),
            use_live_context=False,
        )
        feature_frame = pd.DataFrame(
            [
                {
                    "adx": 33.0,
                    "trend_regime_score": 0.31,
                    "range_regime_score": 0.72,
                }
            ]
        )
        engine_result = {
            "signal": "buy",
            "reason": "pullback long",
            "setup": {"setup": "pullback_long"},
            "score": 8,
            "atr": 0.45,
        }
        with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "hybrid"), mock.patch.object(
            config.ProductionConfig, "AI_ENTRY_STRUCTURE_GUARD_ENABLED", True
        ), mock.patch(
            "services.unified_decision_engine.generate_entry_signal", return_value=engine_result
        ), mock.patch("services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame):
            result = engine.decide_entry(self._build_frame(), StrategyParams())
        self.assertEqual(result["signal"], "hold")
        self.assertIn("structure_block_pullback_long_weak_context", str(result["reason"]))

    def test_structure_guard_blocks_trend_resume_short_near_support(self):
        engine = UnifiedDecisionEngine(
            symbol="BTC/USDT",
            timeframe="15m",
            ai_model=_StubAIModel("sell", {"short": 0.63, "hold": 0.22, "long": 0.15}, 0.63),
            market_context_service=_StubMarketContextService(),
            use_live_context=False,
        )
        feature_frame = pd.DataFrame(
            [
                {
                    "distance_to_rolling_low_pct": 0.24,
                    "channel_position_32": 0.12,
                    "range_regime_score": 0.63,
                }
            ]
        )
        engine_result = {
            "signal": "sell",
            "reason": "resume short",
            "setup": {"setup": "trend_resume_short"},
            "score": 9,
            "atr": 0.52,
        }
        with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "hybrid"), mock.patch.object(
            config.ProductionConfig, "AI_ENTRY_STRUCTURE_GUARD_ENABLED", True
        ), mock.patch(
            "services.unified_decision_engine.generate_entry_signal", return_value=engine_result
        ), mock.patch("services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame):
            result = engine.decide_entry(self._build_frame(), StrategyParams())
        self.assertEqual(result["signal"], "hold")
        self.assertIn("structure_block_trend_resume_short_near_support", str(result["reason"]))

    def test_market_reading_long_enters_without_setup_dependency(self):
        engine = UnifiedDecisionEngine(
            symbol="BTC/USDT",
            timeframe="15m",
            ai_model=_StubAIModel("buy", {"long": 0.66, "hold": 0.20, "short": 0.14}, 0.66),
            market_context_service=_StubMarketContextService(),
            use_live_context=False,
        )
        feature_frame = pd.DataFrame(
            [
                {
                    "atr": 0.41,
                    "adx": 29.0,
                    "trend_regime_score": 0.58,
                    "range_regime_score": 0.33,
                    "channel_position_32": 0.51,
                    "distance_to_rolling_high_pct": 0.94,
                    "distance_to_rolling_low_pct": 0.74,
                    "resistance_pressure_score": 0.24,
                    "support_pressure_score": 0.22,
                    "ema_regime_bias": 1.0,
                    "fast_slow_gap_pct": 0.16,
                    "slow_trend_gap_pct": 0.28,
                }
            ]
        )
        with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "market_reading"), mock.patch(
            "services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame
        ):
            result = engine.decide_entry(self._build_frame(), StrategyParams())
        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["decision_source"], "market_reading_ai")
        self.assertEqual(result["setup"]["setup"], "market_reading_long")

    def test_market_reading_short_blocks_near_support(self):
        engine = UnifiedDecisionEngine(
            symbol="BTC/USDT",
            timeframe="15m",
            ai_model=_StubAIModel("sell", {"short": 0.67, "hold": 0.19, "long": 0.14}, 0.67),
            market_context_service=_StubMarketContextService(),
            use_live_context=False,
        )
        feature_frame = pd.DataFrame(
            [
                {
                    "atr": 0.39,
                    "adx": 31.0,
                    "trend_regime_score": 0.52,
                    "range_regime_score": 0.36,
                    "channel_position_32": 0.08,
                    "distance_to_rolling_high_pct": 1.05,
                    "distance_to_rolling_low_pct": 0.18,
                    "resistance_pressure_score": 0.15,
                    "support_pressure_score": 0.88,
                    "ema_regime_bias": -1.0,
                    "fast_slow_gap_pct": -0.18,
                    "slow_trend_gap_pct": -0.26,
                }
            ]
        )
        with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "market_reading"), mock.patch(
            "services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame
        ), mock.patch(
            "services.unified_decision_engine.detect_market_regime",
            return_value={"regime": "trend_bear"},
        ):
            result = engine.decide_entry(self._build_frame(), StrategyParams())
        self.assertEqual(result["signal"], "hold")
        self.assertIn("market_reading_short_near_support", str(result["reason"]))

    def test_market_reading_blocks_countertrend_long_in_bear_context(self):
        engine = UnifiedDecisionEngine(
            symbol="BTC/USDT",
            timeframe="15m",
            ai_model=_StubAIModel("buy", {"long": 0.58, "hold": 0.24, "short": 0.18}, 0.58),
            market_context_service=_StubMarketContextService(),
            use_live_context=False,
        )
        feature_frame = pd.DataFrame(
            [
                {
                    "atr": 0.38,
                    "adx": 47.0,
                    "trend_regime_score": 0.61,
                    "range_regime_score": 0.34,
                    "channel_position_32": 0.41,
                    "distance_to_rolling_high_pct": 0.96,
                    "distance_to_rolling_low_pct": 0.66,
                    "resistance_pressure_score": 0.26,
                    "support_pressure_score": 0.34,
                    "ema_regime_bias": -1.0,
                    "fast_slow_gap_pct": -0.22,
                    "slow_trend_gap_pct": -0.31,
                    "rsi": 33.0,
                }
            ]
        )
        with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "market_reading"), mock.patch(
            "services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame
        ), mock.patch(
            "services.unified_decision_engine.detect_market_regime",
            return_value={"regime": "trend_bear"},
        ):
            result = engine.decide_entry(self._build_frame(), StrategyParams())
        self.assertEqual(result["signal"], "hold")
        self.assertIn("market_reading_long_countertrend_blocked", str(result["reason"]))

    def test_market_reading_does_not_force_direction_when_hold_still_dominates(self):
        engine = UnifiedDecisionEngine(
            symbol="BTC/USDT",
            timeframe="15m",
            ai_model=_StubAIModel("hold", {"long": 0.41, "hold": 0.43, "short": 0.16}, 0.43),
            market_context_service=_StubMarketContextService(),
            use_live_context=False,
        )
        feature_frame = pd.DataFrame(
            [
                {
                    "atr": 0.40,
                    "adx": 28.0,
                    "trend_regime_score": 0.57,
                    "range_regime_score": 0.36,
                    "channel_position_32": 0.46,
                    "distance_to_rolling_high_pct": 0.88,
                    "distance_to_rolling_low_pct": 0.79,
                    "resistance_pressure_score": 0.28,
                    "support_pressure_score": 0.25,
                    "ema_regime_bias": 1.0,
                    "fast_slow_gap_pct": 0.14,
                    "slow_trend_gap_pct": 0.25,
                }
            ]
        )
        with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "market_reading"), mock.patch(
            "services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame
        ):
            result = engine.decide_entry(self._build_frame(), StrategyParams())
        self.assertEqual(result["signal"], "hold")
        self.assertIn("market_reading_hold_ai_signal", str(result["reason"]))

    def test_market_reading_learning_guard_blocks_degraded_long_side(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            learning = AdaptiveLearningService(
                f"{tmpdir}/memory.json",
                enabled=True,
                min_trades=1,
                max_bias=0.12,
            )
            for value in (-1.2, -0.9, -0.8, -1.0, -0.7, -0.9, -0.6, -1.1, -0.8, -0.9):
                learning.register_trade(
                    symbol="BTC/USDT",
                    timeframe="15m",
                    side="long",
                    setup_name="market_reading_long",
                    net_pct=value,
                )
            engine = UnifiedDecisionEngine(
                symbol="BTC/USDT",
                timeframe="15m",
                ai_model=_StubAIModel("buy", {"long": 0.62, "hold": 0.21, "short": 0.17}, 0.62),
                market_context_service=_StubMarketContextService(),
                learning_service=learning,
                use_live_context=False,
            )
            feature_frame = pd.DataFrame(
                [
                    {
                        "atr": 0.40,
                        "adx": 29.0,
                        "trend_regime_score": 0.56,
                        "range_regime_score": 0.40,
                        "channel_position_32": 0.45,
                        "distance_to_rolling_high_pct": 0.90,
                        "distance_to_rolling_low_pct": 0.70,
                        "resistance_pressure_score": 0.22,
                        "support_pressure_score": 0.18,
                        "ema_regime_bias": 0.0,
                        "fast_slow_gap_pct": 0.04,
                        "slow_trend_gap_pct": 0.01,
                        "rsi": 42.0,
                    }
                ]
            )
            with mock.patch.object(config.ProductionConfig, "AI_ASSIST_MODE", "market_reading"), mock.patch(
                "services.unified_decision_engine.prepare_feature_frame", return_value=feature_frame
            ), mock.patch(
                "services.unified_decision_engine.detect_market_regime",
                return_value={"regime": "range"},
            ):
                result = engine.decide_entry(self._build_frame(), StrategyParams())
            self.assertEqual(result["signal"], "hold")
            self.assertIn("market_reading_long_learning_guard_blocked", str(result["reason"]))

    def test_ai_model_can_exit_long_near_resistance_in_range(self):
        model = object.__new__(AIModel)
        feature_row = pd.Series(
            {
                "close": 101.1,
                "range_regime_score": 0.74,
                "trend_regime_score": 0.22,
                "channel_position_32": 0.93,
                "distance_to_rolling_high_pct": 0.18,
                "distance_to_rolling_low_pct": 1.20,
                "resistance_pressure_score": 0.91,
                "support_pressure_score": 0.09,
                "rsi": 68.0,
                "rsi_delta": -1.8,
                "adx_delta": -2.4,
            }
        )
        ai_decision = {
            "signal": "hold",
            "confidence": 0.58,
            "probabilities": {"short": 0.41, "hold": 0.44, "long": 0.15},
        }
        result = model.should_exit_position(
            position={"side": "long", "entry_price": 100.0},
            ai_decision=ai_decision,
            min_confidence=0.45,
            feature_row=feature_row,
            market_context={"bias": {"caution_bias": 0.08}},
            learning_bias=-0.06,
        )
        self.assertTrue(result["exit"])
        self.assertIn("resistance", str(result["reason"]))

    def test_pending_closed_indexes_ignore_open_current_candle(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-06-17T10:00:00+00:00"),
                    "is_closed": True,
                },
                {
                    "timestamp": pd.Timestamp("2026-06-17T10:15:00+00:00"),
                    "is_closed": False,
                },
            ]
        )
        pending = bot_runner._get_pending_candle_indexes(df, pd.Timestamp("2026-06-17T10:00:00+00:00"))
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
