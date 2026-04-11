from __future__ import annotations

from typing import Dict, Iterable, Optional

from trading_core.block_debug import emit_block_debug


def clear_hard_block(bot):
    bot._last_hard_block_evaluation = {"hard_block": False, "block_reason": None, "block_source": None, "notes": []}


def set_hard_block(bot, block_reason: str, block_source: str = "signal_engine") -> str:
    emit_block_debug(
        "pipeline_engine.set_hard_block",
        block_source=block_source,
        block_reason=block_reason,
    )
    bot._last_hard_block_evaluation = {
        "hard_block": True,
        "block_reason": block_reason,
        "block_source": block_source,
        "notes": [block_reason],
    }
    return block_reason


def normalize_market_pattern_allowlist(allowed_market_patterns: Optional[Iterable[str]]) -> Optional[set[str]]:
    if allowed_market_patterns is None:
        return None
    normalized = {str(value or "").strip().lower() for value in allowed_market_patterns if str(value or "").strip()}
    return normalized or None


def normalize_signal_direction_filter(allowed_signal_directions: Optional[Iterable[str]]) -> Optional[set[str]]:
    if allowed_signal_directions is None:
        return None

    normalized: set[str] = set()
    for value in allowed_signal_directions:
        token = str(value or "").strip().lower()
        if token in {"compra", "buy", "long", "bull", "bullish"}:
            normalized.add("COMPRA")
        elif token in {"venda", "sell", "short", "bear", "bearish"}:
            normalized.add("VENDA")
    return normalized or None


def apply_runtime_market_pattern_policy(
    bot,
    analytical_signal: str,
    allowed_market_patterns: Optional[Iterable[str]] = None,
) -> str:
    normalized_patterns = normalize_market_pattern_allowlist(allowed_market_patterns)
    if not normalized_patterns:
        return analytical_signal

    latest_entry = getattr(bot, "_last_entry_quality_evaluation", None) or {}
    market_pattern = str(latest_entry.get("market_pattern") or latest_entry.get("setup_type") or "").strip().lower()
    if market_pattern and market_pattern in normalized_patterns:
        return analytical_signal
    return "NEUTRO"


def apply_runtime_signal_direction_policy(
    bot,
    analytical_signal: str,
    allowed_signal_directions: Optional[Iterable[str]] = None,
) -> str:
    del bot
    normalized_directions = normalize_signal_direction_filter(allowed_signal_directions)
    if not normalized_directions or analytical_signal == "NEUTRO":
        return analytical_signal
    return analytical_signal if analytical_signal in normalized_directions else "NEUTRO"


def apply_ai_guardrail(
    bot,
    df,
    analytical_signal: str,
    **kwargs,
) -> str:
    del bot, df, kwargs
    return analytical_signal


def finalize_signal_pipeline(bot, analytical_signal: str) -> Dict[str, object]:
    hard_block = getattr(bot, "_last_hard_block_evaluation", None) or {
        "hard_block": False,
        "block_reason": None,
        "block_source": None,
        "notes": [],
    }
    return {
        "approved_signal": analytical_signal,
        "blocked_signal": None,
        "block_reason": hard_block.get("block_reason"),
        "block_source": hard_block.get("block_source"),
        "hard_block_evaluation": hard_block,
    }


def evaluate_signal_pipeline(
    bot,
    df,
    min_confidence=60,
    require_volume=True,
    require_trend=False,
    avoid_ranging=False,
    crypto_optimized=True,
    timeframe="5m",
    day_trading_mode=False,
    context_df=None,
    context_timeframe: Optional[str] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    allowed_execution_setups: Optional[Iterable[str]] = None,
    allowed_signal_directions: Optional[Iterable[str]] = None,
    ai_assist_mode: Optional[str] = None,
    ai_min_win_probability: Optional[float] = None,
    include_ai_explanations: bool = True,
) -> Dict[str, object]:
    del require_volume, require_trend, avoid_ranging, crypto_optimized, day_trading_mode
    del ai_assist_mode, ai_min_win_probability, include_ai_explanations

    if context_df is None and context_timeframe and context_timeframe != timeframe:
        try:
            context_df = bot._fetch_context_df(context_timeframe)
        except Exception:
            context_df = None

    snapshot = bot._build_resume_snapshot(
        df=df,
        timeframe=timeframe,
        context_df=context_df,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    analysis = snapshot.get("analysis") or {}
    context_evaluation = snapshot.get("context_evaluation") or {}
    regime_evaluation = snapshot.get("regime_evaluation") or {}
    structure_evaluation = snapshot.get("structure_evaluation") or {}
    confirmation_evaluation = snapshot.get("confirmation_evaluation") or {}
    entry_quality_evaluation = snapshot.get("entry_evaluation") or {}
    scenario_evaluation = snapshot.get("scenario_evaluation") or {}
    market_state_evaluation = snapshot.get("market_state_evaluation") or {}
    trade_decision = snapshot.get("trade_decision") or {}

    bot._last_context_evaluation = context_evaluation
    bot._last_regime_evaluation = regime_evaluation
    bot._last_price_structure_evaluation = structure_evaluation
    bot._last_confirmation_evaluation = confirmation_evaluation
    bot._last_entry_quality_evaluation = entry_quality_evaluation
    bot._last_scenario_evaluation = scenario_evaluation
    bot._last_market_state_evaluation = market_state_evaluation
    bot._last_trade_decision = trade_decision

    candidate_signal = analysis.get("signal") or "NEUTRO"
    analytical_signal = candidate_signal
    block_reason = None
    block_source = None

    resolved_regime = str(
        regime_evaluation.get("regime")
        or analysis.get("market_regime")
        or ""
    ).strip().lower()
    if candidate_signal != "NEUTRO" and resolved_regime in {"", "unknown", "none", "null"}:
        emit_block_debug(
            "pipeline_engine.regime_unknown_filter",
            candidate_signal=candidate_signal,
            resolved_regime=resolved_regime,
            timeframe=timeframe,
            context_timeframe=context_timeframe,
        )
        analytical_signal = "NEUTRO"
        block_reason = "Regime unknown bloqueado temporariamente."
        block_source = "regime_unknown_filter"

    trade_confidence = float(trade_decision.get("confidence", 0.0) or 0.0) * 10.0
    if analytical_signal != "NEUTRO" and trade_confidence < float(min_confidence or 0.0):
        emit_block_debug(
            "pipeline_engine.confidence_filter",
            candidate_signal=candidate_signal,
            trade_confidence=round(trade_confidence, 2),
            min_confidence=float(min_confidence or 0.0),
            entry_score=entry_quality_evaluation.get("entry_score"),
            scenario_score=scenario_evaluation.get("scenario_score"),
        )
        analytical_signal = "NEUTRO"
        block_reason = f"Confianca abaixo do minimo ({trade_confidence:.1f} < {float(min_confidence):.1f})."
        block_source = "confidence_filter"

    if analytical_signal != "NEUTRO":
        filtered_signal = apply_runtime_market_pattern_policy(
            bot,
            analytical_signal=analytical_signal,
            allowed_market_patterns=allowed_execution_setups,
        )
        if filtered_signal == "NEUTRO":
            emit_block_debug(
                "pipeline_engine.setup_allowlist_block",
                candidate_signal=candidate_signal,
                filtered_signal=filtered_signal,
                market_pattern=entry_quality_evaluation.get("market_pattern") or entry_quality_evaluation.get("setup_type"),
                allowed_execution_setups=list(allowed_execution_setups or []),
            )
            analytical_signal = "NEUTRO"
            block_reason = "Setup fora da allowlist operacional."
            block_source = "setup_allowlist"

    if analytical_signal != "NEUTRO":
        filtered_signal = apply_runtime_signal_direction_policy(
            bot,
            analytical_signal=analytical_signal,
            allowed_signal_directions=allowed_signal_directions,
        )
        if filtered_signal == "NEUTRO":
            emit_block_debug(
                "pipeline_engine.direction_allowlist_block",
                candidate_signal=candidate_signal,
                filtered_signal=filtered_signal,
                allowed_signal_directions=list(allowed_signal_directions or []),
                analytical_signal=analytical_signal,
            )
            analytical_signal = "NEUTRO"
            block_reason = "Direcao fora da allowlist operacional."
            block_source = "direction_allowlist"

    if analytical_signal != "NEUTRO":
        prior_signal = analytical_signal
        analytical_signal = apply_ai_guardrail(
            bot,
            df=df,
            analytical_signal=analytical_signal,
            timeframe=timeframe,
            context_timeframe=context_timeframe,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        if analytical_signal == "NEUTRO":
            emit_block_debug(
                "pipeline_engine.ai_guardrail_block",
                candidate_signal=candidate_signal,
                prior_signal=prior_signal,
                timeframe=timeframe,
                context_timeframe=context_timeframe,
            )

    if analytical_signal == "NEUTRO" and candidate_signal != "NEUTRO" and block_reason is None:
        emit_block_debug(
            "pipeline_engine.trade_decision_block",
            candidate_signal=candidate_signal,
            trade_decision_block_reason=trade_decision.get("block_reason"),
            rejection_reason=entry_quality_evaluation.get("rejection_reason"),
            objective_passed=entry_quality_evaluation.get("objective_passed"),
            failed_flags=entry_quality_evaluation.get("failed_flags"),
        )
        block_reason = trade_decision.get("block_reason") or entry_quality_evaluation.get("rejection_reason")
        block_source = "trade_decision"

    hard_block_evaluation = getattr(bot, "_last_hard_block_evaluation", None) or {
        "hard_block": False,
        "block_reason": None,
        "block_source": None,
        "notes": [],
    }
    approved_signal = analytical_signal
    blocked_signal = candidate_signal if candidate_signal != approved_signal and candidate_signal != "NEUTRO" else None

    return {
        "candidate_signal": candidate_signal,
        "analytical_signal": analytical_signal,
        "approved_signal": approved_signal,
        "blocked_signal": blocked_signal,
        "block_reason": block_reason,
        "block_source": block_source,
        "analysis": analysis,
        "context_evaluation": context_evaluation,
        "regime_evaluation": regime_evaluation,
        "structure_evaluation": structure_evaluation,
        "confirmation_evaluation": confirmation_evaluation,
        "entry_quality_evaluation": entry_quality_evaluation,
        "scenario_evaluation": scenario_evaluation,
        "market_state_evaluation": market_state_evaluation,
        "trade_decision": trade_decision,
        "hard_block_evaluation": hard_block_evaluation,
    }
