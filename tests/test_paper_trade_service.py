import unittest
from unittest import mock

import pandas as pd

import config
from services.paper_trade_service import PaperTradeService


class _FakePaperTradeDatabase:
    def __init__(self):
        self.trades = []
        self._next_id = 1

    def get_recent_paper_trades(self, limit=5, symbol=None, timeframe=None, strategy_version=None):
        filtered = [
            trade
            for trade in self.trades
            if (symbol is None or trade.get("symbol") == symbol)
            and (timeframe is None or trade.get("timeframe") == timeframe)
            and (strategy_version is None or trade.get("strategy_version") == strategy_version)
        ]
        return list(reversed(filtered))[:limit]

    def get_open_paper_trades(self, symbol=None, timeframe=None, strategy_version=None):
        return [
            dict(trade)
            for trade in self.trades
            if trade.get("status") == "OPEN"
            and (symbol is None or trade.get("symbol") == symbol)
            and (timeframe is None or trade.get("timeframe") == timeframe)
            and (strategy_version is None or trade.get("strategy_version") == strategy_version)
        ]

    def create_paper_trade(self, trade_data):
        payload = dict(trade_data)
        payload["id"] = self._next_id
        self._next_id += 1
        self.trades.append(payload)
        return payload["id"]

    def update_paper_trade_management(
        self,
        trade_id,
        stop_loss_price=None,
        take_profit_price=None,
        break_even_active=False,
        trailing_active=False,
        protection_level=None,
        regime_exit_flag=False,
        structure_exit_flag=False,
        post_pump_protection=False,
        mfe_pct=0.0,
        mae_pct=0.0,
        max_unrealized_rr=0.0,
    ):
        trade = self._find_trade(trade_id)
        trade["stop_loss_price"] = stop_loss_price if stop_loss_price is not None else trade.get("stop_loss_price")
        trade["take_profit_price"] = take_profit_price if take_profit_price is not None else trade.get("take_profit_price")
        trade["final_stop_price"] = trade.get("stop_loss_price")
        trade["final_take_price"] = trade.get("take_profit_price")
        trade["break_even_active"] = int(bool(break_even_active))
        trade["trailing_active"] = int(bool(trailing_active))
        trade["protection_level"] = protection_level
        trade["regime_exit_flag"] = int(bool(regime_exit_flag))
        trade["structure_exit_flag"] = int(bool(structure_exit_flag))
        trade["post_pump_protection"] = int(bool(post_pump_protection))
        trade["mfe_pct"] = float(mfe_pct)
        trade["mae_pct"] = float(mae_pct)
        trade["max_unrealized_rr"] = float(max_unrealized_rr)

    def close_paper_trade(
        self,
        trade_id,
        exit_timestamp,
        exit_price,
        outcome,
        close_reason,
        result_pct,
        final_stop_price=None,
        final_take_price=None,
        break_even_active=False,
        trailing_active=False,
        protection_level=None,
        regime_exit_flag=False,
        structure_exit_flag=False,
        post_pump_protection=False,
        mfe_pct=0.0,
        mae_pct=0.0,
        max_unrealized_rr=0.0,
    ):
        trade = self._find_trade(trade_id)
        trade["status"] = "CLOSED"
        trade["exit_timestamp"] = exit_timestamp
        trade["exit_price"] = exit_price
        trade["outcome"] = outcome
        trade["close_reason"] = close_reason
        trade["exit_reason"] = close_reason
        trade["result_pct"] = float(result_pct)
        trade["final_stop_price"] = final_stop_price
        trade["final_take_price"] = final_take_price
        trade["break_even_active"] = int(bool(break_even_active))
        trade["trailing_active"] = int(bool(trailing_active))
        trade["protection_level"] = protection_level
        trade["regime_exit_flag"] = int(bool(regime_exit_flag))
        trade["structure_exit_flag"] = int(bool(structure_exit_flag))
        trade["post_pump_protection"] = int(bool(post_pump_protection))
        trade["mfe_pct"] = float(mfe_pct)
        trade["mae_pct"] = float(mae_pct)
        trade["max_unrealized_rr"] = float(max_unrealized_rr)

    def get_paper_trade_summary(self, symbol=None, timeframe=None):
        del symbol, timeframe
        return {}

    def _find_trade(self, trade_id):
        for trade in self.trades:
            if int(trade["id"]) == int(trade_id):
                return trade
        raise AssertionError(f"trade {trade_id} not found")


class PaperTradeServiceTests(unittest.TestCase):
    def setUp(self):
        self.database = _FakePaperTradeDatabase()
        self.service = PaperTradeService(database=self.database)
        self.symbol = "BTC/USDT"
        self.timeframe = "15m"
        self.entry_timestamp = pd.Timestamp("2026-05-24T12:00:00+00:00")

    def test_register_signal_uses_runtime_managed_profile_seed(self):
        with mock.patch.object(config, "EXECUTION_PROFILE", "managed"):
            trade_id = self.service.register_signal(
                symbol=self.symbol,
                timeframe=self.timeframe,
                signal="COMPRA",
                entry_price=100.0,
                entry_timestamp=self.entry_timestamp,
                stop_loss_pct=1.5,
                take_profit_pct=2.9,
                atr=1.0,
            )

        self.assertEqual(trade_id, 1)
        trade = self.database.trades[0]
        self.assertEqual(trade["execution_mode"], "managed")
        self.assertAlmostEqual(float(trade["entry_price"]), 100.02, places=4)
        self.assertAlmostEqual(float(trade["initial_stop_price"]), 98.5197, places=4)
        self.assertAlmostEqual(float(trade["initial_take_price"]), 101.0202, places=4)

    def test_register_signal_normalizes_sub_one_percent_stop_values(self):
        with mock.patch.object(config, "EXECUTION_PROFILE", "managed"):
            trade_id = self.service.register_signal(
                symbol=self.symbol,
                timeframe=self.timeframe,
                signal="COMPRA",
                entry_price=100.0,
                entry_timestamp=self.entry_timestamp,
                stop_loss_pct=0.8,
                take_profit_pct=1.6,
                atr=0.1,
            )

        self.assertEqual(trade_id, 1)
        trade = self.database.trades[0]
        self.assertAlmostEqual(float(trade["stop_loss_pct"]), 0.8, places=4)
        self.assertAlmostEqual(float(trade["take_profit_pct"]), 1.6, places=4)
        self.assertAlmostEqual(float(trade["initial_stop_price"]), 99.2198, places=4)
        self.assertAlmostEqual(float(trade["initial_take_price"]), 101.0202, places=4)

    def test_register_signal_keeps_existing_open_trade_without_flip(self):
        with mock.patch.object(config, "EXECUTION_PROFILE", "managed"):
            first_id = self.service.register_signal(
                symbol=self.symbol,
                timeframe=self.timeframe,
                signal="COMPRA",
                entry_price=100.0,
                entry_timestamp=self.entry_timestamp,
                stop_loss_pct=1.5,
                take_profit_pct=2.9,
                atr=1.0,
            )
            second_id = self.service.register_signal(
                symbol=self.symbol,
                timeframe=self.timeframe,
                signal="VENDA",
                entry_price=99.0,
                entry_timestamp=self.entry_timestamp + pd.Timedelta(minutes=15),
                stop_loss_pct=1.2,
                take_profit_pct=3.0,
                atr=1.0,
            )

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.database.trades), 1)
        self.assertEqual(self.database.trades[0]["status"], "OPEN")

    def test_evaluate_open_trades_replays_managed_position_engine(self):
        with mock.patch.object(config, "EXECUTION_PROFILE", "managed"):
            trade_id = self.service.register_signal(
                symbol=self.symbol,
                timeframe=self.timeframe,
                signal="COMPRA",
                entry_price=100.0,
                entry_timestamp=self.entry_timestamp,
                stop_loss_pct=1.5,
                take_profit_pct=2.9,
                atr=1.0,
            )

            first_candle = pd.DataFrame(
                [
                    {
                        "timestamp": self.entry_timestamp + pd.Timedelta(minutes=15),
                        "open": 100.0,
                        "high": 102.0,
                        "low": 99.8,
                        "close": 101.8,
                        "volume": 1000.0,
                        "is_closed": True,
                    }
                ]
            ).set_index("timestamp")

            closed_trades = self.service.evaluate_open_trades(
                symbol=self.symbol,
                timeframe=self.timeframe,
                market_data=first_candle,
            )
            self.assertEqual(closed_trades, [])

            open_trade = self.database._find_trade(trade_id)
            self.assertEqual(open_trade["status"], "OPEN")
            self.assertTrue(bool(open_trade["break_even_active"]))
            self.assertTrue(bool(open_trade["trailing_active"]))
            self.assertGreater(float(open_trade["stop_loss_price"]), float(open_trade["entry_price"]))

            second_candle = pd.DataFrame(
                [
                    {
                        "timestamp": self.entry_timestamp + pd.Timedelta(minutes=15),
                        "open": 100.0,
                        "high": 102.0,
                        "low": 99.8,
                        "close": 101.8,
                        "volume": 1000.0,
                        "is_closed": True,
                    },
                    {
                        "timestamp": self.entry_timestamp + pd.Timedelta(minutes=30),
                        "open": 101.8,
                        "high": 102.0,
                        "low": 101.0,
                        "close": 101.0,
                        "volume": 900.0,
                        "is_closed": True,
                    },
                ]
            ).set_index("timestamp")

            closed_trades = self.service.evaluate_open_trades(
                symbol=self.symbol,
                timeframe=self.timeframe,
                market_data=second_candle,
            )

        self.assertEqual(len(closed_trades), 1)
        closed_trade = self.database._find_trade(trade_id)
        self.assertEqual(closed_trade["status"], "CLOSED")
        self.assertEqual(closed_trade["close_reason"], "STOP_OR_TRAILING")
        self.assertTrue(bool(closed_trade["break_even_active"]))
        self.assertTrue(bool(closed_trade["trailing_active"]))
        self.assertGreater(float(closed_trade["result_pct"]), 0.0)


if __name__ == "__main__":
    unittest.main()
