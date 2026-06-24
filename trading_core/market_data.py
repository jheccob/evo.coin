from __future__ import annotations

import time
from typing import Iterable, Optional

import pandas as pd

from config import AppConfig
import config as runtime_config
from market_data import fetch_candles
from strategy_engine import StrategyParams, calculate_indicators as engine_calculate_indicators
from trading_bot_websocket import StreamlinedTradingBot, WEBSOCKETS_AVAILABLE


class RestFallbackStreamClient:
    def __init__(self, symbol: str, timeframe: str):
        self.symbol = symbol
        self.timeframe = timeframe
        self.provider = "rest_fallback"
        self.last_error = "Pacote websockets nao instalado; usando REST."

    def stop(self):
        return None

    def get_current_status(self):
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

    def get_market_data(self, limit: int = 200, timeout: float = 20.0, include_current_candle: bool = False):
        del timeout, include_current_candle
        use_testnet = bool(getattr(runtime_config, "TESTNET", False))
        return fetch_candles(self.symbol, self.timeframe, limit=limit, testnet=use_testnet)


def _stream_key(symbol: str, timeframe: str) -> str:
    return f"{str(symbol or '').upper()}::{str(timeframe or '').lower()}"


def _ensure_registry(bot) -> dict:
    registry = getattr(bot, "_stream_clients", None)
    if not isinstance(registry, dict):
        registry = {}
        bot._stream_clients = registry
    return registry


def _stop_client_safely(client) -> None:
    stop = getattr(client, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            pass


def cleanup_stream_clients(
    bot,
    keep_keys: Optional[Iterable[str]] = None,
    stale_after_seconds: int = 180,
    max_clients: int = 8,
):
    registry = _ensure_registry(bot)
    keep_keys = set(keep_keys or [])
    now_ts = time.time()

    stale_keys: list[str] = []
    for key, payload in list(registry.items()):
        if key in keep_keys:
            continue
        last_used_at = float(payload.get("last_used_at") or 0.0)
        if stale_after_seconds > 0 and last_used_at and now_ts - last_used_at > stale_after_seconds:
            stale_keys.append(key)

    for key in stale_keys:
        payload = registry.pop(key, None) or {}
        _stop_client_safely(payload.get("client"))

    if max_clients <= 0 or len(registry) <= max_clients:
        return

    removable = sorted(
        (
            (key, float(payload.get("last_used_at") or 0.0))
            for key, payload in registry.items()
            if key not in keep_keys
        ),
        key=lambda item: item[1],
    )
    while len(registry) > max_clients and removable:
        key, _ = removable.pop(0)
        payload = registry.pop(key, None) or {}
        _stop_client_safely(payload.get("client"))


def reset_stream_client(bot, symbol: Optional[str] = None, timeframe: Optional[str] = None):
    resolved_symbol = symbol or bot.symbol
    resolved_timeframe = timeframe or bot.timeframe
    registry = _ensure_registry(bot)
    key = _stream_key(resolved_symbol, resolved_timeframe)
    payload = registry.pop(key, None) or {}
    _stop_client_safely(payload.get("client"))


def get_realtime_stream_client(bot, symbol: Optional[str] = None, timeframe: Optional[str] = None):
    resolved_symbol = symbol or bot.symbol
    resolved_timeframe = timeframe or bot.timeframe
    registry = _ensure_registry(bot)
    key = _stream_key(resolved_symbol, resolved_timeframe)
    payload = registry.get(key)
    if payload and payload.get("client") is not None:
        payload["last_used_at"] = time.time()
        return payload["client"]

    cleanup_stream_clients(
        bot,
        keep_keys={key},
        stale_after_seconds=getattr(bot, "STREAM_CLIENT_STALE_SECONDS", 180),
        max_clients=getattr(bot, "MAX_STREAM_CLIENTS", 8),
    )
    if WEBSOCKETS_AVAILABLE:
        client = StreamlinedTradingBot(
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            max_candles=max(int(getattr(AppConfig, "MAX_CANDLES", 1000) or 1000), 250),
            testnet=bool(getattr(runtime_config, "TESTNET", False)),
        )
    else:
        client = RestFallbackStreamClient(symbol=resolved_symbol, timeframe=resolved_timeframe)
    registry[key] = {
        "client": client,
        "last_used_at": time.time(),
        "symbol": resolved_symbol,
        "timeframe": resolved_timeframe,
    }
    return client


def _coerce_market_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    working_df = df.copy()
    if "timestamp" in working_df.columns:
        if pd.api.types.is_numeric_dtype(working_df["timestamp"]):
            working_df["timestamp"] = pd.to_datetime(working_df["timestamp"], unit="ms", utc=True)
        else:
            working_df["timestamp"] = pd.to_datetime(working_df["timestamp"], utc=True, errors="coerce")
        working_df = working_df.set_index("timestamp")
    else:
        working_df.index = pd.to_datetime(working_df.index, utc=True, errors="coerce")

    for column in ("open", "high", "low", "close", "volume"):
        if column in working_df.columns:
            working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

    working_df = working_df.dropna(subset=["open", "high", "low", "close", "volume"])
    working_df = working_df.sort_index()
    if "is_closed" not in working_df.columns:
        working_df["is_closed"] = True
    working_df["is_closed"] = working_df["is_closed"].fillna(True).astype(bool)
    return working_df


def _build_strategy_params() -> StrategyParams:
    return StrategyParams(
        buy_rsi_floor=float(runtime_config.BUY_RSI_SIGNAL),
        sell_rsi_ceiling=float(runtime_config.SELL_RSI_SIGNAL),
    )


def calculate_indicators(bot, df: pd.DataFrame) -> pd.DataFrame:
    del bot
    working_df = _coerce_market_dataframe(df)
    prepared = engine_calculate_indicators(working_df, _build_strategy_params())
    if "is_closed" not in prepared.columns:
        prepared["is_closed"] = True
    prepared["is_closed"] = prepared["is_closed"].fillna(True).astype(bool)

    ema12 = prepared["close"].ewm(span=12, adjust=False).mean()
    ema26 = prepared["close"].ewm(span=26, adjust=False).mean()
    prepared["macd"] = ema12 - ema26
    prepared["macd_signal"] = prepared["macd"].ewm(span=9, adjust=False).mean()
    prepared["volume_ma"] = prepared["volume"].rolling(20).mean()
    prepared["sma_21"] = prepared["close"].rolling(21).mean()

    prepared["market_regime"] = "range"
    bullish = (prepared["ema_fast"] > prepared["ema_slow"]) & (prepared["ema_slow"] > prepared["ema_trend"])
    bearish = (prepared["ema_fast"] < prepared["ema_slow"]) & (prepared["ema_slow"] < prepared["ema_trend"])
    prepared.loc[bullish, "market_regime"] = "bullish"
    prepared.loc[bearish, "market_regime"] = "bearish"

    rsi_distance = (pd.to_numeric(prepared["rsi"], errors="coerce") - 50.0).abs().fillna(0.0)
    macd_strength = (
        pd.to_numeric(prepared["macd"], errors="coerce")
        - pd.to_numeric(prepared["macd_signal"], errors="coerce")
    ).abs().fillna(0.0)
    prepared["signal_confidence"] = (rsi_distance.clip(upper=35) / 35 * 60) + (macd_strength.clip(upper=1.5) / 1.5 * 40)
    prepared["signal_confidence"] = prepared["signal_confidence"].clip(lower=0.0, upper=100.0)
    return prepared


def get_market_data(bot, limit: int = 200, symbol: Optional[str] = None, timeframe: Optional[str] = None):
    resolved_symbol = symbol or bot.symbol
    resolved_timeframe = timeframe or bot.timeframe
    client = get_realtime_stream_client(bot, symbol=resolved_symbol, timeframe=resolved_timeframe)
    if client is None:
        use_testnet = bool(getattr(runtime_config, "TESTNET", False))
        df = fetch_candles(resolved_symbol, resolved_timeframe, limit=limit, testnet=use_testnet)
        return calculate_indicators(bot, df)
    try:
        df = client.get_market_data(limit=limit)
    except Exception:
        use_testnet = bool(getattr(runtime_config, "TESTNET", False))
        df = fetch_candles(resolved_symbol, resolved_timeframe, limit=limit, testnet=use_testnet)
    return calculate_indicators(bot, df)
