from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

import config


MARKET_STRUCTURE_COLUMNS = (
    "recent_high_20",
    "recent_low_20",
    "recent_high_32",
    "recent_low_32",
    "distance_to_recent_high_pct",
    "distance_to_recent_low_pct",
    "close_position",
    "lower_wick_ratio",
    "upper_wick_ratio",
    "volume_ratio",
    "sweep_low_detected",
    "sweep_high_detected",
    "reclaim_recent_low",
    "reject_recent_high",
    "space_to_next_resistance_pct",
    "space_to_next_support_pct",
)


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


def _effective_index(df: pd.DataFrame, index: int = -1) -> int:
    if df.empty:
        return -1
    resolved = len(df) + index if index < 0 else index
    return max(min(resolved, len(df) - 1), 0)


def _pct_distance(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return (numerator / denominator.replace(0, pd.NA)) * 100.0


def annotate_market_structure(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(out.columns)):
        return out

    open_ = pd.to_numeric(out["open"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    close = pd.to_numeric(out["close"], errors="coerce")
    volume = pd.to_numeric(out["volume"], errors="coerce")

    out["recent_high_20"] = high.shift(1).rolling(20, min_periods=1).max()
    out["recent_low_20"] = low.shift(1).rolling(20, min_periods=1).min()

    structure_lookback = max(int(getattr(config, "MARKET_STRUCTURE_LOOKBACK", 32) or 32), 3)
    out["recent_high_32"] = high.shift(1).rolling(structure_lookback, min_periods=1).max()
    out["recent_low_32"] = low.shift(1).rolling(structure_lookback, min_periods=1).min()

    out["distance_to_recent_high_pct"] = _pct_distance(out["recent_high_32"] - close, close)
    out["distance_to_recent_low_pct"] = _pct_distance(close - out["recent_low_32"], close)

    candle_range = (high - low).replace(0, pd.NA)
    out["close_position"] = ((close - low) / candle_range).fillna(0.5).clip(lower=0.0, upper=1.0)
    out["lower_wick_ratio"] = ((pd.concat([open_, close], axis=1).min(axis=1) - low) / candle_range).fillna(0.0)
    out["upper_wick_ratio"] = ((high - pd.concat([open_, close], axis=1).max(axis=1)) / candle_range).fillna(0.0)

    if "vol_ma" in out.columns:
        volume_base = pd.to_numeric(out["vol_ma"], errors="coerce")
    else:
        volume_base = volume.shift(1).rolling(20, min_periods=1).mean()
    fallback_volume_base = volume.shift(1).rolling(20, min_periods=1).mean()
    volume_base = volume_base.where(volume_base > 0, fallback_volume_base)
    out["volume_ratio"] = (volume / volume_base.replace(0, pd.NA)).fillna(0.0)

    sweep_lookback = max(int(getattr(config, "LIQUIDITY_SWEEP_LOOKBACK", 24) or 24), 3)
    sweep_ref_low = low.shift(1).rolling(sweep_lookback, min_periods=2).min()
    sweep_ref_high = high.shift(1).rolling(sweep_lookback, min_periods=2).max()
    min_break_pct = max(float(getattr(config, "LIQUIDITY_SWEEP_MIN_BREAK_PCT", 0.15) or 0.15), 0.0)
    max_break_pct = max(float(getattr(config, "LIQUIDITY_SWEEP_MAX_BREAK_PCT", 2.5) or 2.5), min_break_pct)
    min_reclaim_pct = max(float(getattr(config, "LIQUIDITY_SWEEP_MIN_RECLAIM_PCT", 0.20) or 0.20), 0.0)

    break_low_pct = _pct_distance(sweep_ref_low - low, sweep_ref_low).clip(lower=0.0)
    break_high_pct = _pct_distance(high - sweep_ref_high, sweep_ref_high).clip(lower=0.0)
    out["liquidity_sweep_recent_low"] = sweep_ref_low
    out["liquidity_sweep_recent_high"] = sweep_ref_high
    out["liquidity_sweep_low_break_pct"] = break_low_pct.fillna(0.0)
    out["liquidity_sweep_high_break_pct"] = break_high_pct.fillna(0.0)
    out["sweep_low_detected"] = break_low_pct.between(min_break_pct, max_break_pct, inclusive="both").fillna(False)
    out["sweep_high_detected"] = break_high_pct.between(min_break_pct, max_break_pct, inclusive="both").fillna(False)

    bounce_from_low_pct = _pct_distance(close - low, low).clip(lower=0.0)
    pullback_from_high_pct = _pct_distance(high - close, high).clip(lower=0.0)
    out["reclaim_recent_low"] = (
        out["sweep_low_detected"]
        & ((close >= sweep_ref_low) | (bounce_from_low_pct >= min_reclaim_pct))
    ).fillna(False)
    out["reject_recent_high"] = (
        out["sweep_high_detected"]
        & ((close <= sweep_ref_high) | (pullback_from_high_pct >= min_reclaim_pct))
    ).fillna(False)

    out["space_to_next_resistance_pct"] = out["distance_to_recent_high_pct"].clip(lower=0.0)
    out["space_to_next_support_pct"] = out["distance_to_recent_low_pct"].clip(lower=0.0)
    return out


def analyze_market_structure(df: pd.DataFrame, index: int = -1) -> Dict[str, Any]:
    if df is None or df.empty:
        return {"available": False, "reason": "market_structure_sem_dados"}

    frame = df if set(MARKET_STRUCTURE_COLUMNS).issubset(set(df.columns)) else annotate_market_structure(df)
    idx = _effective_index(frame, index=index)
    if idx < 0:
        return {"available": False, "reason": "market_structure_sem_dados"}

    row = frame.iloc[idx]
    close = _safe_float(row.get("close"))
    recent_high = _safe_float(row.get("recent_high_32"))
    recent_low = _safe_float(row.get("recent_low_32"))
    volume_ratio = _safe_float(row.get("volume_ratio"))
    min_volume_ratio = max(float(getattr(config, "LIQUIDITY_SWEEP_MIN_VOLUME_RATIO", 1.20) or 1.20), 0.0)

    breakout_with_volume = bool(close > recent_high > 0 and volume_ratio >= min_volume_ratio)
    breakdown_with_volume = bool(close < recent_low and recent_low > 0 and volume_ratio >= min_volume_ratio)

    return {
        "available": bool(close > 0),
        "recent_high_20": _safe_float(row.get("recent_high_20")),
        "recent_low_20": _safe_float(row.get("recent_low_20")),
        "recent_high_32": recent_high,
        "recent_low_32": recent_low,
        "distance_to_recent_high_pct": _safe_float(row.get("distance_to_recent_high_pct")),
        "distance_to_recent_low_pct": _safe_float(row.get("distance_to_recent_low_pct")),
        "close_position": _safe_float(row.get("close_position"), 0.5),
        "lower_wick_ratio": _safe_float(row.get("lower_wick_ratio")),
        "upper_wick_ratio": _safe_float(row.get("upper_wick_ratio")),
        "volume_ratio": volume_ratio,
        "sweep_low_detected": bool(row.get("sweep_low_detected", False)),
        "sweep_high_detected": bool(row.get("sweep_high_detected", False)),
        "reclaim_recent_low": bool(row.get("reclaim_recent_low", False)),
        "reject_recent_high": bool(row.get("reject_recent_high", False)),
        "space_to_next_resistance_pct": _safe_float(row.get("space_to_next_resistance_pct")),
        "space_to_next_support_pct": _safe_float(row.get("space_to_next_support_pct")),
        "liquidity_sweep_recent_low": _safe_float(row.get("liquidity_sweep_recent_low")),
        "liquidity_sweep_recent_high": _safe_float(row.get("liquidity_sweep_recent_high")),
        "liquidity_sweep_low_break_pct": _safe_float(row.get("liquidity_sweep_low_break_pct")),
        "liquidity_sweep_high_break_pct": _safe_float(row.get("liquidity_sweep_high_break_pct")),
        "breakout_with_volume": breakout_with_volume,
        "breakdown_with_volume": breakdown_with_volume,
    }


def detect_liquidity_sweep_reversal_long(df: pd.DataFrame, index: int = -1) -> Optional[Dict[str, Any]]:
    if not bool(getattr(config, "ENABLE_LIQUIDITY_SWEEP_REVERSAL_LONG", True)):
        return None

    idx = _effective_index(df, index=index)
    if idx <= 0:
        return None

    frame = df if set(MARKET_STRUCTURE_COLUMNS).issubset(set(df.columns)) else annotate_market_structure(df)
    row = frame.iloc[idx]
    prev = frame.iloc[idx - 1]
    metrics = analyze_market_structure(frame, index=idx)

    if not bool(metrics.get("sweep_low_detected")):
        return None
    if not bool(metrics.get("reclaim_recent_low")):
        return None
    if _safe_float(metrics.get("lower_wick_ratio")) < float(
        getattr(config, "LIQUIDITY_SWEEP_MIN_LOWER_WICK_RATIO", 0.35) or 0.35
    ):
        return None
    if _safe_float(metrics.get("volume_ratio")) < float(
        getattr(config, "LIQUIDITY_SWEEP_MIN_VOLUME_RATIO", 1.20) or 1.20
    ):
        return None

    current_rsi = _safe_float(row.get("rsi"))
    prev_rsi = _safe_float(prev.get("rsi"), current_rsi)
    rsi_max = float(getattr(config, "LIQUIDITY_SWEEP_RSI_MAX_AT_LOW_SWEEP", 45.0) or 45.0)
    rsi_recovering = bool(current_rsi > prev_rsi and min(current_rsi, prev_rsi) <= rsi_max)
    if not rsi_recovering:
        return None

    macd_hist = _safe_float(row.get("macd_hist"))
    prev_macd_hist = _safe_float(prev.get("macd_hist"), macd_hist)
    macd_improving = bool(macd_hist > prev_macd_hist)
    if bool(getattr(config, "LIQUIDITY_SWEEP_REQUIRE_MACD_IMPROVING", True)) and not macd_improving:
        return None

    close_position = _safe_float(metrics.get("close_position"), 0.5)
    close_price = _safe_float(row.get("close"))
    low_price = _safe_float(row.get("low"))
    bounce_from_low_pct = ((close_price - low_price) / low_price * 100.0) if low_price > 0 else 0.0

    confirmation = bool(
        close_price > _safe_float(row.get("ema_fast"))
        or close_price > _safe_float(prev.get("high"))
        or close_position >= 0.65
    )
    if bool(getattr(config, "LIQUIDITY_SWEEP_REQUIRE_CONFIRMATION", True)) and not confirmation:
        return None

    reasons = ["sweep_detected", "reclaim_confirmed"]
    score = 2
    checks = [
        ("pavio_inferior", _safe_float(metrics.get("lower_wick_ratio")) >= float(getattr(config, "LIQUIDITY_SWEEP_MIN_LOWER_WICK_RATIO", 0.35) or 0.35)),
        ("volume", _safe_float(metrics.get("volume_ratio")) >= float(getattr(config, "LIQUIDITY_SWEEP_MIN_VOLUME_RATIO", 1.20) or 1.20)),
        ("rsi_recuperando", rsi_recovering),
        ("macd_melhora", macd_improving),
        ("confirmacao", confirmation),
    ]
    for reason, passed in checks:
        if passed:
            score += 1
            reasons.append(reason)

    min_score = int(getattr(config, "LIQUIDITY_SWEEP_MIN_SCORE", 5) or 5)
    if score < min_score:
        return None

    return {
        "score": score,
        "reasons": reasons,
        "market_structure": metrics,
        "break_pct": round(_safe_float(metrics.get("liquidity_sweep_low_break_pct")), 4),
        "bounce_from_low_pct": round(bounce_from_low_pct, 4),
        "rsi_delta": round(current_rsi - prev_rsi, 4),
        "macd_hist_delta": round(macd_hist - prev_macd_hist, 8),
    }


def detect_liquidity_sweep_reversal_short(df: pd.DataFrame, index: int = -1) -> Optional[Dict[str, Any]]:
    if not bool(getattr(config, "ENABLE_LIQUIDITY_SWEEP_REVERSAL_SHORT", True)):
        return None

    idx = _effective_index(df, index=index)
    if idx <= 0:
        return None

    frame = df if set(MARKET_STRUCTURE_COLUMNS).issubset(set(df.columns)) else annotate_market_structure(df)
    row = frame.iloc[idx]
    prev = frame.iloc[idx - 1]
    metrics = analyze_market_structure(frame, index=idx)

    if not bool(metrics.get("sweep_high_detected")):
        return None
    if not bool(metrics.get("reject_recent_high")):
        return None
    if _safe_float(metrics.get("upper_wick_ratio")) < float(
        getattr(config, "LIQUIDITY_SWEEP_MIN_UPPER_WICK_RATIO", 0.35) or 0.35
    ):
        return None
    if _safe_float(metrics.get("volume_ratio")) < float(
        getattr(config, "LIQUIDITY_SWEEP_MIN_VOLUME_RATIO", 1.20) or 1.20
    ):
        return None

    current_rsi = _safe_float(row.get("rsi"))
    prev_rsi = _safe_float(prev.get("rsi"), current_rsi)
    rsi_min = float(getattr(config, "LIQUIDITY_SWEEP_RSI_MIN_AT_HIGH_SWEEP", 55.0) or 55.0)
    rsi_weakening = bool(current_rsi < prev_rsi and max(current_rsi, prev_rsi) >= rsi_min)
    if not rsi_weakening:
        return None

    macd_hist = _safe_float(row.get("macd_hist"))
    prev_macd_hist = _safe_float(prev.get("macd_hist"), macd_hist)
    macd_worsening = bool(macd_hist < prev_macd_hist)
    if bool(getattr(config, "LIQUIDITY_SWEEP_REQUIRE_MACD_IMPROVING", True)) and not macd_worsening:
        return None

    close_position = _safe_float(metrics.get("close_position"), 0.5)
    high_price = _safe_float(row.get("high"))
    close_price = _safe_float(row.get("close"))
    pullback_from_high_pct = ((high_price - close_price) / high_price * 100.0) if high_price > 0 else 0.0

    confirmation = bool(
        close_price < _safe_float(row.get("ema_fast"))
        or close_price < _safe_float(prev.get("low"))
        or close_position <= 0.35
    )
    if bool(getattr(config, "LIQUIDITY_SWEEP_REQUIRE_CONFIRMATION", True)) and not confirmation:
        return None

    reasons = ["sweep_detected", "reclaim_confirmed"]
    score = 2
    checks = [
        ("pavio_superior", _safe_float(metrics.get("upper_wick_ratio")) >= float(getattr(config, "LIQUIDITY_SWEEP_MIN_UPPER_WICK_RATIO", 0.35) or 0.35)),
        ("volume", _safe_float(metrics.get("volume_ratio")) >= float(getattr(config, "LIQUIDITY_SWEEP_MIN_VOLUME_RATIO", 1.20) or 1.20)),
        ("rsi_perde_forca", rsi_weakening),
        ("macd_piora", macd_worsening),
        ("confirmacao", confirmation),
    ]
    for reason, passed in checks:
        if passed:
            score += 1
            reasons.append(reason)

    min_score = int(getattr(config, "LIQUIDITY_SWEEP_MIN_SCORE", 5) or 5)
    if score < min_score:
        return None

    return {
        "score": score,
        "reasons": reasons,
        "market_structure": metrics,
        "break_pct": round(_safe_float(metrics.get("liquidity_sweep_high_break_pct")), 4),
        "pullback_from_high_pct": round(pullback_from_high_pct, 4),
        "rsi_delta": round(current_rsi - prev_rsi, 4),
        "macd_hist_delta": round(macd_hist - prev_macd_hist, 8),
    }


def evaluate_market_structure_guard(
    df: pd.DataFrame,
    *,
    index: int = -1,
    direction: str,
    setup_name: Optional[str] = None,
) -> Dict[str, Any]:
    metrics = analyze_market_structure(df, index=index)
    if not bool(getattr(config, "MARKET_STRUCTURE_GUARD_ENABLED", True)):
        return {"allowed": True, "reason": "market_structure_disabled", "market_structure": metrics}
    if not bool(metrics.get("available")):
        return {"allowed": True, "reason": "market_structure_unavailable", "market_structure": metrics}

    side = str(direction or "").strip().lower()
    setup_token = str(setup_name or "").strip().lower()
    guarded_level_setups = {
        "trend_resume_long",
        "trend_resume_short",
        "liquidity_sweep_reversal_long",
        "liquidity_sweep_reversal_short",
        "market_reading_long",
        "market_reading_short",
    }
    if setup_token and setup_token not in guarded_level_setups:
        return {
            "allowed": True,
            "reason": "market_structure_legacy_setup_bypass",
            "market_structure": metrics,
        }

    close = _safe_float((df.iloc[_effective_index(df, index=index)]).get("close"))
    min_space_pct = max(float(getattr(config, "MIN_SPACE_TO_TARGET_PCT", 0.60) or 0.60), 0.0)
    min_rr = max(float(getattr(config, "MIN_RISK_REWARD_BEFORE_ENTRY", 1.4) or 1.4), 0.0)
    near_resistance_pct = max(float(getattr(config, "NEAR_RESISTANCE_BLOCK_PCT", 0.25) or 0.25), 0.0)
    near_support_pct = max(float(getattr(config, "NEAR_SUPPORT_BLOCK_PCT", 0.25) or 0.25), 0.0)

    if side == "long":
        resistance = _safe_float(metrics.get("recent_high_32"))
        support = _safe_float(metrics.get("recent_low_32"))
        reward_pct = max(_safe_float(metrics.get("space_to_next_resistance_pct")), 0.0)
        risk_pct = ((close - support) / close * 100.0) if close > 0 and support > 0 and close > support else 0.0
        breakout_ok = bool(metrics.get("breakout_with_volume"))

        if resistance > 0 and reward_pct <= near_resistance_pct and not breakout_ok:
            return {
                "allowed": False,
                "reason": "long_near_resistance",
                "detail": "market_structure:near_resistance",
                "market_structure": metrics,
            }

        late_threshold = float(getattr(config, "LIQUIDITY_SWEEP_MAX_BOUNCE_FROM_LOW_PCT", 4.0) or 4.0)
        if setup_token.startswith("liquidity_sweep") and risk_pct > late_threshold:
            return {
                "allowed": False,
                "reason": "long_entrada_tardia",
                "detail": "market_structure:entrada_tardia",
                "market_structure": metrics,
            }

        if setup_token.startswith("liquidity_sweep") and resistance > 0 and support > 0 and close > support and not breakout_ok:
            rr = reward_pct / risk_pct if risk_pct > 0 else 0.0
            if reward_pct < min_space_pct or rr < min_rr:
                return {
                    "allowed": False,
                    "reason": "long_rr_insuficiente",
                    "detail": f"market_structure:rr_insuficiente rr={rr:.2f}",
                    "market_structure": {**metrics, "estimated_rr": round(rr, 4)},
                }

    if side == "short":
        resistance = _safe_float(metrics.get("recent_high_32"))
        support = _safe_float(metrics.get("recent_low_32"))
        reward_pct = max(_safe_float(metrics.get("space_to_next_support_pct")), 0.0)
        risk_pct = ((resistance - close) / close * 100.0) if close > 0 and resistance > close else 0.0
        breakdown_ok = bool(metrics.get("breakdown_with_volume"))

        if support > 0 and reward_pct <= near_support_pct and not breakdown_ok:
            return {
                "allowed": False,
                "reason": "short_near_support",
                "detail": "market_structure:near_support",
                "market_structure": metrics,
            }

        late_threshold = float(getattr(config, "LIQUIDITY_SWEEP_MAX_PULLBACK_FROM_HIGH_PCT", 4.0) or 4.0)
        if setup_token.startswith("liquidity_sweep") and risk_pct > late_threshold:
            return {
                "allowed": False,
                "reason": "short_entrada_tardia",
                "detail": "market_structure:entrada_tardia",
                "market_structure": metrics,
            }

        if setup_token.startswith("liquidity_sweep") and resistance > 0 and support > 0 and resistance > close and not breakdown_ok:
            rr = reward_pct / risk_pct if risk_pct > 0 else 0.0
            if reward_pct < min_space_pct or rr < min_rr:
                return {
                    "allowed": False,
                    "reason": "short_rr_insuficiente",
                    "detail": f"market_structure:rr_insuficiente rr={rr:.2f}",
                    "market_structure": {**metrics, "estimated_rr": round(rr, 4)},
                }

    return {"allowed": True, "reason": "market_structure_ok", "market_structure": metrics}
