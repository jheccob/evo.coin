from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

import config
from strategy_engine import (
    StrategyParams,
    calculate_indicators as engine_calculate_indicators,
    generate_entry_signal,
)


def _ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "timestamp" in out.columns:
        if pd.api.types.is_numeric_dtype(out["timestamp"]):
            out["timestamp"] = pd.to_datetime(out["timestamp"], unit="ms", utc=True, errors="coerce")
        else:
            out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        out = out.set_index("timestamp")
    else:
        out.index = pd.to_datetime(out.index, utc=True, errors="coerce")
    return out.sort_index()


def _build_params(
    buy_rsi_threshold: Optional[float] = None,
    sell_rsi_threshold: Optional[float] = None,
) -> StrategyParams:
    return StrategyParams(
        buy_rsi_floor=float(
            config.BUY_RSI_SIGNAL if buy_rsi_threshold is None else buy_rsi_threshold
        ),
        sell_rsi_ceiling=float(
            config.SELL_RSI_SIGNAL if sell_rsi_threshold is None else sell_rsi_threshold
        ),
    )


def prepare_candle_features(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_datetime_index(df)

    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close", "volume"])

    prepared = engine_calculate_indicators(out, _build_params())
    if "is_closed" not in prepared.columns:
        prepared["is_closed"] = True
    prepared["is_closed"] = prepared["is_closed"].fillna(True).astype(bool)
    return prepared


def analyze_prepared_candle(
    df: pd.DataFrame,
    index: int = -1,
    buy_rsi_threshold: Optional[float] = None,
    sell_rsi_threshold: Optional[float] = None,
) -> Dict[str, str]:
    if df is None or df.empty:
        return {"signal": "hold", "reason": "dados insuficientes"}

    resolved_df = df.copy()
    if index != -1:
        effective_index = len(resolved_df) + index if index < 0 else index
        effective_index = max(min(effective_index, len(resolved_df) - 1), 0)
        resolved_df = resolved_df.iloc[: effective_index + 1].copy()
    if len(resolved_df) < 3:
        return {"signal": "hold", "reason": "dados insuficientes"}

    required_columns = {"ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "atr_pct"}
    if not required_columns.issubset(set(resolved_df.columns)):
        ohlcv_columns = {"open", "high", "low", "close", "volume"}
        if not ohlcv_columns.issubset(set(resolved_df.columns)):
            return {"signal": "hold", "reason": "dados insuficientes"}
        resolved_df = prepare_candle_features(resolved_df)

    result = generate_entry_signal(
        resolved_df,
        _build_params(
            buy_rsi_threshold=buy_rsi_threshold,
            sell_rsi_threshold=sell_rsi_threshold,
        ),
        index=-1,
    )
    return {
        "signal": str(result.get("signal") or "hold"),
        "reason": str(result.get("reason") or "sem gatilho"),
    }
