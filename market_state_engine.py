from __future__ import annotations

from typing import Any, Dict, List, Optional


LONG_SETUPS = [
    "trend_resume_long",
    "pullback_long",
    "ema_rsi_resume_long",
]
SHORT_SETUPS = [
    "trend_resume_short",
    "pullback_short",
    "ema_rsi_resume_short",
]


def _normalize(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        token = str(value or "").strip().lower()
        if token and token not in out:
            out.append(token)
    return out


def market_states_to_setup_allowlist(market_states: Optional[List[str]]) -> List[str]:
    normalized_states = _normalize(market_states)
    if not normalized_states:
        return []

    allow_long = False
    allow_short = False
    for state in normalized_states:
        if any(flag in state for flag in ("bull", "long", "buy", "compra")):
            allow_long = True
        if any(flag in state for flag in ("bear", "short", "sell", "venda")):
            allow_short = True
        if state in {"all_states", "all", "both"}:
            allow_long = True
            allow_short = True

    setups: List[str] = []
    if allow_long:
        setups.extend(LONG_SETUPS)
    if allow_short:
        setups.extend(SHORT_SETUPS)
    return list(dict.fromkeys(setups))


def setup_types_to_market_state_allowlist(setup_types: Optional[List[str]]) -> List[str]:
    normalized_setups = _normalize(setup_types)
    if not normalized_setups:
        return []

    allow_long = any("long" in setup or "buy" in setup or "compra" in setup for setup in normalized_setups)
    allow_short = any("short" in setup or "sell" in setup or "venda" in setup for setup in normalized_setups)

    states: List[str] = []
    if allow_long:
        states.append("trend_bullish")
    if allow_short:
        states.append("trend_bearish")
    return states


class MarketStateEngine:
    def evaluate(
        self,
        context_result: Optional[Dict[str, Any]] = None,
        regime_result: Optional[Dict[str, Any]] = None,
        structure_result: Optional[Dict[str, Any]] = None,
        confirmation_result: Optional[Dict[str, Any]] = None,
        entry_result: Optional[Dict[str, Any]] = None,
        scenario_score_result: Optional[Dict[str, Any]] = None,
        hard_block_result: Optional[Dict[str, Any]] = None,
        risk_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        del structure_result, confirmation_result

        context = context_result or {}
        regime = regime_result or {}
        entry = entry_result or {}
        scenario = scenario_score_result or {}
        hard_block = hard_block_result or {}
        risk = risk_result or {}

        market_bias = str(context.get("market_bias") or regime.get("market_bias") or "neutral").strip().lower()
        regime_name = str(regime.get("regime") or "range").strip().lower()
        setup_type = entry.get("setup_type") or entry.get("market_pattern")
        signal_direction = str(entry.get("signal_direction") or "").strip().upper()

        if hard_block.get("hard_block"):
            execution_mode = "blocked"
            reason = str(hard_block.get("block_reason") or "Hard block ativo.")
        elif risk and not bool(risk.get("allowed", True)):
            execution_mode = "risk_blocked"
            reason = str(risk.get("risk_reason") or risk.get("reason") or "Risco bloqueou execução.")
        elif bool(entry.get("objective_passed")) and signal_direction in {"COMPRA", "VENDA"}:
            execution_mode = "ready"
            reason = str(entry.get("entry_reason") or "Setup aprovado.")
        else:
            execution_mode = "standby"
            reason = str(entry.get("rejection_reason") or "Aguardando confirmação.")

        if market_bias == "bullish":
            market_state = "trend_bullish"
        elif market_bias == "bearish":
            market_state = "trend_bearish"
        elif regime_name in {"unknown", "none", "null", ""}:
            market_state = "unknown"
        else:
            market_state = "neutral_chop"

        notes = [note for note in [reason, regime.get("reason")] if note]
        confidence = float(max(entry.get("entry_score", 0.0) or 0.0, scenario.get("scenario_score", 0.0) or 0.0))

        return {
            "market_state": market_state,
            "market_bias": market_bias,
            "execution_mode": execution_mode,
            "reason": reason,
            "notes": list(dict.fromkeys(notes)),
            "market_pattern": setup_type,
            "setup_type": setup_type,
            "confidence": round(min(max(confidence, 0.0), 10.0), 2),
            "regime": regime_name,
            "scenario_score": scenario.get("scenario_score"),
            "hard_block": bool(hard_block.get("hard_block")),
            "risk_allowed": bool(risk.get("allowed", True)) if risk else True,
        }
