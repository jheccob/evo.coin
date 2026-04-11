from __future__ import annotations

from typing import Dict

import pandas as pd


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


def _compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / max(period, 1), adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / max(period, 1), adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(max(period, 1)).mean()


def prepare_candle_features(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_datetime_index(df)

    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close", "volume"])

    out["ema_fast"] = out["close"].ewm(span=9, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=21, adjust=False).mean()
    out["ema_trend"] = out["close"].ewm(span=50, adjust=False).mean()
    out["rsi"] = _compute_rsi(out["close"], period=14)
    out["atr"] = _compute_atr(out, period=14)
    out["atr_pct"] = out["atr"] / out["close"].replace(0, pd.NA) * 100

    if "is_closed" not in out.columns:
        out["is_closed"] = True
    out["is_closed"] = out["is_closed"].fillna(True).astype(bool)
    return out


def analyze_prepared_candle(
    df: pd.DataFrame,
    index: int = -1,
    buy_rsi_threshold: float = 54.0,
    sell_rsi_threshold: float = 47.0,
) -> Dict[str, str]:
    if df is None or df.empty or len(df) < 3:
        return {"signal": "hold", "reason": "dados insuficientes"}

    row = df.iloc[index]
    prev = df.iloc[index - 1]

    bullish_structure = row["close"] > row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
    bearish_structure = row["close"] < row["ema_fast"] < row["ema_slow"] < row["ema_trend"]

    bullish_trigger = float(prev["rsi"]) <= float(buy_rsi_threshold) < float(row["rsi"])
    bearish_trigger = float(prev["rsi"]) >= float(sell_rsi_threshold) > float(row["rsi"])

    if bullish_structure and bullish_trigger:
        return {"signal": "buy", "reason": "trend_resume_long confirmado"}
    if bearish_structure and bearish_trigger:
        return {"signal": "sell", "reason": "trend_resume_short confirmado"}
    return {"signal": "hold", "reason": "sem gatilho confirmado"}

