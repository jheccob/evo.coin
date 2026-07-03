from __future__ import annotations

from typing import Dict

import config


def build_native_bracket_position(
    *,
    side: str,
    entry_price: float,
    timestamp,
    stop_price: float,
    take_price: float,
    atr: float = 0.0,
    best_price: float | None = None,
) -> Dict:
    resolved_entry = float(entry_price)
    resolved_best_price = float(best_price) if best_price is not None else resolved_entry
    return {
        "side": str(side),
        "entry_price": resolved_entry,
        "entry_timestamp": timestamp,
        "best_price": resolved_best_price,
        "initial_stop": float(stop_price),
        "current_stop": float(stop_price),
        "partial_target": float(take_price),
        "partial_taken": False,
        "break_even_active": False,
        "atr": float(atr),
        "execution_profile": "native_bracket",
    }


def create_native_bracket_position(signal: str, entry_price: float, timestamp, atr: float = 0.0):
    side = "long" if signal == "buy" else "short"
    stop_pct = float(config.LONG_STOP_LOSS_PCT if side == "long" else config.SHORT_STOP_LOSS_PCT)
    take_pct = float(config.LONG_TAKE_PROFIT_PCT if side == "long" else config.SHORT_TAKE_PROFIT_PCT)
    stop_price = entry_price * (1 - stop_pct / 100) if side == "long" else entry_price * (1 + stop_pct / 100)
    take_price = entry_price * (1 + take_pct / 100) if side == "long" else entry_price * (1 - take_pct / 100)
    return build_native_bracket_position(
        side=side,
        entry_price=entry_price,
        timestamp=timestamp,
        stop_price=stop_price,
        take_price=take_price,
        atr=atr,
    )


def build_managed_position(
    *,
    side: str,
    entry_price: float,
    timestamp,
    stop_price: float,
    partial_target_price: float,
    trailing_trigger_price: float,
    atr: float = 0.0,
    best_price: float | None = None,
    partial_taken: bool = False,
    break_even_active: bool = False,
    realized_partial_pct: float = 0.0,
    trailing_trigger_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    stop_loss_pct: float | None = None,
    partial_target_pct: float | None = None,
    management_profile: str | None = None,
) -> Dict:
    resolved_entry = float(entry_price)
    resolved_best_price = float(best_price) if best_price is not None else resolved_entry
    return {
        "side": str(side),
        "entry_price": resolved_entry,
        "entry_timestamp": timestamp,
        "best_price": resolved_best_price,
        "initial_stop": float(stop_price),
        "current_stop": float(stop_price),
        "partial_target": float(partial_target_price),
        "trailing_trigger_price": float(trailing_trigger_price),
        "partial_taken": bool(partial_taken),
        "break_even_active": bool(break_even_active),
        "realized_partial_pct": float(realized_partial_pct),
        "atr": float(atr),
        "execution_profile": "managed",
        "trailing_trigger_pct": float(trailing_trigger_pct) if trailing_trigger_pct is not None else None,
        "trailing_stop_pct": float(trailing_stop_pct) if trailing_stop_pct is not None else None,
        "stop_loss_pct": float(stop_loss_pct) if stop_loss_pct is not None else None,
        "partial_target_pct": float(partial_target_pct) if partial_target_pct is not None else None,
        "management_profile": str(management_profile or "default"),
    }


def _resolve_managed_position_profile(
    *,
    side: str,
    entry_setup: str | None = None,
    entry_source_setup: str | None = None,
    stop_loss_pct: float | None = None,
    partial_target_pct: float | None = None,
    trailing_trigger_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    management_profile: str | None = None,
) -> Dict[str, float | str | bool]:
    resolved_setup = str(entry_setup or entry_source_setup or "").strip().lower()
    default_stop_loss_pct = float(config.LONG_STOP_LOSS_PCT if side == "long" else config.SHORT_STOP_LOSS_PCT)
    default_partial_target_pct = float(
        config.PARTIAL_TARGET_PCT if side == "long" else (config.SHORT_TAKE_PROFIT_PCT * 0.55)
    )
    default_trailing_trigger_pct = float(config.TRAILING_TRIGGER_PCT)
    default_trailing_stop_pct = float(
        config.LONG_TRAILING_STOP_PCT if side == "long" else config.SHORT_TRAILING_STOP_PCT
    )
    default_use_fixed_stop = False
    resolved_management_profile = str(management_profile or "default")

    if side == "long" and resolved_setup == "trend_resume_long":
        default_stop_loss_pct = float(
            getattr(config, "TREND_RESUME_LONG_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "TREND_RESUME_LONG_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "TREND_RESUME_LONG_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "TREND_RESUME_LONG_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "TREND_RESUME_LONG_USE_FIXED_STOP", True))
        resolved_management_profile = str(management_profile or "trend_resume_long")
    elif side == "long" and resolved_setup == "pullback_long":
        default_stop_loss_pct = float(
            getattr(config, "PULLBACK_LONG_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "PULLBACK_LONG_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "PULLBACK_LONG_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "PULLBACK_LONG_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "PULLBACK_LONG_USE_FIXED_STOP", False))
        resolved_management_profile = str(management_profile or "pullback_long")
    elif side == "long" and resolved_setup == "market_reading_long":
        default_stop_loss_pct = float(
            getattr(config, "MARKET_READING_LONG_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "MARKET_READING_LONG_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "MARKET_READING_LONG_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "MARKET_READING_LONG_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "MARKET_READING_LONG_USE_FIXED_STOP", False))
        resolved_management_profile = str(management_profile or "market_reading_long")
    elif side == "short" and resolved_setup == "trend_resume_short":
        default_stop_loss_pct = float(
            getattr(config, "TREND_RESUME_SHORT_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "TREND_RESUME_SHORT_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "TREND_RESUME_SHORT_USE_FIXED_STOP", False))
        resolved_management_profile = str(management_profile or "trend_resume_short")
    elif side == "short" and resolved_setup == "pullback_short":
        default_stop_loss_pct = float(
            getattr(config, "PULLBACK_SHORT_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "PULLBACK_SHORT_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "PULLBACK_SHORT_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "PULLBACK_SHORT_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "PULLBACK_SHORT_USE_FIXED_STOP", False))
        resolved_management_profile = str(management_profile or "pullback_short")
    elif side == "short" and resolved_setup == "relief_rally_short":
        default_stop_loss_pct = float(
            getattr(config, "RELIEF_RALLY_SHORT_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "RELIEF_RALLY_SHORT_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "RELIEF_RALLY_SHORT_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "RELIEF_RALLY_SHORT_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "RELIEF_RALLY_SHORT_USE_FIXED_STOP", False))
        resolved_management_profile = str(management_profile or "relief_rally_short")
    elif side == "short" and resolved_setup == "market_reading_short":
        default_stop_loss_pct = float(
            getattr(config, "MARKET_READING_SHORT_STOP_LOSS_PCT", default_stop_loss_pct) or default_stop_loss_pct
        )
        default_partial_target_pct = float(
            getattr(config, "MARKET_READING_SHORT_PARTIAL_TARGET_PCT", default_partial_target_pct)
            or default_partial_target_pct
        )
        default_trailing_trigger_pct = float(
            getattr(config, "MARKET_READING_SHORT_TRAILING_TRIGGER_PCT", default_trailing_trigger_pct)
            or default_trailing_trigger_pct
        )
        default_trailing_stop_pct = float(
            getattr(config, "MARKET_READING_SHORT_TRAILING_STOP_PCT", default_trailing_stop_pct)
            or default_trailing_stop_pct
        )
        default_use_fixed_stop = bool(getattr(config, "MARKET_READING_SHORT_USE_FIXED_STOP", False))
        resolved_management_profile = str(management_profile or "market_reading_short")

    return {
        "stop_loss_pct": float(stop_loss_pct if stop_loss_pct is not None else default_stop_loss_pct),
        "partial_target_pct": float(
            partial_target_pct if partial_target_pct is not None else default_partial_target_pct
        ),
        "trailing_trigger_pct": float(
            trailing_trigger_pct if trailing_trigger_pct is not None else default_trailing_trigger_pct
        ),
        "trailing_stop_pct": float(trailing_stop_pct if trailing_stop_pct is not None else default_trailing_stop_pct),
        "use_fixed_stop": bool(default_use_fixed_stop),
        "management_profile": resolved_management_profile,
    }


def _resolve_risk_distance(
    *,
    entry_price: float,
    atr: float,
    stop_loss_pct: float,
    use_fixed_stop: bool,
) -> float:
    pct_distance = entry_price * (float(stop_loss_pct) / 100)
    if use_fixed_stop:
        return pct_distance
    return min(max(pct_distance, float(atr) * 1.5), entry_price * 0.02)


def _resolve_reward_distances(
    *,
    entry_price: float,
    risk_distance: float,
    partial_target_pct: float,
    trailing_trigger_pct: float,
) -> Dict[str, float]:
    resolved_entry = float(entry_price)
    configured_partial_distance = resolved_entry * (float(partial_target_pct) / 100.0)
    configured_trailing_distance = resolved_entry * (float(trailing_trigger_pct) / 100.0)
    partial_distance = configured_partial_distance
    trailing_distance = configured_trailing_distance

    if bool(getattr(config, "ENFORCE_MIN_RISK_REWARD_RATIO", True)):
        min_risk_reward_ratio = max(float(getattr(config, "MIN_RISK_REWARD_RATIO", 2.0) or 0.0), 0.0)
        if resolved_entry > 0 and risk_distance > 0 and min_risk_reward_ratio > 0:
            min_reward_distance = float(risk_distance) * min_risk_reward_ratio
            partial_distance = max(partial_distance, min_reward_distance)
            trailing_distance = max(trailing_distance, min_reward_distance)

    return {
        "partial_distance": float(partial_distance),
        "trailing_distance": float(trailing_distance),
        "partial_target_pct": (float(partial_distance) / resolved_entry) * 100.0 if resolved_entry > 0 else 0.0,
        "trailing_trigger_pct": (float(trailing_distance) / resolved_entry) * 100.0 if resolved_entry > 0 else 0.0,
    }


def create_position(
    signal: str,
    entry_price: float,
    timestamp,
    atr: float,
    *,
    stop_loss_pct: float | None = None,
    partial_target_pct: float | None = None,
    trailing_trigger_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    entry_setup: str | None = None,
    entry_source_setup: str | None = None,
    management_profile: str | None = None,
):
    side = "long" if signal == "buy" else "short"
    profile = _resolve_managed_position_profile(
        side=side,
        entry_setup=entry_setup,
        entry_source_setup=entry_source_setup,
        stop_loss_pct=stop_loss_pct,
        partial_target_pct=partial_target_pct,
        trailing_trigger_pct=trailing_trigger_pct,
        trailing_stop_pct=trailing_stop_pct,
        management_profile=management_profile,
    )
    risk_distance = _resolve_risk_distance(
        entry_price=entry_price,
        atr=atr,
        stop_loss_pct=float(profile["stop_loss_pct"]),
        use_fixed_stop=bool(profile["use_fixed_stop"]),
    )
    reward_distances = _resolve_reward_distances(
        entry_price=entry_price,
        risk_distance=risk_distance,
        partial_target_pct=float(profile["partial_target_pct"]),
        trailing_trigger_pct=float(profile["trailing_trigger_pct"]),
    )
    partial_distance = float(reward_distances["partial_distance"])
    trailing_trigger = float(reward_distances["trailing_distance"])
    resolved_partial_target_pct = float(reward_distances["partial_target_pct"])
    resolved_trailing_trigger_pct = float(reward_distances["trailing_trigger_pct"])

    return build_managed_position(
        side=side,
        entry_price=entry_price,
        timestamp=timestamp,
        stop_price=(entry_price - risk_distance if side == "long" else entry_price + risk_distance),
        partial_target_price=(entry_price + partial_distance if side == "long" else entry_price - partial_distance),
        trailing_trigger_price=(entry_price + trailing_trigger if side == "long" else entry_price - trailing_trigger),
        atr=atr,
        trailing_trigger_pct=resolved_trailing_trigger_pct,
        trailing_stop_pct=float(profile["trailing_stop_pct"]),
        stop_loss_pct=float(profile["stop_loss_pct"]),
        partial_target_pct=resolved_partial_target_pct,
        management_profile=str(profile["management_profile"]),
    )

    if side == "long":
        # Ajuste: O Stop deve ser baseado no custo fixo ou ATR, mas com teto para não desequilibrar o R/R
        risk_distance = min(max(entry_price * (config.LONG_STOP_LOSS_PCT / 100), atr * 1.5), entry_price * 0.02)
        # CORREÇÃO: Usar a variável do config em vez de 1.0% fixo
        partial_distance = entry_price * (config.PARTIAL_TARGET_PCT / 100)
        resolved_trailing_stop_pct = float(
            trailing_stop_pct if trailing_stop_pct is not None else config.LONG_TRAILING_STOP_PCT
        )
    else:
        risk_distance = min(max(entry_price * (config.SHORT_STOP_LOSS_PCT / 100), atr * 1.5), entry_price * 0.02)
        partial_distance = entry_price * ((config.SHORT_TAKE_PROFIT_PCT * 0.55) / 100)
        resolved_trailing_stop_pct = float(
            trailing_stop_pct if trailing_stop_pct is not None else config.SHORT_TRAILING_STOP_PCT
        )

    resolved_trailing_trigger_pct = float(
        trailing_trigger_pct if trailing_trigger_pct is not None else config.TRAILING_TRIGGER_PCT
    )
    trailing_trigger = entry_price * (resolved_trailing_trigger_pct / 100)

    return build_managed_position(
        side=side,
        entry_price=entry_price,
        timestamp=timestamp,
        stop_price=(entry_price - risk_distance if side == "long" else entry_price + risk_distance),
        partial_target_price=(entry_price + partial_distance if side == "long" else entry_price - partial_distance),
        trailing_trigger_price=(entry_price + trailing_trigger if side == "long" else entry_price - trailing_trigger),
        atr=atr,
        trailing_trigger_pct=resolved_trailing_trigger_pct,
        trailing_stop_pct=resolved_trailing_stop_pct,
        management_profile=management_profile,
    )


def _build_intrabar_path_from_candle(candle) -> list[float]:
    open_price = float(candle["open"])
    high_price = float(candle["high"])
    low_price = float(candle["low"])
    close_price = float(candle["close"])
    if close_price >= open_price:
        return [open_price, low_price, high_price, close_price]
    return [open_price, high_price, low_price, close_price]


def _update_native_bracket_metrics(position: Dict, low_price: float, high_price: float) -> Dict:
    pos = position.copy()
    entry_price = float(pos["entry_price"])
    if pos["side"] == "long":
        pos["best_price"] = max(float(pos["best_price"]), high_price)
        pos["mfe_pct"] = max(pos.get("mfe_pct", 0.0), (float(pos["best_price"]) - entry_price) / entry_price * 100)
        pos["mae_pct"] = max(pos.get("mae_pct", 0.0), (entry_price - low_price) / entry_price * 100)
    else:
        pos["best_price"] = min(float(pos["best_price"]), low_price)
        pos["mfe_pct"] = max(pos.get("mfe_pct", 0.0), (entry_price - float(pos["best_price"])) / entry_price * 100)
        pos["mae_pct"] = max(pos.get("mae_pct", 0.0), (high_price - entry_price) / entry_price * 100)
    return pos


def _resolve_native_bracket_cross(
    *,
    side: str,
    previous_price: float,
    next_price: float,
    stop_price: float,
    take_price: float,
):
    ascending = next_price > previous_price
    descending = next_price < previous_price
    candidates: list[tuple[float, str]] = []
    levels = [(stop_price, "stop_loss"), (take_price, "take_profit")]

    for level_price, reason in levels:
        if ascending and previous_price <= level_price <= next_price:
            candidates.append((level_price, reason))
        elif descending and next_price <= level_price <= previous_price:
            candidates.append((level_price, reason))

    if not candidates:
        return None

    if ascending:
        return min(candidates, key=lambda item: item[0])
    return max(candidates, key=lambda item: item[0])


def evaluate_native_bracket_position_on_candle(position: Dict, candle) -> Dict:
    pos = position.copy()
    stop_price = float(pos["current_stop"])
    take_price = float(pos["partial_target"])
    path = _build_intrabar_path_from_candle(candle)
    candle_timestamp = candle["timestamp"]
    first_price = float(path[0])
    side = pos["side"]

    pos = _update_native_bracket_metrics(pos, low_price=first_price, high_price=first_price)
    if side == "long":
        if first_price <= stop_price or first_price >= take_price:
            reason = "stop_loss" if first_price <= stop_price else "take_profit"
            exit_price = first_price
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": exit_price,
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": candle_timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), exit_price),
                    "mfe_pct": pos.get("mfe_pct", 0.0),
                    "mae_pct": pos.get("mae_pct", 0.0),
                    "reason": reason,
                },
            }
    else:
        if first_price >= stop_price or first_price <= take_price:
            reason = "stop_loss" if first_price >= stop_price else "take_profit"
            exit_price = first_price
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": exit_price,
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": candle_timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), exit_price),
                    "mfe_pct": pos.get("mfe_pct", 0.0),
                    "mae_pct": pos.get("mae_pct", 0.0),
                    "reason": reason,
                },
            }

    previous_price = first_price
    for next_price in path[1:]:
        next_price = float(next_price)
        crossing = _resolve_native_bracket_cross(
            side=side,
            previous_price=previous_price,
            next_price=next_price,
            stop_price=stop_price,
            take_price=take_price,
        )
        reached_low = min(previous_price, next_price)
        reached_high = max(previous_price, next_price)
        if crossing is not None:
            exit_price, reason = crossing
            reached_low = min(previous_price, float(exit_price))
            reached_high = max(previous_price, float(exit_price))
            pos = _update_native_bracket_metrics(pos, low_price=reached_low, high_price=reached_high)
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": float(exit_price),
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": candle_timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), float(exit_price)),
                    "mfe_pct": pos.get("mfe_pct", 0.0),
                    "mae_pct": pos.get("mae_pct", 0.0),
                    "reason": reason,
                },
            }
        pos = _update_native_bracket_metrics(pos, low_price=reached_low, high_price=reached_high)
        previous_price = next_price

    return {"action": "hold", "position": pos}


def calculate_trade_pct(side: str, entry_price: float, exit_price: float) -> float:
    if side == "long":
        return (exit_price - entry_price) / entry_price * 100
    return (entry_price - exit_price) / entry_price * 100


def _calculate_partial_gross_pct(position: Dict, fill_price: float) -> float:
    entry_price = float(position["entry_price"])
    return calculate_trade_pct(str(position["side"]), entry_price, float(fill_price))


def _defer_profit_protection_to_candle_close(position: Dict) -> bool:
    profile = str(position.get("management_profile") or "").strip().lower()
    if profile == "trend_resume_short":
        return bool(getattr(config, "TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION", False))
    return False


def _partial_take_profit_enabled() -> bool:
    return bool(getattr(config, "ENABLE_PARTIAL_TAKE_PROFIT", True))


def _apply_long_profit_protection(position: Dict) -> Dict:
    pos = position.copy()
    fee_buffer = float(pos["entry_price"]) * (config.FEE_PCT * 2.5 / 100)
    pos["break_even_active"] = True
    pos["current_stop"] = max(float(pos["current_stop"]), float(pos["entry_price"]) + fee_buffer)
    trailing_stop_pct = float(pos.get("trailing_stop_pct") or config.LONG_TRAILING_STOP_PCT)
    trailing_stop = float(pos["best_price"]) * (1 - trailing_stop_pct / 100)
    pos["current_stop"] = max(float(pos["current_stop"]), trailing_stop)
    return pos


def _apply_short_profit_protection(position: Dict) -> Dict:
    pos = position.copy()
    fee_buffer = float(pos["entry_price"]) * (config.FEE_PCT * 2.5 / 100)
    pos["break_even_active"] = True
    pos["current_stop"] = min(float(pos["current_stop"]), float(pos["entry_price"]) - fee_buffer)
    trailing_stop_pct = float(pos.get("trailing_stop_pct") or config.SHORT_TRAILING_STOP_PCT)
    trailing_stop = float(pos["best_price"]) * (1 + trailing_stop_pct / 100)
    pos["current_stop"] = min(float(pos["current_stop"]), trailing_stop)
    return pos


def evaluate_open_position(
    position: Dict,
    current_price: float,
    timestamp,
    *,
    exit_at_stop_price: bool = False,
) -> Dict:
    pos = position.copy()
    side = pos["side"]
    price = float(current_price)
    defer_profit_protection = _defer_profit_protection_to_candle_close(pos)

    if side == "long":
        pos["best_price"] = max(float(pos["best_price"]), price)
        if (not pos["partial_taken"]) and price >= float(pos["partial_target"]):
            pos = _apply_long_profit_protection(pos)
            if _partial_take_profit_enabled():
                pos["partial_taken"] = True
                return {"action": "partial", "position": pos, "reason": "partial_target_hit"}
        
        # Registra métricas para análise
        pos["mfe_pct"] = max(pos.get("mfe_pct", 0.0), (pos["best_price"] - pos["entry_price"]) / pos["entry_price"] * 100)
        pos["mae_pct"] = max(pos.get("mae_pct", 0.0), (pos["entry_price"] - price) / pos["entry_price"] * 100)

        # Ativa trailing se atingir o gatilho de lucro
        if price >= float(pos["trailing_trigger_price"]):
            pos = _apply_long_profit_protection(pos)
        if price <= float(pos["current_stop"]):
            exit_price = float(pos["current_stop"]) if exit_at_stop_price else price
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": exit_price,
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), exit_price),
                    "mfe_pct": pos.get("mfe_pct", 0.0),
                    "mae_pct": pos.get("mae_pct", 0.0),
                    "reason": "stop_or_trailing",
                },
            }
    else:
        pos["best_price"] = min(float(pos["best_price"]), price)
        if (not pos["partial_taken"]) and price <= float(pos["partial_target"]):
            pos = _apply_short_profit_protection(pos)
            if _partial_take_profit_enabled():
                pos["partial_taken"] = True
                return {"action": "partial", "position": pos, "reason": "partial_target_hit"}

        # Registra métricas para análise
        pos["mfe_pct"] = max(pos.get("mfe_pct", 0.0), (pos["entry_price"] - pos["best_price"]) / pos["entry_price"] * 100)
        pos["mae_pct"] = max(pos.get("mae_pct", 0.0), (price - pos["entry_price"]) / pos["entry_price"] * 100)

        # Ativa trailing se atingir o gatilho de lucro, mesmo sem parcial
        if price <= float(pos["trailing_trigger_price"]):
            if not defer_profit_protection or bool(pos["partial_taken"]) or not _partial_take_profit_enabled():
                pos = _apply_short_profit_protection(pos)
        if price >= float(pos["current_stop"]):
            exit_price = float(pos["current_stop"]) if exit_at_stop_price else price
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": exit_price,
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), exit_price),
                    "mfe_pct": pos.get("mfe_pct", 0.0),
                    "mae_pct": pos.get("mae_pct", 0.0),
                    "reason": "stop_or_trailing",
                },
            }

    return {"action": "hold", "position": pos}


def evaluate_managed_position_on_candle(
    position: Dict,
    candle,
    *,
    realized_partial_pct: float = 0.0,
) -> Dict:
    active_position = position
    realized_partial = float(realized_partial_pct)
    path = _build_intrabar_path_from_candle(candle)
    candle_timestamp = candle["timestamp"]

    for point_index, point_price in enumerate(path):
        stop_fill_on_cross = point_index > 0

        while active_position is not None:
            management = evaluate_open_position(
                active_position,
                current_price=float(point_price),
                timestamp=candle_timestamp,
                exit_at_stop_price=stop_fill_on_cross,
            )

            if management["action"] == "partial":
                realized_partial += _calculate_partial_gross_pct(
                    active_position,
                    float(active_position["partial_target"]),
                ) * 0.5
                active_position = management["position"]
                continue

            if management["action"] == "close":
                return {
                    "action": "close",
                    "position_before_close": active_position,
                    "closed_position": management["closed_position"],
                    "realized_partial_pct": realized_partial,
                }

            active_position = management["position"]
            break

    if active_position is not None and _defer_profit_protection_to_candle_close(active_position):
        close_price = float(candle["close"])
        if close_price <= float(active_position["trailing_trigger_price"]):
            active_position = _apply_short_profit_protection(active_position)

    return {
        "action": "hold",
        "position": active_position,
        "realized_partial_pct": realized_partial,
    }
