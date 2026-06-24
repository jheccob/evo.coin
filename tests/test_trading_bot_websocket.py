from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

import trading_bot_websocket


def _build_candle_frame(start: str, periods: int, *, price_start: float) -> pd.DataFrame:
    timestamps = pd.date_range(start=start, periods=periods, freq="15min", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        price = price_start + index
        rows.append(
            {
                "timestamp": timestamp,
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.5,
                "volume": 1000 + index,
            }
        )
    return pd.DataFrame(rows)


class StreamlinedTradingBotTests(unittest.TestCase):
    def test_get_market_data_refreshes_stale_bootstrap_via_rest(self):
        stale_df = _build_candle_frame("2026-04-03T02:45:00Z", 4, price_start=100.0)
        fresh_df = _build_candle_frame("2026-05-30T04:45:00Z", 4, price_start=200.0)
        now_epoch = pd.Timestamp("2026-05-30T05:30:00Z").timestamp()

        with mock.patch.object(trading_bot_websocket, "WEBSOCKETS_AVAILABLE", False):
            with mock.patch.object(trading_bot_websocket, "connect", None):
                with mock.patch.object(
                    trading_bot_websocket,
                    "fetch_candles",
                    return_value=fresh_df,
                ) as fetch_mock:
                    with mock.patch.object(trading_bot_websocket.time, "time", return_value=now_epoch):
                        bot = trading_bot_websocket.StreamlinedTradingBot(
                            symbol="BTC/USDT",
                            timeframe="15m",
                            allow_rest_fallback=True,
                            bootstrap_df=stale_df,
                        )
                        result = bot.get_market_data(limit=4, timeout=0, include_current_candle=False)

        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(result["timestamp"].iloc[-1], fresh_df["timestamp"].iloc[-1])
        self.assertGreater(result["close"].iloc[-1], stale_df["close"].iloc[-1])

    def test_get_market_data_raises_when_rest_refresh_remains_stale(self):
        stale_df = _build_candle_frame("2026-04-03T02:45:00Z", 4, price_start=100.0)
        now_epoch = pd.Timestamp("2026-05-30T05:30:00Z").timestamp()

        with mock.patch.object(trading_bot_websocket, "WEBSOCKETS_AVAILABLE", False):
            with mock.patch.object(trading_bot_websocket, "connect", None):
                with mock.patch.object(
                    trading_bot_websocket,
                    "fetch_candles",
                    return_value=stale_df.copy(),
                ):
                    with mock.patch.object(trading_bot_websocket.time, "time", return_value=now_epoch):
                        bot = trading_bot_websocket.StreamlinedTradingBot(
                            symbol="BTC/USDT",
                            timeframe="15m",
                            allow_rest_fallback=True,
                            bootstrap_df=stale_df,
                        )
                        with self.assertRaisesRegex(RuntimeError, "desatualizado"):
                            bot.get_market_data(limit=4, timeout=0, include_current_candle=False)


if __name__ == "__main__":
    unittest.main()
