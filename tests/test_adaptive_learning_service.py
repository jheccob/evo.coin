import tempfile
import unittest
from pathlib import Path

from services.adaptive_learning_service import AdaptiveLearningService


class FakeLearningDatabase:
    def __init__(self, initial=None, fail_save=False):
        self.payload = initial
        self.fail_save = fail_save
        self.saved = []

    def get_ai_learning_memory(self, memory_key):
        return self.payload

    def save_ai_learning_memory(self, memory_key, payload):
        if self.fail_save:
            raise RuntimeError("database offline")
        self.saved.append((memory_key, payload))
        self.payload = payload
        return True


class AdaptiveLearningServiceTests(unittest.TestCase):
    def test_loads_existing_memory_from_database(self):
        database = FakeLearningDatabase(
            {
                "updated_at_utc": "2026-07-04T00:00:00+00:00",
                "stats": {
                    "BTC/USDT|15m|long|__all__": {
                        "trades": 6,
                        "wins": 4,
                        "losses": 2,
                        "net_sum_pct": 3.0,
                        "avg_net_pct": 0.5,
                        "win_rate_pct": 66.6667,
                    }
                },
            }
        )

        service = AdaptiveLearningService(
            "unused.json",
            database=database,
            memory_key="BTC/USDT|15m",
            min_trades=1,
        )

        bias = service.get_bias(
            symbol="BTC/USDT",
            timeframe="15m",
            side="long",
            setup_name="unknown",
        )

        self.assertEqual(bias.trade_count, 6)
        self.assertGreater(bias.bias, 0.0)

    def test_saves_trade_memory_to_database(self):
        database = FakeLearningDatabase()
        service = AdaptiveLearningService(
            "unused.json",
            database=database,
            memory_key="BTC/USDT|15m",
            min_trades=1,
        )

        service.register_trade(
            symbol="BTC/USDT",
            timeframe="15m",
            side="long",
            setup_name="trend_resume_long",
            net_pct=1.2,
        )

        self.assertEqual(database.saved[-1][0], "BTC/USDT|15m")
        self.assertIn("BTC/USDT|15m|long|trend_resume_long", database.payload["stats"])

    def test_falls_back_to_file_when_database_save_fails(self):
        database = FakeLearningDatabase(fail_save=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "memory.json"
            service = AdaptiveLearningService(
                path,
                database=database,
                memory_key="BTC/USDT|15m",
                min_trades=1,
            )

            service.register_trade(
                symbol="BTC/USDT",
                timeframe="15m",
                side="long",
                setup_name="trend_resume_long",
                net_pct=1.2,
            )

            self.assertTrue(path.exists())
            self.assertIn("trend_resume_long", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
