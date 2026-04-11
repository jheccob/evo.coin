from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import pandas as pd

import config


@dataclass
class StrategyParams:
    ema_fast: int = config.FAST_EMA
    ema_slow: int = config.SLOW_EMA
    ema_trend: int = config.TREND_EMA
    rsi_period: int = config.RSI_PERIOD
    atr_period: int = config.ATR_PERIOD
    buy_rsi_floor: float = float(config.BUY_RSI_SIGNAL)
    sell_rsi_ceiling: float = float(config.SELL_RSI_SIGNAL)
    long_min_atr_pct: float = float(config.LONG_MIN_ATR_PCT)
    short_min_atr_pct: float = float(config.SHORT_MIN_ATR_PCT)
    long_regime_gap_pct: float = float(config.LONG_TREND_GAP_PCT)
    short_regime_gap_pct: float = float(config.SHORT_TREND_GAP_PCT)
    pullback_buffer_pct: float = 0.12
    long_partial_pct: float = float(config.LONG_TAKE_PROFIT_PCT) * 0.55
    short_partial_pct: float = float(config.SHORT_TAKE_PROFIT_PCT) * 0.55
    long_stop_pct: float = float(config.LONG_STOP_LOSS_PCT)
    short_stop_pct: float = float(config.SHORT_STOP_LOSS_PCT)
    long_trailing_pct: float = float(config.LONG_TRAILING_STOP_PCT)
    short_trailing_pct: float = float(config.SHORT_TRAILING_STOP_PCT)


def calculate_indicators(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=params.ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=params.ema_slow, adjust=False).mean()
    out["ema_trend"] = out["close"].ewm(span=params.ema_trend, adjust=False).mean()

    delta = out["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(params.rsi_period).mean()
    avg_loss = losses.rolling(params.rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(params.atr_period).mean()
    out["atr_pct"] = out["atr"] / out["close"] * 100
    return out


def detect_market_regime(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Dict[str, object]:
    row = df.iloc[index]
    ema_gap_pct = abs(row["ema_slow"] - row["ema_trend"]) / row["close"] * 100
    bullish = row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
    bearish = row["ema_fast"] < row["ema_slow"] < row["ema_trend"]
    long_tradeable = bool(row["atr_pct"] >= params.long_min_atr_pct)
    short_tradeable = bool(row["atr_pct"] >= params.short_min_atr_pct)

    if bullish and ema_gap_pct >= params.long_regime_gap_pct and long_tradeable:
        regime = "trend_bull"
    elif bearish and ema_gap_pct >= params.short_regime_gap_pct and short_tradeable:
        regime = "trend_bear"
    elif bullish:
        regime = "weak_bull"
    elif bearish:
        regime = "weak_bear"
    else:
        regime = "range"

    return {
        "regime": regime,
        "tradeable_long": long_tradeable,
        "tradeable_short": short_tradeable,
        "ema_gap_pct": round(float(ema_gap_pct), 4),
        "atr_pct": round(float(row["atr_pct"]), 4),
    }


def detect_setup(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Dict[str, object]:
    row = df.iloc[index]
    regime = detect_market_regime(df, params, index=index)

    pullback_long = row["low"] <= row["ema_fast"] * (1 + params.pullback_buffer_pct / 100)
    pullback_short = row["high"] >= row["ema_fast"] * (1 - params.pullback_buffer_pct / 100)

    if regime["regime"] == "trend_bull" and pullback_long:
        return {"setup": "pullback_long", "direction": "long", "regime": regime}
    if regime["regime"] == "trend_bear" and pullback_short:
        return {"setup": "pullback_short", "direction": "short", "regime": regime}
    if regime["regime"] == "trend_bull":
        return {"setup": "trend_resume_long", "direction": "long", "regime": regime}
    if regime["regime"] == "trend_bear":
        return {"setup": "trend_resume_short", "direction": "short", "regime": regime}
    return {"setup": None, "direction": None, "regime": regime}


def generate_entry_signal(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Dict[str, object]:
    min_rows = max(params.ema_trend + 5, params.rsi_period + 5, params.atr_period + 5)

    if len(df) < min_rows:
        return {"signal": "hold", "reason": "dados insuficientes"}

    row = df.iloc[index]
    prev = df.iloc[index - 1]

    setup = detect_setup(df, params, index=index)
    direction = setup["direction"]
    regime_name = str((setup.get("regime") or {}).get("regime") or "").strip().lower()

    if bool(getattr(config, "BLOCK_UNKNOWN_REGIME", True)) and regime_name in {"", "unknown", "none", "null"}:
        return {
            "signal": "hold",
            "reason": "regime unknown bloqueado",
            "setup": setup,
        }

    trend_strength_pct = (
        abs(row["ema_fast"] - row["ema_slow"]) / row["close"] * 100
    )

    trend_context_pct = (
        abs(row["ema_slow"] - row["ema_trend"]) / row["close"] * 100
    )

    if direction == "long":
        if not setup["regime"]["tradeable_long"]:

            return {
                "signal": "hold",
                "reason": "volatilidade fraca para long",
                "setup": setup,
            }
        if trend_context_pct < 0.10: 
            return {
                "signal": "hold",
                "reason": "mercado lateral fraco",
                "setup": setup,
            }


        if trend_strength_pct < config.MIN_TREND_STRENGTH_PCT:
            return {
                "signal": "hold",
                "reason": "tendencia fraca para long",
                "setup": setup,
            }

        if trend_context_pct < params.long_regime_gap_pct:
            return {
                "signal": "hold",
                "reason": "contexto fraco para long",
                "setup": setup,
            }

        if setup.get("setup") == "pullback_long":
            min_pullback_strength = float(
                max(
                    config.MIN_TREND_STRENGTH_PCT,
                    float(
                        getattr(
                            config,
                            "LONG_PULLBACK_MIN_TREND_STRENGTH_PCT",
                            config.MIN_TREND_STRENGTH_PCT,
                        )
                    ),
                )
            )
            if trend_strength_pct < min_pullback_strength:
                return {
                    "signal": "hold",
                    "reason": "pullback_long tendencia fraca",
                    "setup": setup,
                }

        rsi_ok = row["rsi"] >= params.buy_rsi_floor

        candle_ok = (
            row["close"] > row["ema_fast"]
            and row["close"] > prev["close"]
        )

        structure_ok = (
            (row["ema_fast"] - row["ema_slow"]) / row["close"] * 100
            >= config.LONG_FAST_SLOW_GAP_PCT
        )

        if rsi_ok and candle_ok and structure_ok and config.ALLOW_LONG:
            return {
                "signal": "buy",
                "reason": f"{setup['setup']} confirmado",
                "setup": setup,
                "entry_price": float(row["close"]),
                "atr": float(row["atr"]),
            }

    if direction == "short":
        if not setup["regime"]["tradeable_short"]:
            return {
                "signal": "hold",
                "reason": "volatilidade fraca para short",
                "setup": setup,
            }
        
        if not config.ENABLE_SHORT_PULLBACK and setup.get("setup") == "pullback_short":
            return {"signal": "hold", "reason": "pullback_short bloqueado", "setup": setup}

        if not config.ENABLE_SHORT_RESUME and setup.get("setup") == "trend_resume_short":
            return {"signal": "hold", "reason": "trend_resume_short bloqueado", "setup": setup}

        rsi_ok = config.SHORT_RSI_MIN <= row["rsi"] <= params.sell_rsi_ceiling

        if trend_context_pct < 0.10:
            return { "signal": "hold", 
                    "reason": "mercado lateral fraco",
                     "setup": setup

            }

        if trend_strength_pct < config.MIN_TREND_STRENGTH_PCT_SHORT:
            return {
                "signal": "hold",
                "reason": "tendencia fraca para short",
                "setup": setup,
            }

        if trend_context_pct < params.short_regime_gap_pct:
            return {
                "signal": "hold",
                "reason": "contexto fraco para short",
                "setup": setup,
            }

        candle_ok = (
            row["close"] < row["ema_fast"]
            and row["close"] < prev["close"]
        )

        structure_ok = (
            (row["ema_slow"] - row["ema_fast"]) / row["close"] * 100
            >= config.SHORT_FAST_SLOW_GAP_PCT
        )

        if rsi_ok and candle_ok and structure_ok and config.ALLOW_SHORT:
            return {
                "signal": "sell",
                "reason": f"{setup['setup']} confirmado",
                "setup": setup,
                "entry_price": float(row["close"]),
                "atr": float(row["atr"]),
            }

    return {"signal": "hold", "reason": "sem gatilho", "setup": setup}
