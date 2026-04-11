from __future__ import annotations

from typing import Dict

import config


def create_position(signal: str, entry_price: float, timestamp, atr: float):
    side = "long" if signal == "buy" else "short"
    if side == "long":
        risk_distance = max(entry_price * (config.LONG_STOP_LOSS_PCT / 100), atr)
        partial_distance = entry_price * ((config.LONG_TAKE_PROFIT_PCT * 0.55) / 100)
    else:
        risk_distance = max(entry_price * (config.SHORT_STOP_LOSS_PCT / 100), atr)
        partial_distance = entry_price * ((config.SHORT_TAKE_PROFIT_PCT * 0.55) / 100)

    return {
        "side": side,
        "entry_price": float(entry_price),
        "entry_timestamp": timestamp,
        "best_price": float(entry_price),
        "initial_stop": float(entry_price - risk_distance if side == "long" else entry_price + risk_distance),
        "current_stop": float(entry_price - risk_distance if side == "long" else entry_price + risk_distance),
        "partial_target": float(entry_price + partial_distance if side == "long" else entry_price - partial_distance),
        "partial_taken": False,
        "break_even_active": False,
        "atr": float(atr),
    }


def calculate_trade_pct(side: str, entry_price: float, exit_price: float) -> float:
    if side == "long":
        return (exit_price - entry_price) / entry_price * 100
    return (entry_price - exit_price) / entry_price * 100


def evaluate_open_position(position: Dict, current_price: float, timestamp) -> Dict:
    pos = position.copy()
    side = pos["side"]
    price = float(current_price)

    if side == "long":
        pos["best_price"] = max(float(pos["best_price"]), price)
        if (not pos["partial_taken"]) and price >= float(pos["partial_target"]):
            pos["partial_taken"] = True
            pos["break_even_active"] = True
            pos["current_stop"] = max(float(pos["current_stop"]), float(pos["entry_price"]))
            return {"action": "partial", "position": pos, "reason": "partial_target_hit"}
        if pos["break_even_active"]:
            trailing_stop = float(pos["best_price"]) * (1 - config.LONG_TRAILING_STOP_PCT / 100)
            pos["current_stop"] = max(float(pos["current_stop"]), trailing_stop)
        if price <= float(pos["current_stop"]):
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": price,
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), price),
                    "reason": "stop_or_trailing",
                },
            }
    else:
        pos["best_price"] = min(float(pos["best_price"]), price)
        if (not pos["partial_taken"]) and price <= float(pos["partial_target"]):
            pos["partial_taken"] = True
            pos["break_even_active"] = True
            pos["current_stop"] = min(float(pos["current_stop"]), float(pos["entry_price"]))
            return {"action": "partial", "position": pos, "reason": "partial_target_hit"}
        if pos["break_even_active"]:
            trailing_stop = float(pos["best_price"]) * (1 + config.SHORT_TRAILING_STOP_PCT / 100)
            pos["current_stop"] = min(float(pos["current_stop"]), trailing_stop)
        if price >= float(pos["current_stop"]):
            return {
                "action": "close",
                "position": None,
                "closed_position": {
                    "side": side,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": price,
                    "entry_timestamp": pos["entry_timestamp"],
                    "exit_timestamp": timestamp,
                    "best_price": float(pos["best_price"]),
                    "gross_pct": calculate_trade_pct(side, float(pos["entry_price"]), price),
                    "reason": "stop_or_trailing",
                },
            }

    return {"action": "hold", "position": pos}
