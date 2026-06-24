from __future__ import annotations

import unittest

from live_go_live_check import evaluate_backtest_gate


class LiveGoLiveCheckTests(unittest.TestCase):
    def test_evaluate_backtest_gate_passes_conservative_threshold(self):
        summary = {
            "trades": 110,
            "profit_factor": 1.49,
            "max_drawdown": 6.35,
            "net_pct": 43.9,
            "long_stats": {"net": 22.72},
            "short_stats": {"net": 21.18},
        }

        result = evaluate_backtest_gate(summary)

        self.assertEqual(result.status, "PASS")

    def test_evaluate_backtest_gate_warns_when_positive_but_below_bar(self):
        summary = {
            "trades": 70,
            "profit_factor": 1.05,
            "max_drawdown": 14.0,
            "net_pct": 4.0,
            "long_stats": {"net": 3.0},
            "short_stats": {"net": 1.0},
        }

        result = evaluate_backtest_gate(summary)

        self.assertEqual(result.status, "WARN")


if __name__ == "__main__":
    unittest.main()
