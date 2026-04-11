from __future__ import annotations

from typing import Any, Dict, Optional

from trading_core.block_debug import emit_block_debug


def make_trade_decision(
    bot,
    context_result: Optional[Dict[str, Any]],
    structure_result: Optional[Dict[str, Any]],
    confirmation_result: Optional[Dict[str, Any]],
    entry_result: Optional[Dict[str, Any]],
    hard_block_result: Optional[Dict[str, Any]] = None,
    scenario_score_result: Optional[Dict[str, Any]] = None,
    risk_result: Optional[Dict[str, Any]] = None,
    regime_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    del bot, structure_result, confirmation_result
    context_result = context_result or {}
    entry_result = entry_result or {}
    hard_block_result = hard_block_result or {}
    scenario_score_result = scenario_score_result or {}
    risk_result = risk_result or {}
    regime_result = regime_result or {}

    block_reason = None
    action = "wait"
    if hard_block_result.get("hard_block"):
        emit_block_debug(
            "signal_engine.hard_block",
            block_source=hard_block_result.get("block_source"),
            block_reason=hard_block_result.get("block_reason"),
            entry_score=entry_result.get("entry_score"),
            scenario_score=scenario_score_result.get("scenario_score"),
        )
        block_reason = hard_block_result.get("block_reason") or "Hard block ativo."
    elif risk_result and not bool(risk_result.get("allowed", True)):
        emit_block_debug(
            "signal_engine.risk_block",
            risk_reason=risk_result.get("risk_reason") or risk_result.get("reason"),
            risk_allowed=risk_result.get("allowed"),
            risk_score=risk_result.get("risk_score"),
            signal_direction=entry_result.get("signal_direction"),
        )
        block_reason = risk_result.get("risk_reason") or risk_result.get("reason") or "Risco bloqueou a operacao."
    elif not bool(entry_result.get("objective_passed")):
        emit_block_debug(
            "signal_engine.objective_gate_block",
            objective_passed=entry_result.get("objective_passed"),
            rejection_reason=entry_result.get("rejection_reason"),
            failed_flags=entry_result.get("failed_flags"),
            entry_score=entry_result.get("entry_score"),
            signal_direction=entry_result.get("signal_direction"),
            context_bias=entry_result.get("context_bias"),
        )
        block_reason = entry_result.get("rejection_reason") or "Setup nao aprovado."
    else:
        signal_direction = str(entry_result.get("signal_direction") or "").upper()
        if signal_direction == "COMPRA":
            action = "buy"
        elif signal_direction == "VENDA":
            action = "sell"

    confidence = round(
        min(
            max(
                float(entry_result.get("entry_score", 0.0) or 0.0),
                float(scenario_score_result.get("scenario_score", 0.0) or 0.0),
            ),
            10.0,
        ),
        2,
    )

    market_pattern = entry_result.get("market_pattern") or entry_result.get("setup_type")
    return {
        "action": action,
        "confidence": confidence,
        "market_bias": context_result.get("market_bias") or regime_result.get("market_bias") or "neutral",
        "market_state": regime_result.get("regime") or "range",
        "execution_mode": "ready" if action in {"buy", "sell"} else "standby",
        "market_pattern": market_pattern,
        "setup_type": market_pattern,
        "entry_reason": entry_result.get("entry_reason") if action in {"buy", "sell"} else None,
        "block_reason": block_reason,
        "invalid_if": entry_result.get("invalid_if"),
    }


def check_signal(
    bot,
    df,
    **kwargs,
):
    pipeline = bot.evaluate_signal_pipeline(df, **kwargs)
    return pipeline.get("approved_signal") or pipeline.get("analytical_signal") or "NEUTRO"


def get_signal_with_confidence(bot, df):
    pipeline = bot.evaluate_signal_pipeline(df)
    decision = pipeline.get("trade_decision") or {}
    approved_signal = pipeline.get("approved_signal") or pipeline.get("analytical_signal") or "NEUTRO"
    return {
        "signal": approved_signal,
        "confidence": round(float(decision.get("confidence", 0.0) or 0.0) * 10.0, 2),
    }


def generate_advanced_signal(bot, row):
    return generate_trend_signal(bot, row, getattr(bot, "rsi_min", 54), getattr(bot, "rsi_max", 47))


def calculate_signal_confidence(bot, row):
    del bot
    signal_strength = abs(float(row.get("rsi", 50.0) or 50.0) - 50.0) / 50.0
    return round(min(signal_strength * 100.0, 100.0), 2)


def get_effective_min_confidence(bot, min_confidence: float, timeframe: Optional[str]) -> float:
    del bot, timeframe
    return float(min_confidence or 0.0)


def relax_low_confidence_signal(
    signal: str,
    confidence: float,
    effective_min_confidence: float,
    timeframe: Optional[str],
):
    del timeframe
    return signal if confidence >= effective_min_confidence else None


def generate_trend_signal(bot, row, rsi_min: float, rsi_max: float) -> str:
    del bot
    rsi = float(row.get("rsi", 50.0) or 50.0)
    close = float(row.get("close", 0.0) or 0.0)
    ema_fast = float(row.get("ema_fast", close) or close)
    ema_slow = float(row.get("ema_slow", close) or close)
    ema_trend = float(row.get("ema_trend", close) or close)
    if close > ema_fast > ema_slow > ema_trend and rsi >= float(rsi_min):
        return "COMPRA"
    if close < ema_fast < ema_slow < ema_trend and rsi <= float(rsi_max):
        return "VENDA"
    return "NEUTRO"


def calculate_advanced_score(bot, row, signal=None):
    del bot
    resolved_signal = signal or "NEUTRO"
    base = calculate_signal_confidence(None, row) / 10.0
    if resolved_signal in {"COMPRA", "VENDA"}:
        return round(min(base + 1.0, 10.0), 2)
    return round(min(base, 10.0), 2)
