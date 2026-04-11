from __future__ import annotations

import ccxt
import pandas as pd



def get_exchange() -> ccxt.Exchange:
    return ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})



def _candles_to_dataframe(ohlcv: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("Int64")
    df = df.dropna().reset_index(drop=True)
    return df



def fetch_historical_candles(
    symbol: str,
    timeframe: str,
    total_limit: int = 2000,
    batch_limit: int = 500,
) -> pd.DataFrame:
    exchange = get_exchange()
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    since = exchange.milliseconds() - (total_limit * timeframe_ms)
    all_ohlcv: list[list[float]] = []

    while len(all_ohlcv) < total_limit:
        current_limit = min(batch_limit, total_limit - len(all_ohlcv))
        batch = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=current_limit,
        )
        if not batch:
            break

        all_ohlcv.extend(batch)
        since = int(batch[-1][0]) + timeframe_ms

        if len(batch) < current_limit:
            break

    unique = {int(row[0]): row for row in all_ohlcv}
    ordered = [unique[key] for key in sorted(unique)]
    return _candles_to_dataframe(ordered[-total_limit:])


def fetch_candles(
    symbol: str,
    timeframe: str,
    limit: int = 500,
    testnet: bool = False,
) -> pd.DataFrame:
    del testnet
    exchange = get_exchange()
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return _candles_to_dataframe(ohlcv)
