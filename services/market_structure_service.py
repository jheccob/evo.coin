from __future__ import annotations

from typing import Any, Dict

import pandas as pd

import config


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return float(default)
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _window(df: pd.DataFrame, end_index: int, lookback: int, *, include_current: bool = True) -> pd.DataFrame:
    end = end_index + 1 if include_current else end_index
    start = max(end_index - max(int(lookback), 1) + (1 if include_current else 0), 0)
    return df.iloc[start:end]


def calculate_market_structure(df: pd.DataFrame, index: int = -1) -> Dict[str, float | bool]:
    effective_index = len(df) + index if index < 0 else index
    if effective_index < 0 or effective_index >= len(df):
        return {}

    row = df.iloc[effective_index]
    prev = df.iloc[max(effective_index - 1, 0)]
    close = _safe_float(row.get("close"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    open_price = _safe_float(row.get("open"), close)
    candle_range = max(high - low, 0.0)
    close_position = ((close - low) / candle_range) if candle_range > 0 else 0.5
    lower_wick = max(min(open_price, close) - low, 0.0)
    upper_wick = max(high - max(open_price, close), 0.0)
    lower_wick_ratio = lower_wick / candle_range if candle_range > 0 else 0.0
    upper_wick_ratio = upper_wick / candle_range if candle_range > 0 else 0.0
    volume_ma = _safe_float(row.get("vol_ma"))
    volume_ratio = (_safe_float(row.get("volume")) / volume_ma) if volume_ma > 0 else 0.0

    window_20 = _window(df, effective_index, 20, include_current=True)
    window_32 = _window(df, effective_index, int(getattr(config, "MARKET_STRUCTURE_LOOKBACK", 32) or 32), include_current=True)
    prior_sweep_window = _window(
        df,
        effective_index,
        int(getattr(config, "LIQUIDITY_SWEEP_LOOKBACK", 24) or 24),
        include_current=False,
    )
    prior_structure_window = _window(
        df,
        effective_index,
        int(getattr(config, "MARKET_STRUCTURE_LOOKBACK", 32) or 32),
        include_current=False,
    )

    recent_high_20 = _safe_float(window_20["high"].max(), high) if not window_20.empty else high
    recent_low_20 = _safe_float(window_20["low"].min(), low) if not window_20.empty else low
    recent_high_32 = _safe_float(window_32["high"].max(), high) if not window_32.empty else high
    recent_low_32 = _safe_float(window_32["low"].min(), low) if not window_32.empty else low
    prior_high = _safe_float(prior_sweep_window["high"].max(), high) if not prior_sweep_window.empty else high
    prior_low = _safe_float(prior_sweep_window["low"].min(), low) if not prior_sweep_window.empty else low
    next_resistance = (
        _safe_float(prior_structure_window["high"].max(), recent_high_32)
        if not prior_structure_window.empty
        else recent_high_32
    )
    next_support = (
        _safe_float(prior_structure_window["low"].min(), recent_low_32)
        if not prior_structure_window.empty
        else recent_low_32
    )

    min_break_pct = float(getattr(config, "LIQUIDITY_SWEEP_MIN_BREAK_PCT", 0.15) or 0.15)
    max_break_pct = float(getattr(config, "LIQUIDITY_SWEEP_MAX_BREAK_PCT", 2.5) or 2.5)
    min_reclaim_pct = float(getattr(config, "LIQUIDITY_SWEEP_MIN_RECLAIM_PCT", 0.20) or 0.20)

    low_break_pct = ((prior_low - low) / prior_low * 100) if prior_low > 0 and low < prior_low else 0.0
    high_break_pct = ((high - prior_high) / prior_high * 100) if prior_high > 0 and high > prior_high else 0.0
    reclaim_from_low_pct = ((close - low) / low * 100) if low > 0 and close > low else 0.0
    pullback_from_high_pct = ((high - close) / high * 100) if high > 0 and close < high else 0.0

    sweep_low_detected = bool(min_break_pct <= low_break_pct <= max_break_pct)
    sweep_high_detected = bool(min_break_pct <= high_break_pct <= max_break_pct)
    reclaim_recent_low = bool(close > prior_low or reclaim_from_low_pct >= min_reclaim_pct)
    reject_recent_high = bool(close < prior_high or pullback_from_high_pct >= min_reclaim_pct)

    return {
        "recent_high_20": float(recent_high_20),
        "recent_low_20": float(recent_low_20),
        "recent_high_32": float(recent_high_32),
        "recent_low_32": float(recent_low_32),
        "prior_recent_high": float(prior_high),
        "prior_recent_low": float(prior_low),
        "distance_to_recent_high_pct": ((recent_high_32 - close) / close * 100) if close > 0 else 0.0,
        "distance_to_recent_low_pct": ((close - recent_low_32) / close * 100) if close > 0 else 0.0,
        "close_position": float(close_position),
        "lower_wick_ratio": float(lower_wick_ratio),
        "upper_wick_ratio": float(upper_wick_ratio),
        "volume_ratio": float(volume_ratio),
        "sweep_low_detected": sweep_low_detected,
        "sweep_high_detected": sweep_high_detected,
        "reclaim_recent_low": reclaim_recent_low,
        "reject_recent_high": reject_recent_high,
        "space_to_next_resistance_pct": ((next_resistance - close) / close * 100) if close > 0 else 0.0,
        "space_to_next_support_pct": ((close - next_support) / close * 100) if close > 0 else 0.0,
        "low_break_pct": float(low_break_pct),
        "high_break_pct": float(high_break_pct),
        "reclaim_from_low_pct": float(reclaim_from_low_pct),
        "pullback_from_high_pct": float(pullback_from_high_pct),
        "rsi_delta": _safe_float(row.get("rsi")) - _safe_float(prev.get("rsi"), _safe_float(row.get("rsi"))),
        "macd_hist_delta": _safe_float(row.get("macd_hist")) - _safe_float(prev.get("macd_hist"), _safe_float(row.get("macd_hist"))),
    }
