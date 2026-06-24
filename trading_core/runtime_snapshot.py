from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from config import AppConfig, ProductionConfig
from strategy_engine import StrategyParams, generate_entry_signal
from trading_core.block_debug import emit_block_debug


def _coerce_float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return float(default)
    return float(numeric)


def _prefer_closed_candles(bot, df: Optional[pd.DataFrame]) -> pd.DataFrame:
    preferred = bot._prefer_closed_candles(df)
    if preferred is None:
        return pd.DataFrame()
    return preferred.copy()


def _build_wait_market_state(reason: str) -> Dict[str, Any]:
    return {
        "market_state": "neutral_chop",
        "market_bias": "neutral",
        "execution_mode": "standby",
        "reason": reason,
        "notes": [reason],
        "market_pattern": None,
        "setup_type": None,
    }


def _ensure_indicator_columns(bot, working_df: pd.DataFrame) -> pd.DataFrame:
    required_columns = {
        "ema_fast",
        "ema_slow",
        "ema_trend",
        "rsi",
        "atr",
        "atr_pct",
        "macd",
        "macd_signal",
        "volume_ma",
        "sma_21",
    }
    if required_columns.issubset(set(working_df.columns)):
        return working_df.copy()
    return bot.calculate_indicators(working_df)


def _resolve_resume_thresholds(bot) -> tuple[float, float]:
    return float(getattr(bot, "rsi_min", AppConfig.DEFAULT_RSI_MIN)), float(
        getattr(bot, "rsi_max", AppConfig.DEFAULT_RSI_MAX)
    )


def _derive_market_bias(row: pd.Series) -> str:
    ema_fast = _coerce_float(row.get("ema_fast"), 0.0)
    ema_slow = _coerce_float(row.get("ema_slow"), 0.0)
    ema_trend = _coerce_float(row.get("ema_trend"), 0.0)
    if ema_fast > ema_slow > ema_trend:
        return "bullish"
    if ema_fast < ema_slow < ema_trend:
        return "bearish"
    return "neutral"


def _analyze_resume_signal(
    df: pd.DataFrame,
    buy_threshold: float,
    sell_threshold: float,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < 3:
        return {
            "signal": "NEUTRO",
            "reason": "dados insuficientes para analise",
            "market_bias": "neutral",
            "structure_state": "flat",
            "price_location": "mid_range",
            "confirmation_state": "weak",
            "entry_quality": "bad",
            "entry_score": 0.0,
            "scenario_score": 0.0,
            "market_pattern": None,
            "setup_type": None,
            "rr_estimate": 0.0,
            "structural_stop_price": None,
            "structural_take_profit_price": None,
            "risk_distance_pct": 0.0,
            "target_distance_pct": 0.0,
            "invalid_if": None,
            "target_reason": None,
        }

    signal_result = generate_entry_signal(
        df,
        StrategyParams(
            buy_rsi_floor=float(buy_threshold),
            sell_rsi_ceiling=float(sell_threshold),
        ),
        index=-1,
    )
    row = df.iloc[-1]
    prev = df.iloc[-2]
    close_price = _coerce_float(row.get("close"), 0.0)
    ema_fast = _coerce_float(row.get("ema_fast"), close_price)
    ema_slow = _coerce_float(row.get("ema_slow"), close_price)
    ema_trend = _coerce_float(row.get("ema_trend"), close_price)
    current_rsi = _coerce_float(row.get("rsi"), 50.0)
    prev_rsi = _coerce_float(prev.get("rsi"), current_rsi)
    setup_name = str(((signal_result.get("setup") or {}).get("setup")) or "").strip().lower() or None

    market_bias = _derive_market_bias(row)
    if setup_name in {"trend_resume_long", "trend_resume_short"}:
        structure_state = "trend_resume"
    elif setup_name in {"pullback_long", "pullback_short"}:
        structure_state = "pullback"
    else:
        structure_state = "uptrend" if market_bias == "bullish" else "downtrend" if market_bias == "bearish" else "flat"
    if close_price > ema_fast:
        price_location = "above_ema_fast"
    elif close_price < ema_fast:
        price_location = "below_ema_fast"
    else:
        price_location = "mid_range"

    stop_loss_pct = float(stop_loss_pct or ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT or 0.8)
    take_profit_pct = float(take_profit_pct or ProductionConfig.DEFAULT_LIVE_TAKE_PROFIT_PCT or 1.8)
    
    # Normalização robusta de percentual (evita erro se vier 1.0 representando 1%)
    _sl = stop_loss_pct / 100 if stop_loss_pct >= 0.1 else stop_loss_pct
    _tp = take_profit_pct / 100 if take_profit_pct >= 0.1 else take_profit_pct
    
    rr_estimate = round(float(take_profit_pct) / float(stop_loss_pct), 2) if stop_loss_pct > 0 else 0.0

    # Filtro Estrutural: Só permite COMPRA se o preço estiver acima da EMA de tendência (1h)
    # e VENDA se estiver abaixo. Isso evita "adivinhar" topos e fundos.
    is_above_trend = close_price > ema_trend

    if signal_result["signal"] == "buy" and is_above_trend:
        signal = "COMPRA"
        confirmation_state = "confirmed"
        entry_quality = "good"
        market_pattern = setup_name or "trend_resume_long"
        structural_stop_price = close_price * (1 - _sl)
        structural_take_profit_price = close_price * (1 + _tp)
        invalid_if = "Perder a EMA rapida e invalidar o impulso comprador."
        target_reason = "Alvo padrao baseado em continuidade de tendencia compradora."
    elif signal_result["signal"] == "sell" and not is_above_trend:
        signal = "VENDA"
        confirmation_state = "confirmed"
        entry_quality = "good"
        market_pattern = setup_name or "trend_resume_short"
        structural_stop_price = close_price * (1 + _sl)
        structural_take_profit_price = close_price * (1 - _tp)
        invalid_if = "Recuperar a EMA rapida e invalidar o impulso vendedor."
        target_reason = "Alvo padrao baseado em continuidade de tendencia vendedora."
    else:
        signal = "NEUTRO"
        confirmation_state = "waiting" if market_bias in {"bullish", "bearish"} else "weak"
        entry_quality = "ok" if market_bias in {"bullish", "bearish"} else "bad"
        market_pattern = None
        structural_stop_price = None
        structural_take_profit_price = None
        invalid_if = None
        target_reason = None

    rsi_strength = min(abs(current_rsi - 50.0), 25.0) / 25.0
    trend_strength = min(abs(ema_fast - ema_slow) / max(abs(close_price), 1e-9) * 1000, 1.0)
    crossover_bonus = 1.0 if (prev_rsi <= buy_threshold < current_rsi) or (prev_rsi >= sell_threshold > current_rsi) else 0.0
    entry_score = round((rsi_strength * 4.0) + (trend_strength * 3.0) + (crossover_bonus * 2.0) + (1.0 if signal != "NEUTRO" else 0.0), 2)
    scenario_score = round(entry_score if signal != "NEUTRO" else max(entry_score - 1.0, 0.0), 2)

    return {
        "signal": signal,
        "reason": signal_result["reason"],
        "market_bias": market_bias,
        "structure_state": structure_state,
        "price_location": price_location,
        "confirmation_state": confirmation_state,
        "entry_quality": entry_quality,
        "entry_score": entry_score,
        "scenario_score": scenario_score,
        "market_pattern": market_pattern,
        "setup_type": market_pattern,
        "rr_estimate": rr_estimate,
        "structural_stop_price": structural_stop_price,
        "structural_take_profit_price": structural_take_profit_price,
        "risk_distance_pct": round(stop_loss_pct * 100, 4),
        "target_distance_pct": round(take_profit_pct * 100, 4),
        "invalid_if": invalid_if,
        "target_reason": target_reason,
    }


def _build_resume_context_evaluation(
    bot,
    context_df: Optional[pd.DataFrame],
    buy_threshold: float,
    sell_threshold: float,
) -> Optional[Dict[str, Any]]:
    del buy_threshold, sell_threshold
    if context_df is None or context_df.empty:
        return None

    prepared_context = _ensure_indicator_columns(bot, context_df)
    last_row = prepared_context.iloc[-1]
    market_bias = _derive_market_bias(last_row)
    context_strength = round(min(abs(_coerce_float(last_row.get("rsi"), 50.0) - 50.0) / 5.0, 10.0), 2)
    reason = (
        "Contexto favorece continuidade compradora."
        if market_bias == "bullish"
        else "Contexto favorece continuidade vendedora."
        if market_bias == "bearish"
        else "Contexto lateral sem vies dominante."
    )
    return {
        "market_bias": market_bias,
        "bias": market_bias,
        "context_strength": context_strength,
        "is_tradeable": market_bias in {"bullish", "bearish"},
        "reason": reason,
    }


def _build_resume_regime_evaluation(df: pd.DataFrame, timeframe: Optional[str] = None) -> Dict[str, Any]:
    if df is None or df.empty:
        return {
            "timeframe": timeframe,
            "regime": "range",
            "regime_score": 0.0,
            "market_bias": "neutral",
            "adx": 0.0,
            "atr_pct": 0.0,
            "ema_distance_pct": 0.0,
            "ema_slope": 0.0,
            "volatility_state": "low_volatility",
            "trend_state": "range",
            "parabolic": False,
            "legacy_regime": "ranging",
            "price_above_ema_200": False,
            "is_tradeable": False,
            "has_minimum_history": False,
            "notes": ["Sem dados para avaliar regime."],
            "reason": "Sem dados para avaliar regime.",
        }

    row = df.iloc[-1]
    market_bias = _derive_market_bias(row)
    close_price = _coerce_float(row.get("close"), 0.0)
    ema_fast = _coerce_float(row.get("ema_fast"), close_price)
    ema_slow = _coerce_float(row.get("ema_slow"), close_price)
    ema_trend = _coerce_float(row.get("ema_trend"), close_price)
    ema_distance_pct = abs(ema_fast - ema_trend) / max(abs(close_price), 1e-9) * 100
    ema_slope = 0.0
    if len(df) >= 6:
        ema_slope = (
            _coerce_float(df["ema_slow"].iloc[-1], ema_slow)
            - _coerce_float(df["ema_slow"].iloc[-6], ema_slow)
        ) / max(abs(close_price), 1e-9)
    atr_pct = _coerce_float(row.get("atr_pct"), 0.0)
    trend_state = "trend" if market_bias in {"bullish", "bearish"} else "range"
    volatility_state = "elevated" if atr_pct >= 1.0 else "normal" if atr_pct >= 0.3 else "low_volatility"
    regime_score = round(min((ema_distance_pct * 4.0) + (atr_pct * 0.6), 10.0), 2)
    reason = (
        "Regime direcional comprador."
        if market_bias == "bullish"
        else "Regime direcional vendedor."
        if market_bias == "bearish"
        else "Regime lateral."
    )
    return {
        "timeframe": timeframe,
        "regime": "trend" if trend_state == "trend" else "range",
        "regime_score": regime_score,
        "market_bias": market_bias,
        "adx": round(min(regime_score * 2.5, 50.0), 2),
        "atr_pct": round(atr_pct, 4),
        "ema_distance_pct": round(ema_distance_pct, 4),
        "ema_slope": round(ema_slope, 6),
        "volatility_state": volatility_state,
        "trend_state": trend_state,
        "parabolic": False,
        "legacy_regime": "trending" if trend_state == "trend" else "ranging",
        "price_above_ema_200": bool(close_price > ema_trend),
        "is_tradeable": market_bias in {"bullish", "bearish"},
        "has_minimum_history": len(df) >= 30,
        "notes": [reason],
        "reason": reason,
    }


def _evaluate_indicator_objective_gate(
    analysis: Dict[str, Any],
    context_evaluation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    context_evaluation = context_evaluation or {}
    signal_direction = analysis.get("signal")
    context_bias = str(context_evaluation.get("market_bias") or context_evaluation.get("bias") or "neutral")
    signal_bias = "bullish" if signal_direction == "COMPRA" else "bearish" if signal_direction == "VENDA" else "neutral"

    failed_flags: list[str] = []
    if signal_direction == "NEUTRO":
        failed_flags.append("neutral_signal")
    if analysis.get("entry_score", 0.0) < 6.0:
        failed_flags.append("low_entry_score")
    if context_bias not in {"neutral", signal_bias} and signal_bias != "neutral":
        failed_flags.append("context_misaligned")

    objective_passed = signal_direction in {"COMPRA", "VENDA"} and "low_entry_score" not in failed_flags and "context_misaligned" not in failed_flags
    objective_quality = analysis.get("entry_quality") if objective_passed else "bad"
    rejection_reason = None if objective_passed else analysis.get("reason")

    if not objective_passed:
        emit_block_debug(
            "runtime_snapshot.objective_gate_failed",
            signal_direction=signal_direction,
            entry_score=round(float(analysis.get("entry_score", 0.0) or 0.0), 2),
            context_bias=context_bias,
            failed_flags=failed_flags,
            rejection_reason=rejection_reason,
            market_pattern=analysis.get("market_pattern"),
            setup_type=analysis.get("setup_type"),
            confirmation_state=analysis.get("confirmation_state"),
        )

    return {
        "objective_passed": objective_passed,
        "objective_quality": objective_quality,
        "signal_direction": signal_direction,
        "context_bias": context_bias,
        "context_aligned": context_bias in {"neutral", signal_bias},
        "context_tradeable": context_bias in {"bullish", "bearish"},
        "passes_score_floor": analysis.get("entry_score", 0.0) >= 6.0,
        "failed_flags": failed_flags,
        "critical_failed_flags": list(failed_flags),
        "rejection_reason": rejection_reason,
    }


def _build_empty_resume_snapshot(reason: str, timeframe: Optional[str]) -> Dict[str, Any]:
    neutral_market_state = _build_wait_market_state(reason)
    return {
        "analysis": {
            "signal": "NEUTRO",
            "side": None,
            "reason": reason,
            "market_bias": "neutral",
            "atr_pct": 0.0,
            "confirmation_state": "weak",
            "price_location": "mid_range",
            "entry_score": 0.0,
            "scenario_score": 0.0,
            "market_pattern": None,
            "setup_type": None,
            "market_state": "neutral_chop",
            "structure_state": "flat",
            "entry_quality": "bad",
        },
        "context_evaluation": {
            "market_bias": "neutral",
            "bias": "neutral",
            "context_strength": 0.0,
            "is_tradeable": False,
            "reason": reason,
        },
        "regime_evaluation": {
            "timeframe": timeframe,
            "regime": "range",
            "regime_score": 0.0,
            "market_bias": "neutral",
            "adx": 0.0,
            "atr_pct": 0.0,
            "ema_distance_pct": 0.0,
            "ema_slope": 0.0,
            "volatility_state": "low_volatility",
            "trend_state": "range",
            "parabolic": False,
            "legacy_regime": "ranging",
            "price_above_ema_200": False,
            "is_tradeable": False,
            "has_minimum_history": False,
            "notes": [reason],
            "reason": reason,
        },
        "structure_evaluation": {
            "structure_state": "flat",
            "structure_quality": 0.0,
            "price_location": "mid_range",
            "notes": [reason],
            "breakout_pressure": False,
            "breakout_pressure_side": "",
            "trend_bias": "neutral",
            "timeframe": timeframe,
            "has_minimum_history": False,
        },
        "confirmation_evaluation": {
            "confirmation_state": "weak",
            "confirmation_score": 0.0,
            "hypothesis_side": None,
            "notes": [reason],
            "conflicts": [reason],
            "has_minimum_history": False,
        },
        "entry_evaluation": {
            "entry_quality": "bad",
            "entry_score": 0.0,
            "objective_passed": False,
            "objective_quality": "bad",
            "market_pattern": None,
            "setup_type": None,
            "rr_estimate": 0.0,
            "rejection_reason": reason,
            "notes": [reason],
            "minimum_scenario_score": 6.0,
            "entry_reason": None,
            "has_minimum_history": False,
        },
        "scenario_evaluation": {
            "scenario_score": 0.0,
            "scenario_grade": "D",
            "pullback_intensity": "not_applicable",
            "pullback_score": 0.0,
            "notes": [reason],
            "has_minimum_history": False,
        },
        "market_state_evaluation": neutral_market_state,
        "trade_decision": {
            "action": "wait",
            "confidence": 0.0,
            "market_bias": "neutral",
            "market_state": "neutral_chop",
            "execution_mode": "standby",
            "market_pattern": None,
            "setup_type": None,
            "entry_reason": None,
            "block_reason": reason,
            "invalid_if": None,
        },
    }


def _build_fallback_context_evaluation(analysis: Dict[str, Any]) -> Dict[str, Any]:
    market_bias = str(analysis.get("market_bias") or "neutral")
    return {
        "market_bias": market_bias,
        "bias": market_bias,
        "context_strength": 6.0 if market_bias in {"bullish", "bearish"} else 3.0,
        "is_tradeable": market_bias in {"bullish", "bearish"},
        "reason": analysis.get("reason"),
    }


def _resolve_rr_estimate(
    analysis: Dict[str, Any],
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
) -> float:
    rr_estimate = float(analysis.get("rr_estimate", 0.0) or 0.0)
    if rr_estimate <= 0 and float(stop_loss_pct or 0.0) > 0 and float(take_profit_pct or 0.0) > 0:
        rr_estimate = float(take_profit_pct) / float(stop_loss_pct)
    elif rr_estimate <= 0 and float(ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT or 0.0) > 0:
        rr_estimate = (
            float(ProductionConfig.DEFAULT_LIVE_TAKE_PROFIT_PCT)
            / float(ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT)
        )
    return rr_estimate


def _build_structure_evaluation(analysis: Dict[str, Any], timeframe: Optional[str]) -> Dict[str, Any]:
    return {
        "structure_state": analysis["structure_state"],
        "structure_quality": 7.0 if analysis["signal"] in {"COMPRA", "VENDA"} else 4.0,
        "price_location": analysis["price_location"],
        "notes": [analysis["reason"]],
        "breakout_pressure": False,
        "breakout_pressure_side": "",
        "trend_bias": analysis["market_bias"],
        "timeframe": timeframe,
        "has_minimum_history": True,
    }


def _build_confirmation_evaluation(analysis: Dict[str, Any]) -> Dict[str, Any]:
    confirmation_state = analysis["confirmation_state"]
    return {
        "confirmation_state": confirmation_state,
        "confirmation_score": 7.4 if confirmation_state == "confirmed" else 5.6 if confirmation_state == "waiting" else 3.5,
        "hypothesis_side": analysis["market_bias"] if analysis["market_bias"] in {"bullish", "bearish"} else None,
        "notes": [analysis["reason"]],
        "conflicts": [] if analysis["signal"] in {"COMPRA", "VENDA"} else [analysis["reason"]],
        "has_minimum_history": True,
    }


def _build_entry_evaluation(
    analysis: Dict[str, Any],
    objective_gate: Dict[str, Any],
    rr_estimate: float,
) -> Dict[str, Any]:
    return {
        "entry_quality": analysis["entry_quality"] if objective_gate["objective_passed"] else "bad",
        "entry_score": round(float(analysis["entry_score"]), 2),
        "objective_passed": bool(objective_gate["objective_passed"]),
        "objective_quality": str(objective_gate["objective_quality"]),
        "market_pattern": analysis.get("market_pattern"),
        "setup_type": analysis.get("setup_type"),
        "signal_direction": objective_gate["signal_direction"],
        "context_bias": objective_gate["context_bias"],
        "context_aligned": bool(objective_gate["context_aligned"]),
        "context_tradeable": bool(objective_gate["context_tradeable"]),
        "passes_score_floor": bool(objective_gate["passes_score_floor"]),
        "failed_flags": list(objective_gate["failed_flags"]),
        "critical_failed_flags": list(objective_gate["critical_failed_flags"]),
        "rr_estimate": round(float(rr_estimate), 2),
        "structural_stop_price": analysis.get("structural_stop_price"),
        "structural_take_profit_price": analysis.get("structural_take_profit_price"),
        "risk_distance_pct": float(analysis.get("risk_distance_pct", 0.0) or 0.0),
        "target_distance_pct": float(analysis.get("target_distance_pct", 0.0) or 0.0),
        "rejection_reason": (
            None
            if bool(objective_gate["objective_passed"])
            else objective_gate["rejection_reason"] or analysis["reason"]
        ),
        "notes": [analysis["reason"]],
        "minimum_scenario_score": 6.0,
        "entry_reason": analysis["reason"] if bool(objective_gate["objective_passed"]) else None,
        "invalid_if": analysis.get("invalid_if"),
        "target_reason": analysis.get("target_reason"),
        "has_minimum_history": True,
    }


def _build_scenario_evaluation(analysis: Dict[str, Any]) -> Dict[str, Any]:
    scenario_score = max(float(analysis["scenario_score"]), float(analysis["entry_score"]) - 0.2)
    return {
        "scenario_score": round(float(scenario_score), 2),
        "scenario_grade": "A" if scenario_score >= 7.5 else "B" if scenario_score >= 6.4 else "C" if scenario_score >= 5.0 else "D",
        "pullback_intensity": "not_applicable",
        "pullback_score": 0.0,
        "notes": [analysis["reason"]],
        "has_minimum_history": True,
    }


def build_runtime_snapshot(
    bot,
    df: Optional[pd.DataFrame],
    timeframe: Optional[str] = None,
    context_df: Optional[pd.DataFrame] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
) -> Dict[str, Any]:
    resolved_timeframe = timeframe or getattr(bot, "timeframe", None)
    working_df = _prefer_closed_candles(bot, df)
    if working_df.empty:
        return _build_empty_resume_snapshot(
            reason="Sem candles suficientes para leitura do motor EMA/RSI.",
            timeframe=resolved_timeframe,
        )

    working_df = _ensure_indicator_columns(bot, working_df)
    buy_threshold, sell_threshold = _resolve_resume_thresholds(bot)
    analysis = _analyze_resume_signal(
        working_df,
        buy_threshold=buy_threshold,
        sell_threshold=sell_threshold,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    context_evaluation = _build_resume_context_evaluation(
        bot,
        context_df=context_df,
        buy_threshold=buy_threshold,
        sell_threshold=sell_threshold,
    ) or _build_fallback_context_evaluation(analysis)
    regime_evaluation = _build_resume_regime_evaluation(
        working_df,
        timeframe=resolved_timeframe,
    )
    rr_estimate = _resolve_rr_estimate(
        analysis,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    structure_evaluation = _build_structure_evaluation(
        analysis,
        timeframe=resolved_timeframe,
    )
    confirmation_evaluation = _build_confirmation_evaluation(analysis)
    objective_gate = _evaluate_indicator_objective_gate(
        analysis=analysis,
        context_evaluation=context_evaluation,
    )
    entry_evaluation = _build_entry_evaluation(
        analysis,
        objective_gate=objective_gate,
        rr_estimate=rr_estimate,
    )
    scenario_evaluation = _build_scenario_evaluation(analysis)
    market_state_evaluation = bot.evaluate_market_state(
        context_result=context_evaluation,
        regime_result=regime_evaluation,
        structure_result=structure_evaluation,
        confirmation_result=confirmation_evaluation,
        entry_result=entry_evaluation,
        scenario_score_result=scenario_evaluation,
    )
    trade_decision = bot.make_trade_decision(
        context_result=context_evaluation,
        structure_result=structure_evaluation,
        confirmation_result=confirmation_evaluation,
        entry_result=entry_evaluation,
        hard_block_result={"hard_block": False, "block_reason": None, "block_source": None, "notes": []},
        scenario_score_result=scenario_evaluation,
        risk_result=None,
        regime_result=regime_evaluation,
    )
    return {
        "analysis": analysis,
        "context_evaluation": context_evaluation,
        "regime_evaluation": regime_evaluation,
        "structure_evaluation": structure_evaluation,
        "confirmation_evaluation": confirmation_evaluation,
        "entry_evaluation": entry_evaluation,
        "scenario_evaluation": scenario_evaluation,
        "market_state_evaluation": market_state_evaluation,
        "trade_decision": trade_decision,
    }
