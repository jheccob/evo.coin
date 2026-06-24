from __future__ import annotations

import unittest

from bot_runner import _resolve_runtime_signal_decision
from services.market_context_service import MarketContextService


class MarketContextServiceTests(unittest.TestCase):
    def test_bias_summary_favors_long_on_extreme_fear_and_positive_news(self):
        service = MarketContextService(ttl_sec=60)
        bias = service._build_bias_summary(  # noqa: SLF001 - tested on purpose
            {"available": True, "value": 18, "classification": "Extreme Fear"},
            {"available": True, "sentiment_score": 0.72, "headline_count": 5},
        )

        self.assertGreater(bias["long_bias"], 0.0)
        self.assertEqual(bias["short_bias"], 0.0)
        self.assertIn("fear_greed_extreme_fear", bias["reasons"])
        self.assertIn("news_positive", bias["reasons"])

    def test_runtime_signal_decision_uses_ai_full_when_confident(self):
        engine_result = {"signal": "hold", "reason": "sem gatilho", "setup": {}, "score": 0.0, "atr": 0.2}
        ai_decision = {
            "enabled": True,
            "signal": "buy",
            "label": "long",
            "confidence": 0.61,
            "reason": "ai_model:test",
            "probabilities": {"short": 0.12, "hold": 0.27, "long": 0.61},
            "context": {"bias": {"long_bias": 0.04, "short_bias": 0.0, "caution_bias": 0.0}},
        }

        resolved = _resolve_runtime_signal_decision(
            engine_result=engine_result,
            ai_decision=ai_decision,
            ai_mode="full",
            min_confidence=0.40,
        )

        self.assertEqual(resolved["signal"], "buy")
        self.assertEqual(resolved["decision_source"], "ai_full")
        self.assertEqual((resolved.get("setup") or {}).get("setup"), "ai_runtime_full")

    def test_runtime_signal_decision_blocks_engine_when_ai_filter_disagrees(self):
        engine_result = {"signal": "sell", "reason": "short score", "setup": {}, "score": 9.0, "atr": 0.3}
        ai_decision = {
            "enabled": True,
            "signal": "buy",
            "label": "long",
            "confidence": 0.58,
            "reason": "ai_model:test",
            "probabilities": {"short": 0.12, "hold": 0.30, "long": 0.58},
            "context": {},
        }

        resolved = _resolve_runtime_signal_decision(
            engine_result=engine_result,
            ai_decision=ai_decision,
            ai_mode="filter",
            min_confidence=0.40,
        )

        self.assertEqual(resolved["signal"], "hold")
        self.assertEqual(resolved["decision_source"], "ai_filter_blocked")


if __name__ == "__main__":
    unittest.main()
