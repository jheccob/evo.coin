from __future__ import annotations

from typing import Optional

import pandas as pd

from market_data import fetch_candles


WEBSOCKETS_AVAILABLE = False


class StreamlinedTradingBot:
    """
    Fallback compatível com a interface websocket.
    Usa REST em modo pull quando websocket não está disponível.
    """

    def __init__(self, symbol: str, timeframe: str, max_candles: int = 500):
        self.symbol = symbol
        self.timeframe = timeframe
        self.max_candles = max(200, int(max_candles))
        self.provider = "rest_fallback"
        self.last_error: Optional[str] = None

    def stop(self) -> None:
        return None

    def get_current_status(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "provider": self.provider,
            "connected": False,
            "candles": None,
            "last_price": None,
            "last_message_age_sec": None,
            "last_closed_timestamp": None,
            "last_error": self.last_error,
        }

    def get_market_data(self, limit: int = 200, timeout: float = 20.0, include_current_candle: bool = False) -> pd.DataFrame:
        del timeout, include_current_candle
        requested = max(50, min(int(limit or 200), self.max_candles))
        df = fetch_candles(self.symbol, self.timeframe, limit=requested)
        if "is_closed" not in df.columns:
            df["is_closed"] = True
        return df
