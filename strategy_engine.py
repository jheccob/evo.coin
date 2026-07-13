from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

import config
from services.market_structure_service import (
    annotate_market_structure,
    detect_liquidity_sweep_reversal_long,
    detect_liquidity_sweep_reversal_short,
    evaluate_market_structure_guard,
)


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
    pullback_buffer_pct: float = float(config.PULLBACK_BUFFER_PCT)
    long_partial_pct: float = float(config.LONG_TAKE_PROFIT_PCT) * 0.55
    short_partial_pct: float = float(config.SHORT_TAKE_PROFIT_PCT) * 0.55
    long_stop_pct: float = float(config.LONG_STOP_LOSS_PCT)
    short_stop_pct: float = float(config.SHORT_STOP_LOSS_PCT)
    long_trailing_pct: float = float(config.LONG_TRAILING_STOP_PCT)
    short_trailing_pct: float = float(config.SHORT_TRAILING_STOP_PCT)
    long_max_distance_pct: float = float(config.LONG_MAX_DISTANCE_EMA_PCT)
    short_max_distance_pct: float = float(config.SHORT_MAX_DISTANCE_EMA_PCT)
    long_slope_lookback: int = int(config.LONG_SLOPE_LOOKBACK)
    long_trend_ema_lookback: int = int(getattr(config, "LONG_TREND_EMA_LOOKBACK", 3))
    long_fast_slow_gap_pct: float = float(config.LONG_FAST_SLOW_GAP_PCT)
    short_slope_lookback: int = int(getattr(config, "SHORT_SLOPE_LOOKBACK", 5))
    short_trend_ema_lookback: int = int(getattr(config, "SHORT_TREND_EMA_LOOKBACK", 3))
    short_fast_slow_gap_pct: float = float(config.SHORT_FAST_SLOW_GAP_PCT)


def calculate_indicators(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=params.ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=params.ema_slow, adjust=False).mean()
    out["ema_trend"] = out["close"].ewm(span=params.ema_trend, adjust=False).mean()
    macd_fast = out["close"].ewm(span=int(getattr(config, "MACD_FAST_PERIOD", 12)), adjust=False).mean()
    macd_slow = out["close"].ewm(span=int(getattr(config, "MACD_SLOW_PERIOD", 26)), adjust=False).mean()
    out["macd"] = macd_fast - macd_slow
    out["macd_signal"] = out["macd"].ewm(span=int(getattr(config, "MACD_SIGNAL_PERIOD", 9)), adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

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

    plus_dm = out["high"].diff().clip(lower=0)
    minus_dm = out["low"].diff().clip(upper=0).abs()

    atr_adx = tr.rolling(config.ADX_PERIOD).mean()
    plus_di = 100 * (plus_dm.rolling(config.ADX_PERIOD).mean() / atr_adx)
    minus_di = 100 * (minus_dm.rolling(config.ADX_PERIOD).mean() / atr_adx)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)).fillna(0) * 100
    out["adx"] = dx.rolling(config.ADX_PERIOD).mean()

    out["vol_ma"] = out["volume"].rolling(config.VOLUME_MA_PERIOD).mean()
    out["atr"] = tr.rolling(params.atr_period).mean()
    out["atr_pct"] = out["atr"] / out["close"] * 100
    out = annotate_market_structure(out)
    return out


def _macd_direction_ok(row: pd.Series, side: str) -> bool:
    if not bool(getattr(config, "ENABLE_MACD_ENTRY_FILTER", False)):
        return True

    mode = str(getattr(config, "MACD_ENTRY_FILTER_MODE", "histogram") or "histogram").strip().lower()
    macd_value = float(row.get("macd", 0.0) or 0.0)
    signal_value = float(row.get("macd_signal", 0.0) or 0.0)
    hist_value = float(row.get("macd_hist", 0.0) or 0.0)

    if side == "long":
        if mode == "line":
            return macd_value > signal_value
        if mode == "zero":
            return macd_value > 0
        return hist_value > 0

    if mode == "line":
        return macd_value < signal_value
    if mode == "zero":
        return macd_value < 0
    return hist_value < 0


def _volume_ma_entry_ok(row: pd.Series) -> bool:
    if not bool(getattr(config, "ENABLE_VOLUME_MA_ENTRY_FILTER", False)):
        return True

    volume = float(row.get("volume", 0.0) or 0.0)
    volume_ma = row.get("vol_ma")
    if pd.isna(volume_ma):
        return False

    multiplier = max(float(getattr(config, "VOLUME_MA_ENTRY_MULTIPLIER", 1.0) or 1.0), 0.0)
    return volume > float(volume_ma or 0.0) * multiplier


def resolve_signal_timestamp(df: pd.DataFrame, index: int = -1) -> Optional[pd.Timestamp]:
    resolved_index = len(df) + index if index < 0 else index
    if resolved_index < 0 or resolved_index >= len(df):
        return None

    raw_timestamp = None
    if "timestamp" in df.columns:
        raw_timestamp = df.iloc[resolved_index].get("timestamp")
    elif len(df.index) > resolved_index:
        raw_timestamp = df.index[resolved_index]

    if raw_timestamp is None:
        return None

    timestamp = pd.Timestamp(raw_timestamp)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def detect_market_regime(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Dict[str, object]:
    row = df.iloc[index]
    ema_gap_pct = abs(row["ema_slow"] - row["ema_trend"]) / row["close"] * 100
    bullish = row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
    bearish = row["ema_fast"] < row["ema_slow"] < row["ema_trend"]
    long_tradeable = bool(row["atr_pct"] >= params.long_min_atr_pct)
    short_tradeable = bool(row["atr_pct"] >= params.short_min_atr_pct)
    long_gap_tradeable = bool(ema_gap_pct >= params.long_regime_gap_pct)
    short_gap_tradeable = bool(ema_gap_pct >= params.short_regime_gap_pct)

    if bullish and long_gap_tradeable and long_tradeable:
        regime = "trend_bull"
        regime_detail = "trend_bull"
    elif bearish and short_gap_tradeable:
        regime = "trend_bear"
        regime_detail = "trend_bear" if short_tradeable else "trend_bear_atr_low"
    elif bullish:
        regime = "weak_bull"
        blockers = []
        if not long_gap_tradeable:
            blockers.append("gap")
        if not long_tradeable:
            blockers.append("atr")
        regime_detail = f"weak_bull_{'_'.join(blockers)}" if blockers else "weak_bull"
    elif bearish:
        regime = "weak_bear"
        blockers = []
        if not short_gap_tradeable:
            blockers.append("gap")
        if not short_tradeable:
            blockers.append("atr")
        regime_detail = f"weak_bear_{'_'.join(blockers)}" if blockers else "weak_bear"
    else:
        regime = "range"
        regime_detail = "range"

    return {
        "regime": regime,
        "regime_detail": regime_detail,
        "tradeable_long": long_tradeable,
        "tradeable_short": short_tradeable,
        "gap_tradeable_long": long_gap_tradeable,
        "gap_tradeable_short": short_gap_tradeable,
        "ema_gap_pct": round(float(ema_gap_pct), 4),
        "atr_pct": round(float(row["atr_pct"]), 4),
    }


def _detect_long_reversal_rebound(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Optional[Dict[str, object]]:
    if not bool(getattr(config, "ENABLE_LONG_REVERSAL_REBOUND", True)):
        return None

    effective_index = len(df) + index if index < 0 else index
    if effective_index <= 0:
        return None

    lookback = max(int(getattr(config, "LONG_REVERSAL_LOOKBACK_CANDLES", 12) or 12), 3)
    start_index = max(effective_index - lookback, 0)
    window = df.iloc[start_index : effective_index + 1]
    if len(window) < 3:
        return None

    row = df.iloc[effective_index]
    prev = df.iloc[effective_index - 1]
    recent_high = float(window["high"].max())
    recent_low = float(window["low"].min())
    current_close = float(row["close"])
    if recent_high <= 0 or recent_low <= 0 or current_close <= 0:
        return None

    drop_pct = (recent_high - recent_low) / recent_high * 100
    bounce_from_low_pct = (current_close - recent_low) / recent_low * 100
    candle_state = _resolve_candle_state(row, prev)
    volume_ma = row.get("vol_ma")
    if pd.isna(volume_ma) or float(volume_ma or 0.0) <= 0:
        volume_ratio = 0.0
    else:
        volume_ratio = float(row.get("volume", 0.0) or 0.0) / float(volume_ma)

    rsi_value = float(row.get("rsi", 0.0) or 0.0)
    prev_rsi = float(prev.get("rsi", rsi_value) or rsi_value)
    adx_value = float(row.get("adx", 0.0) or 0.0)
    macd_hist = float(row.get("macd_hist", 0.0) or 0.0)
    prev_macd_hist = float(prev.get("macd_hist", macd_hist) or macd_hist)
    macd_improving = bool(macd_hist > prev_macd_hist)
    reclaim_fast = bool(current_close >= float(row["ema_fast"]))
    breakout_reclaim = bool(current_close > float(prev["high"]) * 1.001)
    bullish_reaction = bool(candle_state["bullish_close"] and candle_state["close_position"] >= 0.5)

    min_drop_pct = float(getattr(config, "LONG_REVERSAL_MIN_DROP_PCT", 2.2) or 2.2)
    min_bounce_pct = float(getattr(config, "LONG_REVERSAL_MIN_BOUNCE_FROM_LOW_PCT", 1.2) or 1.2)
    max_bounce_pct = float(getattr(config, "LONG_REVERSAL_MAX_BOUNCE_FROM_LOW_PCT", 5.8) or 5.8)
    min_close_position = float(getattr(config, "LONG_REVERSAL_MIN_CLOSE_POSITION", 0.55) or 0.55)
    min_volume_ratio = float(getattr(config, "LONG_REVERSAL_MIN_VOLUME_RATIO", 1.15) or 1.15)
    min_rsi = float(getattr(config, "LONG_REVERSAL_MIN_RSI", 42.0) or 42.0)
    max_rsi = float(getattr(config, "LONG_REVERSAL_MAX_RSI", 68.0) or 68.0)
    high_rsi_threshold = float(getattr(config, "LONG_REVERSAL_HIGH_RSI_THRESHOLD", 58.0) or 58.0)
    high_rsi_min_adx = float(getattr(config, "LONG_REVERSAL_HIGH_RSI_MIN_ADX", 35.0) or 35.0)
    require_macd_improving = bool(getattr(config, "LONG_REVERSAL_REQUIRE_MACD_IMPROVING", True))

    if drop_pct < min_drop_pct:
        return None
    if bounce_from_low_pct < min_bounce_pct or bounce_from_low_pct > max_bounce_pct:
        return None
    if float(candle_state["close_position"]) < min_close_position:
        return None
    if volume_ratio < min_volume_ratio:
        return None
    if rsi_value < min_rsi or rsi_value > max_rsi:
        return None
    if rsi_value > high_rsi_threshold and adx_value < high_rsi_min_adx:
        return None
    if require_macd_improving and not macd_improving:
        return None
    if not bullish_reaction:
        return None
    if not (reclaim_fast or breakout_reclaim):
        return None

    return {
        "drop_pct": round(drop_pct, 4),
        "bounce_from_low_pct": round(bounce_from_low_pct, 4),
        "close_position": round(float(candle_state["close_position"]), 4),
        "volume_ratio": round(volume_ratio, 4),
        "rsi": round(rsi_value, 4),
        "rsi_delta": round(rsi_value - prev_rsi, 4),
        "adx": round(adx_value, 4),
        "macd_hist": round(macd_hist, 8),
        "macd_hist_delta": round(macd_hist - prev_macd_hist, 8),
        "reclaim_fast": reclaim_fast,
        "breakout_reclaim": breakout_reclaim,
    }


def _detect_short_reversal_rejection(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Optional[Dict[str, object]]:
    if not bool(getattr(config, "ENABLE_SHORT_REVERSAL_REJECTION", True)):
        return None

    effective_index = len(df) + index if index < 0 else index
    if effective_index <= 0:
        return None

    lookback = max(int(getattr(config, "SHORT_REVERSAL_LOOKBACK_CANDLES", 12) or 12), 3)
    start_index = max(effective_index - lookback, 0)
    window = df.iloc[start_index : effective_index + 1]
    if len(window) < 3:
        return None

    row = df.iloc[effective_index]
    prev = df.iloc[effective_index - 1]
    signal_timestamp = resolve_signal_timestamp(df, index=effective_index)
    blocked_hours = set(getattr(config, "SHORT_REVERSAL_BLOCKED_ENTRY_HOURS_UTC", []) or [])
    if signal_timestamp is not None and signal_timestamp.hour in blocked_hours:
        return None

    recent_high = float(window["high"].max())
    recent_low = float(window["low"].min())
    current_close = float(row["close"])
    if recent_high <= 0 or recent_low <= 0 or current_close <= 0:
        return None

    rise_pct = (recent_high - recent_low) / recent_low * 100
    pullback_from_high_pct = (recent_high - current_close) / recent_high * 100
    candle_state = _resolve_candle_state(row, prev)
    volume_ma = row.get("vol_ma")
    if pd.isna(volume_ma) or float(volume_ma or 0.0) <= 0:
        volume_ratio = 0.0
    else:
        volume_ratio = float(row.get("volume", 0.0) or 0.0) / float(volume_ma)

    rsi_value = float(row.get("rsi", 0.0) or 0.0)
    prev_rsi = float(prev.get("rsi", rsi_value) or rsi_value)
    adx_value = float(row.get("adx", 0.0) or 0.0)
    macd_hist = float(row.get("macd_hist", 0.0) or 0.0)
    prev_macd_hist = float(prev.get("macd_hist", macd_hist) or macd_hist)
    macd_worsening = bool(macd_hist < prev_macd_hist)
    lose_fast = bool(current_close <= float(row["ema_fast"]))
    breakdown_reclaim = bool(current_close < float(prev["low"]) * 0.999)
    bearish_reaction = bool(candle_state["bearish_close"] and candle_state["close_position"] <= 0.5)

    min_rise_pct = float(getattr(config, "SHORT_REVERSAL_MIN_RISE_PCT", 2.2) or 2.2)
    min_pullback_pct = float(getattr(config, "SHORT_REVERSAL_MIN_PULLBACK_FROM_HIGH_PCT", 1.1) or 1.1)
    max_pullback_pct = float(getattr(config, "SHORT_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT", 5.8) or 5.8)
    max_close_position = float(getattr(config, "SHORT_REVERSAL_MAX_CLOSE_POSITION", 0.45) or 0.45)
    min_volume_ratio = float(getattr(config, "SHORT_REVERSAL_MIN_VOLUME_RATIO", 1.15) or 1.15)
    min_rsi = float(getattr(config, "SHORT_REVERSAL_MIN_RSI", 32.0) or 32.0)
    max_rsi = float(getattr(config, "SHORT_REVERSAL_MAX_RSI", 62.0) or 62.0)
    low_rsi_threshold = float(getattr(config, "SHORT_REVERSAL_LOW_RSI_THRESHOLD", 42.0) or 42.0)
    low_rsi_min_adx = float(getattr(config, "SHORT_REVERSAL_LOW_RSI_MIN_ADX", 35.0) or 35.0)
    require_macd_worsening = bool(getattr(config, "SHORT_REVERSAL_REQUIRE_MACD_WORSENING", True))

    if rise_pct < min_rise_pct:
        return None
    if pullback_from_high_pct < min_pullback_pct or pullback_from_high_pct > max_pullback_pct:
        return None
    if float(candle_state["close_position"]) > max_close_position:
        return None
    if volume_ratio < min_volume_ratio:
        return None
    if rsi_value < min_rsi or rsi_value > max_rsi:
        return None
    if rsi_value < low_rsi_threshold and adx_value < low_rsi_min_adx:
        return None
    if require_macd_worsening and not macd_worsening:
        return None
    if not bearish_reaction:
        return None
    if not (lose_fast or breakdown_reclaim):
        return None

    return {
        "rise_pct": round(rise_pct, 4),
        "pullback_from_high_pct": round(pullback_from_high_pct, 4),
        "close_position": round(float(candle_state["close_position"]), 4),
        "volume_ratio": round(volume_ratio, 4),
        "rsi": round(rsi_value, 4),
        "rsi_delta": round(rsi_value - prev_rsi, 4),
        "adx": round(adx_value, 4),
        "macd_hist": round(macd_hist, 8),
        "macd_hist_delta": round(macd_hist - prev_macd_hist, 8),
        "lose_fast": lose_fast,
        "breakdown_reclaim": breakdown_reclaim,
    }


def detect_setup(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Dict[str, object]:
    row = df.iloc[index]
    prev = df.iloc[index - 1]
    regime = detect_market_regime(df, params, index=index)
    regime_name = regime["regime"]

    pullback_long = bool(row["low"] <= row["ema_fast"] * (1 + params.pullback_buffer_pct / 100))
    pullback_short = bool(row["high"] >= row["ema_fast"] * (1 - params.pullback_buffer_pct / 100))

    if regime_name == "trend_bull" and pullback_long:
        return {"setup": "pullback_long", "direction": "long", "regime": regime}
    if regime_name == "trend_bear" and pullback_short:
        return {"setup": "pullback_short", "direction": "short", "regime": regime}

    liquidity_sweep_long = detect_liquidity_sweep_reversal_long(df, index=index)
    if liquidity_sweep_long:
        return {
            "setup": "liquidity_sweep_reversal_long",
            "direction": "long",
            "regime": {
                **regime,
                "liquidity_sweep": liquidity_sweep_long,
                "market_structure": liquidity_sweep_long.get("market_structure") or {},
            },
        }

    liquidity_sweep_short = detect_liquidity_sweep_reversal_short(df, index=index)
    if liquidity_sweep_short:
        return {
            "setup": "liquidity_sweep_reversal_short",
            "direction": "short",
            "regime": {
                **regime,
                "liquidity_sweep": liquidity_sweep_short,
                "market_structure": liquidity_sweep_short.get("market_structure") or {},
            },
        }

    reversal_rebound = _detect_long_reversal_rebound(df, params, index=index)
    if reversal_rebound:
        return {
            "setup": "reversal_rebound_long",
            "direction": "long",
            "regime": {
                **regime,
                "reversal_rebound": reversal_rebound,
            },
        }

    reversal_rejection = _detect_short_reversal_rejection(df, params, index=index)
    if reversal_rejection:
        return {
            "setup": "reversal_rejection_short",
            "direction": "short",
            "regime": {
                **regime,
                "reversal_rejection": reversal_rejection,
            },
        }

    if regime_name in {"trend_bull", "weak_bull"}:
        if row["close"] > row["ema_fast"] * (1 + params.long_max_distance_pct / 100):
            return {"setup": None, "direction": None, "regime": regime}
        return {"setup": "trend_resume_long", "direction": "long", "regime": regime}

    if regime_name in {"trend_bear", "weak_bear"}:
        if row["close"] > prev["close"] and row["close"] < row["ema_slow"]:
            return {"setup": "relief_rally_short", "direction": "short", "regime": regime}
        return {"setup": "trend_resume_short", "direction": "short", "regime": regime}

    return {"setup": None, "direction": None, "regime": regime}


def get_min_required_rows(params: StrategyParams) -> int:
    return max(
        params.ema_trend + 5,
        params.rsi_period + 5,
        params.atr_period + 5,
        params.long_slope_lookback + 5,
        params.long_trend_ema_lookback + 5,
        params.short_slope_lookback + 5,
        params.short_trend_ema_lookback + 5,
        config.ADX_PERIOD + 5,
        config.VOLUME_MA_PERIOD + 5,
    )


def _build_signal_payload(
    *,
    signal: str,
    reason: str,
    setup: Dict[str, object],
    row,
    df: Optional[pd.DataFrame] = None,
    index: int = -1,
) -> Dict[str, object]:
    resolved_setup = dict(setup or {})
    if signal in {"buy", "sell"} and df is not None:
        direction = "long" if signal == "buy" else "short"
        guard = evaluate_market_structure_guard(
            df,
            index=index,
            direction=direction,
            setup_name=str(resolved_setup.get("setup") or ""),
        )
        market_structure = guard.get("market_structure") or {}
        if market_structure:
            resolved_setup = {
                **resolved_setup,
                "regime": {
                    **(dict(resolved_setup.get("regime") or {})),
                    "market_structure": market_structure,
                    "market_structure_guard": {
                        "allowed": bool(guard.get("allowed", True)),
                        "reason": str(guard.get("reason") or ""),
                        "detail": str(guard.get("detail") or ""),
                    },
                },
            }
        if not bool(guard.get("allowed", True)):
            detail = str(guard.get("detail") or "").strip()
            return {
                "signal": "hold",
                "reason": f"{guard.get('reason')}{'|' + detail if detail else ''}",
                "setup": resolved_setup,
                "market_structure": market_structure,
            }

    payload = {
        "signal": signal,
        "reason": reason,
        "setup": resolved_setup,
    }
    if signal in {"buy", "sell"}:
        payload["entry_price"] = float(row["close"])
        payload["atr"] = float(row["atr"])
        market_structure = ((resolved_setup.get("regime") or {}).get("market_structure") or {})
        if market_structure:
            payload["market_structure"] = market_structure
    return payload


def _safe_metric(value: object, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return float(default)
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _mean_metric(df: pd.DataFrame, column: str, default: float = 0.0) -> float:
    if column not in df.columns or df.empty:
        return float(default)
    series = pd.to_numeric(df[column], errors="coerce").dropna()
    if series.empty:
        return float(default)
    return float(series.mean())


def _resolve_candle_state(row, prev) -> Dict[str, float | bool]:
    candle_range = float(row["high"] - row["low"])
    close_position = 0.5
    if candle_range > 0:
        close_position = (float(row["close"]) - float(row["low"])) / candle_range

    return {
        "close_position": float(close_position),
        "bullish_close": bool(float(row["close"]) > float(row["open"])),
        "bearish_close": bool(float(row["close"]) < float(row["open"])),
        "close_above_ema_fast": bool(float(row["close"]) >= float(row["ema_fast"])),
        "close_below_ema_fast": bool(float(row["close"]) <= float(row["ema_fast"])),
        "close_below_prev_close": bool(float(row["close"]) <= float(prev["close"])),
    }


def _resolve_triggerless_fallback_setup(setup: Dict[str, object], row, prev) -> Dict[str, object]:
    if not bool(getattr(config, "ALLOW_TRIGGERLESS_ENTRIES", False)):
        return setup
    if setup.get("direction") in {"long", "short"}:
        return setup

    regime_payload = setup.get("regime") or {}
    regime_name = str(regime_payload.get("regime") or "").strip().lower()
    regime_label = str(regime_payload.get("regime_detail") or regime_name).strip().lower()
    bullish_alignment = bool(
        float(row["close"]) >= float(row["ema_trend"])
        and float(row["ema_fast"]) >= float(row["ema_slow"])
        and float(row["ema_fast"]) >= float(prev["ema_fast"])
    )
    bearish_alignment = bool(
        float(row["close"]) <= float(row["ema_trend"])
        and float(row["ema_fast"]) <= float(row["ema_slow"])
        and float(row["ema_fast"]) <= float(prev["ema_fast"])
    )

    if not bullish_alignment and not bearish_alignment:
        if float(row["close"]) > float(prev["close"]):
            bullish_alignment = True
        elif float(row["close"]) < float(prev["close"]):
            bearish_alignment = True

    if bullish_alignment:
        resolved_regime = regime_name if regime_name in {"trend_bull", "weak_bull"} else "weak_bull"
        return {
            **setup,
            "setup": "trend_resume_long",
            "direction": "long",
            "source_setup": "triggerless_fallback",
            "regime": {
                **(setup.get("regime") or {}),
                "regime": resolved_regime,
            },
        }

    if bearish_alignment:
        resolved_regime = regime_name if regime_name in {"trend_bear", "weak_bear"} else "weak_bear"
        return {
            **setup,
            "setup": "trend_resume_short",
            "direction": "short",
            "source_setup": "triggerless_fallback",
            "regime": {
                **(setup.get("regime") or {}),
                "regime": resolved_regime,
            },
        }

    return setup


def generate_entry_signal(df: pd.DataFrame, params: StrategyParams, index: int = -1) -> Dict[str, object]:
    min_rows = get_min_required_rows(params)

    if len(df) < min_rows:
        return {"signal": "hold", "reason": "dados insuficientes"}

    row = df.iloc[index]
    prev = df.iloc[index - 1]

    setup = detect_setup(df, params, index=index)
    setup = _resolve_triggerless_fallback_setup(setup, row, prev)
    direction = setup["direction"]
    regime_payload = setup.get("regime") or {}
    regime_name = str(regime_payload.get("regime") or "").strip().lower()
    regime_label = str(regime_payload.get("regime_detail") or regime_name).strip().lower()
    is_reversal_rebound_long = bool(setup.get("setup") == "reversal_rebound_long")
    is_reversal_rejection_short = bool(setup.get("setup") == "reversal_rejection_short")
    is_liquidity_sweep_long = bool(setup.get("setup") == "liquidity_sweep_reversal_long")
    is_liquidity_sweep_short = bool(setup.get("setup") == "liquidity_sweep_reversal_short")

    if bool(getattr(config, "BLOCK_UNKNOWN_REGIME", True)) and regime_name in {"", "unknown", "none", "null"}:
        return {
            "signal": "hold",
            "reason": "regime unknown bloqueado",
            "setup": setup,
        }

    trend_strength_pct = abs(row["ema_fast"] - row["ema_slow"]) / row["close"] * 100

    if row["atr_pct"] < config.GLOBAL_MIN_ATR_PCT:
        return {"signal": "hold", "reason": f"volatilidade insuficiente ({row['atr_pct']:.2f}%)", "setup": setup}

    if trend_strength_pct < config.MIN_TREND_STRENGTH_PCT and not (is_reversal_rebound_long or is_liquidity_sweep_long):
        return {"signal": "hold", "reason": f"tendência abaixo do piso ({trend_strength_pct:.2f}%)", "setup": setup}

    trend_context_pct = abs(row["ema_slow"] - row["ema_trend"]) / row["close"] * 100
    symbol_family = str(config.get_symbol_family_key(getattr(config, "SYMBOL", "")) or "global")
    if symbol_family == "alt_trend_strict" and bool(getattr(config, "ALT_STRICT_CONTEXT_FILTER", True)):
        alt_min_context = float(getattr(config, "ALT_MIN_CONTEXT_GAP_PCT", 0.22) or 0.22)
        alt_min_atr = float(getattr(config, "ALT_MIN_GLOBAL_ATR_PCT", 0.14) or 0.14)
        if trend_context_pct < alt_min_context:
            return {"signal": "hold", "reason": f"contexto alt fraco ({trend_context_pct:.2f}%)", "setup": setup}
        if row["atr_pct"] < alt_min_atr:
            return {"signal": "hold", "reason": f"atr alt insuficiente ({row['atr_pct']:.2f}%)", "setup": setup}

    if direction == "long" and config.ALLOW_LONG:
        signal_timestamp = resolve_signal_timestamp(df, index=index)
        use_entry_hour_blocks = bool(getattr(config, "USE_ENTRY_HOUR_BLOCKS", False))
        blocked_long_hours = set(getattr(config, "BLOCKED_LONG_ENTRY_HOURS_UTC", []) or [])
        if use_entry_hour_blocks and signal_timestamp is not None and signal_timestamp.hour in blocked_long_hours:
            return {
                "signal": "hold",
                "reason": f"hora bloqueada para long ({signal_timestamp.hour:02d} UTC)",
                "setup": setup,
            }

        if setup.get("setup") == "pullback_long" and not bool(getattr(config, "ENABLE_LONG_PULLBACK", True)):
            if bool(getattr(config, "LONG_PULLBACK_AS_RESUME_WHEN_DISABLED", False)) and bool(
                getattr(config, "ENABLE_LONG_RESUME", True)
            ):
                setup = {
                    **setup,
                    "setup": "trend_resume_long",
                    "source_setup": "pullback_long",
                }
            else:
                return {"signal": "hold", "reason": "pullback_long bloqueado", "setup": setup}

        if setup.get("setup") == "trend_resume_long" and not bool(getattr(config, "ENABLE_LONG_RESUME", True)):
            return {"signal": "hold", "reason": "trend_resume_long bloqueado", "setup": setup}

        allow_weak_bull_atr = bool(
            getattr(config, "ALLOW_WEAK_BULL_ATR_LONG_ENTRIES", False)
            and regime_name == "weak_bull"
            and regime_label == "weak_bull_atr"
        )
        if (
            regime_name != "trend_bull"
            and not allow_weak_bull_atr
            and not is_reversal_rebound_long
            and not is_liquidity_sweep_long
            and not bool(getattr(config, "BYPASS_WEAK_REGIME_GATE", False))
        ):
            return {"signal": "hold", "reason": f"regime fraco ({regime_label})", "setup": setup}

        if is_liquidity_sweep_long:
            sweep_payload = dict(regime_payload.get("liquidity_sweep") or {})
            sweep_score = int(sweep_payload.get("score", 0) or 0)
            sweep_reasons = [str(item) for item in (sweep_payload.get("reasons") or []) if str(item).strip()]
            return _build_signal_payload(
                signal="buy",
                reason=f"liquidity_sweep_reversal_long_score={sweep_score}|{','.join(sweep_reasons)}",
                setup=setup,
                row=row,
                df=df,
                index=index,
            )

        if is_reversal_rebound_long:
            reversal_payload = dict(regime_payload.get("reversal_rebound") or {})
            score = 0
            reasons = []
            if float(reversal_payload.get("drop_pct", 0.0) or 0.0) >= float(
                getattr(config, "LONG_REVERSAL_MIN_DROP_PCT", 2.2) or 2.2
            ):
                score += 1
                reasons.append(f"queda={float(reversal_payload.get('drop_pct', 0.0) or 0.0):.2f}%")
            if float(reversal_payload.get("bounce_from_low_pct", 0.0) or 0.0) >= float(
                getattr(config, "LONG_REVERSAL_MIN_BOUNCE_FROM_LOW_PCT", 1.2) or 1.2
            ):
                score += 1
                reasons.append(f"reacao={float(reversal_payload.get('bounce_from_low_pct', 0.0) or 0.0):.2f}%")
            if float(reversal_payload.get("close_position", 0.0) or 0.0) >= float(
                getattr(config, "LONG_REVERSAL_MIN_CLOSE_POSITION", 0.55) or 0.55
            ):
                score += 1
                reasons.append("fechamento_forte")
            if float(reversal_payload.get("volume_ratio", 0.0) or 0.0) >= float(
                getattr(config, "LONG_REVERSAL_MIN_VOLUME_RATIO", 1.15) or 1.15
            ):
                score += 1
                reasons.append(f"volume={float(reversal_payload.get('volume_ratio', 0.0) or 0.0):.2f}x")
            if float(reversal_payload.get("rsi_delta", 0.0) or 0.0) > 0:
                score += 1
                reasons.append("rsi_retoma")
            if float(reversal_payload.get("macd_hist_delta", 0.0) or 0.0) > 0:
                score += 1
                reasons.append("macd_melhora")
            if bool(reversal_payload.get("reclaim_fast")) or bool(reversal_payload.get("breakout_reclaim")):
                score += 1
                reasons.append("reclaim")

            min_reversal_score = int(getattr(config, "LONG_REVERSAL_MIN_SCORE", 5) or 5)
            if score >= min_reversal_score:
                return _build_signal_payload(
                    signal="buy",
                    reason=f"reversal_rebound_long_score={score}|" + ",".join(reasons),
                    setup=setup,
                    row=row,
                    df=df,
                    index=index,
                )

            return {
                "signal": "hold",
                "reason": f"reversal_rebound_long_score_baixo={score}",
                "setup": setup,
            }

        if setup.get("setup") == "pullback_long":
            pullback_min_adx = float(getattr(config, "PULLBACK_LONG_MIN_ADX", 0.0) or 0.0)
            pullback_max_context_gap = float(getattr(config, "PULLBACK_LONG_MAX_CONTEXT_GAP_PCT", 0.0) or 0.0)
            pullback_min_rsi = float(getattr(config, "PULLBACK_LONG_MIN_RSI", 0.0) or 0.0)
            pullback_max_rsi = float(getattr(config, "PULLBACK_LONG_MAX_RSI", 0.0) or 0.0)
            if float(row["adx"]) < pullback_min_adx:
                return {
                    "signal": "hold",
                    "reason": f"pullback_long_adx_fraco={float(row['adx']):.2f}",
                    "setup": setup,
                }
            if pullback_max_context_gap > 0 and trend_context_pct > pullback_max_context_gap:
                return {
                    "signal": "hold",
                    "reason": f"pullback_long_contexto_quente={trend_context_pct:.2f}",
                    "setup": setup,
                }
            if pullback_min_rsi > 0 and float(row["rsi"]) < pullback_min_rsi:
                return {
                    "signal": "hold",
                    "reason": f"pullback_long_rsi_baixo={float(row['rsi']):.2f}",
                    "setup": setup,
                }
            if pullback_max_rsi > 0 and float(row["rsi"]) > pullback_max_rsi:
                return {
                    "signal": "hold",
                    "reason": f"pullback_long_rsi_alto={float(row['rsi']):.2f}",
                    "setup": setup,
                }

        if setup.get("setup") == "trend_resume_long":
            min_context_gap = float(getattr(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.0) or 0.0)
            max_context_gap = float(getattr(config, "TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT", 0.0) or 0.0)
            min_adx = float(getattr(config, "TREND_RESUME_LONG_MIN_ADX", 0.0) or 0.0)
            min_trend_strength = float(getattr(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.0) or 0.0)
            max_rsi = float(getattr(config, "TREND_RESUME_LONG_MAX_RSI", 0.0) or 0.0)
            require_close_above_prev_close = bool(
                getattr(config, "TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE", False)
            )
            if trend_context_pct < min_context_gap:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_long_contexto_fraco={trend_context_pct:.2f}",
                    "setup": setup,
                }
            if max_context_gap > 0 and trend_context_pct > max_context_gap:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_long_contexto_quente={trend_context_pct:.2f}",
                    "setup": setup,
                }
            if float(row["adx"]) < min_adx:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_long_adx_fraco={float(row['adx']):.2f}",
                    "setup": setup,
                }
            if trend_strength_pct < min_trend_strength:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_long_tendencia_fraca={trend_strength_pct:.2f}",
                    "setup": setup,
                }
            if max_rsi > 0 and float(row["rsi"]) > max_rsi:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_long_rsi_esticado={float(row['rsi']):.2f}",
                    "setup": setup,
                }
            if require_close_above_prev_close and float(row["close"]) <= float(prev["close"]):
                return {
                    "signal": "hold",
                    "reason": "trend_resume_long_sem_confirmacao_close",
                    "setup": setup,
                }

        if not _macd_direction_ok(row, "long"):
            return {
                "signal": "hold",
                "reason": f"macd_long_contra={float(row.get('macd_hist', 0.0) or 0.0):.6f}",
                "setup": setup,
            }

        if not _volume_ma_entry_ok(row):
            return {
                "signal": "hold",
                "reason": (
                    f"volume_abaixo_ma{int(getattr(config, 'VOLUME_MA_PERIOD', 21) or 21)}="
                    f"{float(row.get('volume', 0.0) or 0.0):.4f}/"
                    f"{float(row.get('vol_ma', 0.0) or 0.0):.4f}"
                ),
                "setup": setup,
            }

        score = 0
        reasons = []
        slope_lookback = max(int(params.long_slope_lookback), 1)
        trend_lookback = max(int(params.long_trend_ema_lookback), 1)
        slope_index = index - slope_lookback if index >= 0 else len(df) + index - slope_lookback
        trend_anchor_index = index - trend_lookback if index >= 0 else len(df) + index - trend_lookback
        slope_row = df.iloc[max(slope_index, 0)]
        trend_anchor_row = df.iloc[max(trend_anchor_index, 0)]
        fast_slow_gap_pct = (row["ema_fast"] - row["ema_slow"]) / row["close"] * 100
        candle_state = _resolve_candle_state(row, prev)

        if (
            row["close"] > row["ema_trend"]
            and row["ema_trend"] > prev["ema_trend"]
            and prev["ema_trend"] >= trend_anchor_row["ema_trend"]
        ):
            score += 1
            reasons.append("tendencia_macro")

        if (
            row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
            and fast_slow_gap_pct >= params.long_fast_slow_gap_pct
        ):
            score += 1
            reasons.append("ema_estrutura")

        if row["ema_fast"] > slope_row["ema_fast"] and row["ema_slow"] >= slope_row["ema_slow"]:
            score += 1
            reasons.append("momentum_media")

        distance_trend = (row["close"] - row["ema_trend"]) / row["close"] * 100
        if distance_trend < params.long_max_distance_pct:
            score += 1
            reasons.append("nao_estendido")

        rsi_floor = max(50.0, params.buy_rsi_floor)
        rsi_retake = bool(rsi_floor < row["rsi"] < 65 and row["rsi"] > prev["rsi"])
        breakout_confirmed = bool(row["close"] > prev["high"] * 1.002)

        if row["low"] <= row["ema_fast"] * (1 + (params.pullback_buffer_pct / 2) / 100):
            if bool(getattr(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False)) and setup.get("setup") == "pullback_long":
                hot_context_gap = float(getattr(config, "LONG_PULLBACK_HOT_CONTEXT_GAP_PCT", 0.85) or 0.85)
                hot_atr_pct = float(getattr(config, "LONG_PULLBACK_HOT_ATR_PCT", 0.40) or 0.40)
                hot_context = bool(trend_context_pct >= hot_context_gap)
                hot_atr = bool(float(row["atr_pct"]) >= hot_atr_pct)
                if hot_context and (hot_atr or rsi_retake):
                    reasons_block = []
                    if hot_atr:
                        reasons_block.append(f"atr={float(row['atr_pct']):.2f}")
                    if rsi_retake:
                        reasons_block.append(f"rsi={float(row['rsi']):.2f}")
                    return {
                        "signal": "hold",
                        "reason": "pullback_long_contexto_quente_exp|" + ",".join(reasons_block),
                        "setup": setup,
                    }
            score += 1
            reasons.append("pullback")

        if bool(getattr(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False)) and setup.get("setup") == "trend_resume_long":
            hot_resume_gap = float(getattr(config, "LONG_RESUME_HOT_CONTEXT_GAP_PCT", 0.88) or 0.88)
            if breakout_confirmed and trend_context_pct >= hot_resume_gap:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_long_contexto_quente_exp={trend_context_pct:.2f}",
                    "setup": setup,
                }

        if rsi_retake:
            score += 1
            reasons.append("rsi_retoma")

        if row["adx"] > config.LONG_ADX_THRESHOLD:
            score += 1
            reasons.append("adx_ok")

        if row["volume"] > row["vol_ma"] * config.LONG_VOLUME_RATIO_REQUIRED:
            score += 1
            reasons.append("volume_ok")

        allow_breakout_score = True
        if setup.get("setup") == "pullback_long":
            allow_breakout_score = bool(getattr(config, "PULLBACK_LONG_COUNT_BREAKOUT_SCORE", True))
        if setup.get("setup") == "pullback_long" and breakout_confirmed and trend_context_pct >= 0.9:
            return {
                "signal": "hold",
                "reason": f"pullback_long_breakout_tardio={trend_context_pct:.2f}",
                "setup": setup,
            }
        if allow_breakout_score and breakout_confirmed:
            score += 1
            reasons.append("rompimento")

        max_pullback_long_score = int(getattr(config, "MAX_PULLBACK_LONG_SCORE", 99) or 99)
        if setup.get("setup") == "pullback_long" and score > max_pullback_long_score:
            return {
                "signal": "hold",
                "reason": f"pullback_long_score_alto={score}",
                "setup": setup,
            }

        min_long_score = getattr(config, "MIN_LONG_SCORE", 7)
        if score >= min_long_score:
            return _build_signal_payload(
                signal="buy",
                reason=f"long_score={score}|" + ",".join(reasons),
                setup=setup,
                row=row,
                df=df,
                index=index,
            )

        return {
            "signal": "hold",
            "reason": f"score_baixo={score}",
            "setup": setup,
        }

    if direction == "short" and config.ALLOW_SHORT:
        if setup.get("setup") == "pullback_short" and not bool(getattr(config, "ENABLE_SHORT_PULLBACK", True)):
            return {"signal": "hold", "reason": "pullback_short bloqueado", "setup": setup}

        if setup.get("setup") == "relief_rally_short" and not bool(getattr(config, "ENABLE_SHORT_RELIEF_RALLY", False)):
            return {"signal": "hold", "reason": "relief_rally_short bloqueado", "setup": setup}

        if setup.get("setup") == "trend_resume_short" and not bool(getattr(config, "ENABLE_SHORT_RESUME", True)):
            return {"signal": "hold", "reason": "trend_resume_short bloqueado", "setup": setup}

        signal_timestamp = resolve_signal_timestamp(df, index=index)
        use_entry_hour_blocks = bool(getattr(config, "USE_ENTRY_HOUR_BLOCKS", False))
        blocked_short_hours = set(getattr(config, "BLOCKED_SHORT_ENTRY_HOURS_UTC", []) or [])
        if use_entry_hour_blocks and signal_timestamp is not None and signal_timestamp.hour in blocked_short_hours:
            return {
                "signal": "hold",
                "reason": f"hora bloqueada para short ({signal_timestamp.hour:02d} UTC)",
                "setup": setup,
            }

        if is_liquidity_sweep_short:
            sweep_payload = dict(regime_payload.get("liquidity_sweep") or {})
            sweep_score = int(sweep_payload.get("score", 0) or 0)
            sweep_reasons = [str(item) for item in (sweep_payload.get("reasons") or []) if str(item).strip()]
            return _build_signal_payload(
                signal="sell",
                reason=f"liquidity_sweep_reversal_short_score={sweep_score}|{','.join(sweep_reasons)}",
                setup=setup,
                row=row,
                df=df,
                index=index,
            )

        if is_reversal_rejection_short:
            rejection_payload = dict(regime_payload.get("reversal_rejection") or {})
            score = 0
            reasons = []
            if float(rejection_payload.get("rise_pct", 0.0) or 0.0) >= float(
                getattr(config, "SHORT_REVERSAL_MIN_RISE_PCT", 2.2) or 2.2
            ):
                score += 1
                reasons.append(f"alta={float(rejection_payload.get('rise_pct', 0.0) or 0.0):.2f}%")
            if float(rejection_payload.get("pullback_from_high_pct", 0.0) or 0.0) >= float(
                getattr(config, "SHORT_REVERSAL_MIN_PULLBACK_FROM_HIGH_PCT", 1.1) or 1.1
            ):
                score += 1
                reasons.append(
                    f"rejeicao={float(rejection_payload.get('pullback_from_high_pct', 0.0) or 0.0):.2f}%"
                )
            if float(rejection_payload.get("close_position", 1.0) or 1.0) <= float(
                getattr(config, "SHORT_REVERSAL_MAX_CLOSE_POSITION", 0.45) or 0.45
            ):
                score += 1
                reasons.append("fechamento_fraco")
            if float(rejection_payload.get("volume_ratio", 0.0) or 0.0) >= float(
                getattr(config, "SHORT_REVERSAL_MIN_VOLUME_RATIO", 1.15) or 1.15
            ):
                score += 1
                reasons.append(f"volume={float(rejection_payload.get('volume_ratio', 0.0) or 0.0):.2f}x")
            if float(rejection_payload.get("rsi_delta", 0.0) or 0.0) < 0:
                score += 1
                reasons.append("rsi_perde_forca")
            if float(rejection_payload.get("macd_hist_delta", 0.0) or 0.0) < 0:
                score += 1
                reasons.append("macd_piora")
            if bool(rejection_payload.get("lose_fast")) or bool(rejection_payload.get("breakdown_reclaim")):
                score += 1
                reasons.append("perde_media")

            min_rejection_score = int(getattr(config, "SHORT_REVERSAL_MIN_SCORE", 5) or 5)
            if score >= min_rejection_score:
                return _build_signal_payload(
                    signal="sell",
                    reason=f"reversal_rejection_short_score={score}|" + ",".join(reasons),
                    setup=setup,
                    row=row,
                    df=df,
                    index=index,
                )

            return {
                "signal": "hold",
                "reason": f"reversal_rejection_short_score_baixo={score}",
                "setup": setup,
            }

        if (
            bool(getattr(config, "SHORT_REQUIRE_STRICT_REGIME", False))
            and regime_name != "trend_bear"
            and not bool(getattr(config, "BYPASS_WEAK_REGIME_GATE", False))
        ):
            return {"signal": "hold", "reason": f"regime fraco para short ({regime_label})", "setup": setup}

        if row["close"] > row["ema_trend"]:
            return {"signal": "hold", "reason": "contexto macro altista (Preco > EMA Trend)", "setup": setup}

        if trend_strength_pct < config.MIN_TREND_STRENGTH_PCT_SHORT:
            return {"signal": "hold", "reason": f"tendencia de baixa sem abertura ({trend_strength_pct:.2f}%)", "setup": setup}

        if trend_context_pct < params.short_regime_gap_pct:
            return {"signal": "hold", "reason": f"contexto fraco para short ({trend_context_pct:.2f}%)", "setup": setup}

        if pd.isna(row["adx"]) or pd.isna(row["rsi"]) or pd.isna(row["vol_ma"]):
            return {"signal": "hold", "reason": "indicadores técnicos calculando (NaN)", "setup": setup}

        if setup.get("setup") == "pullback_short":
            min_context_gap = float(
                getattr(config, "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", params.short_regime_gap_pct)
                or params.short_regime_gap_pct
            )
            min_adx = float(
                getattr(config, "SHORT_PULLBACK_MIN_ADX", config.SHORT_ADX_THRESHOLD) or config.SHORT_ADX_THRESHOLD
            )
            min_trend_strength = float(
                getattr(config, "SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT", config.MIN_TREND_STRENGTH_PCT_SHORT)
                or config.MIN_TREND_STRENGTH_PCT_SHORT
            )
            if trend_context_pct < min_context_gap:
                return {
                    "signal": "hold",
                    "reason": f"pullback_short_contexto_fraco={trend_context_pct:.2f}",
                    "setup": setup,
                }
            if float(row["adx"]) < min_adx:
                return {
                    "signal": "hold",
                    "reason": f"pullback_short_adx_fraco={float(row['adx']):.2f}",
                    "setup": setup,
                }
            if trend_strength_pct < min_trend_strength:
                return {
                    "signal": "hold",
                    "reason": f"pullback_short_tendencia_fraca={trend_strength_pct:.2f}",
                    "setup": setup,
                }

        if setup.get("setup") == "trend_resume_short":
            blocked_resume_hours = set(getattr(config, "TREND_RESUME_SHORT_BLOCKED_ENTRY_HOURS_UTC", []) or [])
            if signal_timestamp is not None and signal_timestamp.hour in blocked_resume_hours:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_short_hora_bloqueada={signal_timestamp.hour:02d} UTC",
                    "setup": setup,
                }
            min_context_gap = float(getattr(config, "TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT", 0.0) or 0.0)
            min_adx = float(getattr(config, "TREND_RESUME_SHORT_MIN_ADX", 0.0) or 0.0)
            require_breakdown_confirmation = bool(
                getattr(config, "TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION", False)
            )
            breakdown_buffer = float(getattr(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.0) or 0.0)
            breakdown_trigger = prev["low"] * (1 - breakdown_buffer / 100)
            if trend_context_pct < min_context_gap:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_short_contexto_fraco={trend_context_pct:.2f}",
                    "setup": setup,
                }
            if float(row["adx"]) < min_adx:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_short_adx_fraco={float(row['adx']):.2f}",
                    "setup": setup,
                }
            if require_breakdown_confirmation and float(row["close"]) >= breakdown_trigger:
                return {
                    "signal": "hold",
                    "reason": f"trend_resume_short_sem_breakdown={float(row['close']):.4f}",
                    "setup": setup,
                }

        if setup.get("setup") == "relief_rally_short":
            min_context_gap = float(
                getattr(config, "RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT", params.short_regime_gap_pct)
                or params.short_regime_gap_pct
            )
            min_adx = float(
                getattr(config, "RELIEF_RALLY_SHORT_MIN_ADX", config.SHORT_ADX_THRESHOLD) or config.SHORT_ADX_THRESHOLD
            )
            min_rsi = float(getattr(config, "SHORT_RSI_MIN_RELIEF_RALLY", 50.0) or 50.0)
            max_rsi = float(getattr(config, "SHORT_RSI_MAX_RELIEF_RALLY", 65.0) or 65.0)
            if trend_context_pct < min_context_gap:
                return {
                    "signal": "hold",
                    "reason": f"relief_rally_short_contexto_fraco={trend_context_pct:.2f}",
                    "setup": setup,
                }
            if float(row["adx"]) < min_adx:
                return {
                    "signal": "hold",
                    "reason": f"relief_rally_short_adx_fraco={float(row['adx']):.2f}",
                    "setup": setup,
                }
            if float(row["rsi"]) < min_rsi or float(row["rsi"]) > max_rsi:
                return {
                    "signal": "hold",
                    "reason": f"relief_rally_short_rsi_fora={float(row['rsi']):.2f}",
                    "setup": setup,
                }

        candle_range = row["high"] - row["low"]
        lower_wick = min(row["open"], row["close"]) - row["low"]
        wick_limit = (
            config.CANDLE_WICK_REJECTION_RATIO_SHORT_RELIEF
            if setup.get("setup") == "relief_rally_short"
            else config.CANDLE_WICK_REJECTION_RATIO
        )
        if candle_range > 0 and (lower_wick / candle_range) > wick_limit:
            return {"signal": "hold", "reason": "rejeicao de fundo (pavio longo)", "setup": setup}

        if not _macd_direction_ok(row, "short"):
            return {
                "signal": "hold",
                "reason": f"macd_short_contra={float(row.get('macd_hist', 0.0) or 0.0):.6f}",
                "setup": setup,
            }

        if not _volume_ma_entry_ok(row):
            return {
                "signal": "hold",
                "reason": (
                    f"volume_abaixo_ma{int(getattr(config, 'VOLUME_MA_PERIOD', 21) or 21)}="
                    f"{float(row.get('volume', 0.0) or 0.0):.4f}/"
                    f"{float(row.get('vol_ma', 0.0) or 0.0):.4f}"
                ),
                "setup": setup,
            }

        score = 0
        reasons = []
        slope_lookback = max(int(params.short_slope_lookback), 1)
        trend_lookback = max(int(params.short_trend_ema_lookback), 1)
        slope_index = index - slope_lookback if index >= 0 else len(df) + index - slope_lookback
        trend_anchor_index = index - trend_lookback if index >= 0 else len(df) + index - trend_lookback
        slope_row = df.iloc[max(slope_index, 0)]
        trend_anchor_row = df.iloc[max(trend_anchor_index, 0)]
        fast_slow_gap_pct = (row["ema_slow"] - row["ema_fast"]) / row["close"] * 100
        distance_trend = (row["ema_trend"] - row["close"]) / row["close"] * 100
        breakdown_buffer = float(getattr(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.0) or 0.0)
        breakdown_trigger = prev["low"] * (1 - breakdown_buffer / 100)
        row_rsi = row["rsi"]
        prev_rsi = prev["rsi"]
        rsi_falling = bool(pd.notna(row_rsi) and pd.notna(prev_rsi) and float(row_rsi) < float(prev_rsi))
        candle_state = _resolve_candle_state(row, prev)

        if (
            row["close"] < row["ema_trend"]
            and row["ema_trend"] < prev["ema_trend"]
            and prev["ema_trend"] <= trend_anchor_row["ema_trend"]
        ):
            score += 1
            reasons.append("tendencia_macro")

        if (
            row["ema_fast"] < row["ema_slow"] < row["ema_trend"]
            and fast_slow_gap_pct >= params.short_fast_slow_gap_pct
        ):
            score += 1
            reasons.append("ema_estrutura")

        if row["ema_fast"] < slope_row["ema_fast"] and row["ema_slow"] <= slope_row["ema_slow"]:
            score += 1
            reasons.append("momentum_media")

        if distance_trend <= params.short_max_distance_pct:
            score += 1
            reasons.append("nao_estendido")

        if setup.get("setup") == "pullback_short":
            if row["high"] >= row["ema_fast"] * (1 - (params.pullback_buffer_pct / 2) / 100):
                if bool(getattr(config, "EXPERIMENTAL_SHORT_SIDE_LOGIC", False)):
                    min_context_gap = float(getattr(config, "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", params.short_regime_gap_pct) or params.short_regime_gap_pct)
                    min_adx = float(getattr(config, "SHORT_PULLBACK_MIN_ADX", config.SHORT_ADX_THRESHOLD) or config.SHORT_ADX_THRESHOLD)
                    rally_failure = bool(candle_state["close_below_ema_fast"]) and (
                        bool(candle_state["close_below_prev_close"]) or bool(row["close"] < breakdown_trigger)
                    )
                    context_ok = trend_context_pct >= min_context_gap
                    adx_ok = float(row["adx"]) >= min_adx

                    if rally_failure and context_ok and adx_ok:
                        score += 2
                        reasons.append("falha_rali")
                    elif rally_failure:
                        score += 1
                        reasons.append("rali_falhando")
                    else:
                        reasons.append("pullback")
                else:
                    score += 1
                    reasons.append("pullback")
        elif setup.get("setup") == "relief_rally_short":
            if row["close"] > prev["close"] and row["close"] < row["ema_slow"]:
                score += 1
                reasons.append("relief_rally")
        elif row["close"] < row["ema_fast"]:
            score += 1
            reasons.append("trend_resume")

        if rsi_falling and row["rsi"] < 50:
            score += 1
            reasons.append("rsi_baixa")

        if setup.get("setup") == "relief_rally_short":
            if rsi_falling and row["rsi"] >= config.SHORT_RSI_MIN_RELIEF_RALLY and row["rsi"] <= 65:
                score += 1
                reasons.append("rsi_relief")
        elif rsi_falling and config.SHORT_RSI_MIN <= row["rsi"] <= max(params.sell_rsi_ceiling, 40.0):
            score += 1
            reasons.append("rsi_setup")

        if row["adx"] >= config.SHORT_ADX_THRESHOLD:
            score += 1
            reasons.append("adx_ok")

        if row["volume"] >= row["vol_ma"] * config.SHORT_VOLUME_RATIO_REQUIRED:
            score += 1
            reasons.append("volume_ok")

        if setup.get("setup") == "relief_rally_short":
            if row["high"] >= prev["high"] or row["close"] > prev["close"]:
                score += 1
                reasons.append("relief_confirm")
        elif row["close"] < breakdown_trigger:
            score += 1
            reasons.append("breakdown")

        if bool(getattr(config, "DISABLE_SHORT_SCORE_GATE", False)):
            return _build_signal_payload(
                signal="sell",
                reason=f"short_score={score}|" + ",".join(reasons),
                setup=setup,
                row=row,
                df=df,
                index=index,
            )

        min_short_score = getattr(config, "MIN_SHORT_SCORE", 7)
        if score >= min_short_score:
            return _build_signal_payload(
                signal="sell",
                reason=f"short_score={score}|" + ",".join(reasons),
                setup=setup,
                row=row,
                df=df,
                index=index,
            )

        return {"signal": "hold", "reason": f"short_score_baixo={score}", "setup": setup}

    return {"signal": "hold", "reason": "sem gatilho", "setup": setup}
