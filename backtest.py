from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pandas as pd

import config
from database.database import db
from ia.dataset_builder import prepare_feature_frame
from market_data import fetch_historical_candles, fetch_historical_candles_from_csv
from position_manager import (
    create_native_bracket_position,
    create_position,
    evaluate_managed_position_on_candle,
    evaluate_native_bracket_position_on_candle,
)
from services.risk_management_service import RiskManagementService
from services.unified_decision_engine import UnifiedDecisionEngine
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal, get_min_required_rows

def _build_phase_summaries(trades):
    if not trades:
        return []

    total_trades = len(trades)
    cuts = [0, total_trades // 4, total_trades // 2, (3 * total_trades) // 4, total_trades]
    phases = []
    for phase_index in range(4):
        start = cuts[phase_index]
        end = cuts[phase_index + 1]
        chunk = trades[start:end]
        if not chunk:
            continue

        wins = sum(1 for trade in chunk if float(trade.get("net_pct", 0.0) or 0.0) > 0)
        net_pct = sum(float(trade.get("net_pct", 0.0) or 0.0) for trade in chunk)
        gross_wins = sum(float(trade.get("net_pct", 0.0) or 0.0) for trade in chunk if float(trade.get("net_pct", 0.0) or 0.0) > 0)
        gross_losses = -sum(float(trade.get("net_pct", 0.0) or 0.0) for trade in chunk if float(trade.get("net_pct", 0.0) or 0.0) < 0)
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

        phases.append(
            {
                "phase": phase_index + 1,
                "trades": len(chunk),
                "win_rate_pct": round(wins / len(chunk) * 100, 2),
                "net_pct": round(net_pct, 4),
                "profit_factor": round(profit_factor, 2),
                "from": chunk[0].get("entry_timestamp"),
                "to": chunk[-1].get("exit_timestamp"),
            }
        )
    return phases


def _build_streak_diagnostics(trades):
    max_loss_streak = 0
    max_win_streak = 0
    current_loss_streak = 0
    current_win_streak = 0

    for trade in trades:
        if float(trade.get("net_pct", 0.0) or 0.0) > 0:
            current_win_streak += 1
            current_loss_streak = 0
        else:
            current_loss_streak += 1
            current_win_streak = 0

        max_loss_streak = max(max_loss_streak, current_loss_streak)
        max_win_streak = max(max_win_streak, current_win_streak)

    return {
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
    }


def _build_giveback_diagnostics(trades):
    losers = [trade for trade in trades if float(trade.get("net_pct", 0.0) or 0.0) <= 0]
    if not losers:
        return {
            "losers": 0,
            "avg_loser_mfe_pct": 0.0,
            "losses_after_0_5pct_profit": 0,
            "losses_after_1_0pct_profit": 0,
            "losses_after_1_5pct_profit": 0,
            "immediate_failures_mfe_le_0_25": 0,
        }

    def _mfe(trade):
        return float(trade.get("mfe_pct", 0.0) or 0.0)

    return {
        "losers": len(losers),
        "avg_loser_mfe_pct": round(sum(_mfe(trade) for trade in losers) / len(losers), 4),
        "losses_after_0_5pct_profit": sum(1 for trade in losers if _mfe(trade) >= 0.5),
        "losses_after_1_0pct_profit": sum(1 for trade in losers if _mfe(trade) >= 1.0),
        "losses_after_1_5pct_profit": sum(1 for trade in losers if _mfe(trade) >= 1.5),
        "immediate_failures_mfe_le_0_25": sum(1 for trade in losers if _mfe(trade) <= 0.25),
    }


def _build_monthly_diagnostics(trades):
    monthly_net = defaultdict(float)
    for trade in trades:
        month_key = str(trade.get("exit_timestamp") or "")[:7]
        monthly_net[month_key] += float(trade.get("net_pct", 0.0) or 0.0)

    ordered = sorted(monthly_net.items())
    worst_months = sorted(ordered, key=lambda item: item[1])[:4]
    best_months = sorted(ordered, key=lambda item: item[1], reverse=True)[:4]
    return {
        "worst_months": [{"month": month, "net_pct": round(net_pct, 4)} for month, net_pct in worst_months],
        "best_months": [{"month": month, "net_pct": round(net_pct, 4)} for month, net_pct in best_months],
    }


def _build_equity_diagnostics(trades):
    equity = 100.0
    peak_equity = 100.0
    max_equity = 100.0
    max_equity_trade = None
    worst_drawdown_pct = 0.0
    worst_drawdown_trade = None

    for trade_index, trade in enumerate(trades, start=1):
        equity *= (1 + float(trade.get("net_pct", 0.0) or 0.0) / 100)
        if equity > peak_equity:
            peak_equity = equity
        if equity > max_equity:
            max_equity = equity
            max_equity_trade = trade_index

        drawdown_pct = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0.0
        if drawdown_pct > worst_drawdown_pct:
            worst_drawdown_pct = drawdown_pct
            worst_drawdown_trade = trade_index

    final_giveback_from_peak_pct = (max_equity - equity) / max_equity * 100 if max_equity > 0 else 0.0
    return {
        "max_equity_pct_base100": round(max_equity, 4),
        "max_equity_trade": max_equity_trade,
        "worst_drawdown_pct": round(worst_drawdown_pct, 4),
        "worst_drawdown_trade": worst_drawdown_trade,
        "final_giveback_from_peak_pct": round(final_giveback_from_peak_pct, 4),
    }


def _build_reason_breakdown(trades):
    reason_counter = Counter(str(trade.get("reason") or "unknown") for trade in trades)
    by_side = {}
    for side in ("long", "short"):
        side_counter = Counter(
            str(trade.get("reason") or "unknown")
            for trade in trades
            if str(trade.get("side") or "").strip().lower() == side
        )
        by_side[side] = dict(side_counter)
    return {
        "all": dict(reason_counter),
        "by_side": by_side,
    }

def _resolve_trade_stop_pct(trade: dict) -> float:
    side = str(trade.get("side") or "").strip().lower()
    if side == "long":
        return float(getattr(config, "LONG_STOP_LOSS_PCT", 0.0) or 0.0)
    if side == "short":
        return float(getattr(config, "SHORT_STOP_LOSS_PCT", 0.0) or 0.0)
    return 0.0


def build_account_risk_summary(
    trades,
    *,
    initial_balance: float,
    risk_per_trade_pct: float,
    leverage: float | None = None,
    position_sizing_mode: str | None = None,
    position_margin_allocation_pct: float | None = None,
    order_balance_usage_pct: float | None = None,
):
    resolved_initial_balance = float(initial_balance or 0.0)
    resolved_risk_pct = max(float(risk_per_trade_pct or 0.0), 0.0)
    resolved_leverage = max(float(leverage or getattr(config, "LEVERAGE", 1) or 1), 1.0)
    resolved_position_sizing_mode = str(
        position_sizing_mode or getattr(config, "POSITION_SIZING_MODE", "risk") or "risk"
    ).strip().lower()
    resolved_position_margin_allocation_pct = max(
        float(
            (
                order_balance_usage_pct
                if order_balance_usage_pct is not None
                else getattr(config, "ORDER_BALANCE_USAGE_PCT", 100.0)
            )
            if resolved_position_sizing_mode == "order_value"
            else (
                position_margin_allocation_pct
                if position_margin_allocation_pct is not None
                else getattr(config, "POSITION_MARGIN_ALLOCATION_PCT", 50.0)
            )
        ),
        0.0,
    )
    sizing_service = RiskManagementService(database=db)
    if resolved_position_sizing_mode == "allocation":
        model_name = "fixed_margin_allocation"
    elif resolved_position_sizing_mode == "hybrid":
        model_name = "risk_capped_by_margin_allocation"
    elif resolved_position_sizing_mode == "order_value":
        model_name = "order_value_notional"
    else:
        model_name = "fixed_risk_per_trade"

    if resolved_initial_balance <= 0:
        return {
            "model": model_name,
            "initial_balance_usdt": round(resolved_initial_balance, 4),
            "final_balance_usdt": round(resolved_initial_balance, 4),
            "net_profit_usdt": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "risk_per_trade_pct": round(resolved_risk_pct, 4),
            "position_sizing_mode": resolved_position_sizing_mode,
            "position_margin_allocation_pct": round(resolved_position_margin_allocation_pct, 4),
            "order_balance_usage_pct": (
                round(resolved_position_margin_allocation_pct, 4)
                if resolved_position_sizing_mode == "order_value"
                else 0.0
            ),
            "leverage": round(resolved_leverage, 4),
            "avg_effective_risk_pct": 0.0,
            "max_effective_risk_pct": 0.0,
            "avg_trade_pnl_usdt": 0.0,
            "long_pnl_usdt": 0.0,
            "short_pnl_usdt": 0.0,
            "best_balance_usdt": round(resolved_initial_balance, 4),
            "worst_balance_usdt": round(resolved_initial_balance, 4),
            "best_trade_pnl_usdt": 0.0,
            "best_trade_index": None,
            "worst_trade_pnl_usdt": 0.0,
            "worst_trade_index": None,
        }

    equity = resolved_initial_balance
    peak_equity = resolved_initial_balance
    trough_equity = resolved_initial_balance
    max_drawdown_pct = 0.0
    long_pnl_usdt = 0.0
    short_pnl_usdt = 0.0
    best_trade_pnl_usdt = float("-inf")
    best_trade_index = None
    worst_trade_pnl_usdt = float("inf")
    worst_trade_index = None
    effective_risk_values = []

    for trade_index, trade in enumerate(trades, start=1):
        stop_pct = _resolve_trade_stop_pct(trade)
        trade_net_pct = float(trade.get("net_pct", 0.0) or 0.0)
        sizing = sizing_service.calculate_position_size(
            account_balance=equity,
            entry_price=1.0,
            stop_loss_pct=stop_pct,
            risk_pct=resolved_risk_pct,
            leverage=resolved_leverage,
            sizing_mode=resolved_position_sizing_mode,
            margin_allocation_pct=resolved_position_margin_allocation_pct,
        )
        effective_risk_pct = float(sizing.get("effective_risk_pct", 0.0) or 0.0)
        if effective_risk_pct > 0:
            effective_risk_values.append(effective_risk_pct)
        position_notional = float(
            sizing.get("position_notional_raw", sizing.get("position_notional", 0.0)) or 0.0
        )
        pnl_usdt = position_notional * (trade_net_pct / 100.0)
        equity += pnl_usdt

        side = str(trade.get("side") or "").strip().lower()
        if side == "long":
            long_pnl_usdt += pnl_usdt
        elif side == "short":
            short_pnl_usdt += pnl_usdt

        if pnl_usdt > best_trade_pnl_usdt:
            best_trade_pnl_usdt = pnl_usdt
            best_trade_index = trade_index
        if pnl_usdt < worst_trade_pnl_usdt:
            worst_trade_pnl_usdt = pnl_usdt
            worst_trade_index = trade_index

        if equity > peak_equity:
            peak_equity = equity
        if equity < trough_equity:
            trough_equity = equity

        drawdown_pct = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0.0
        if drawdown_pct > max_drawdown_pct:
            max_drawdown_pct = drawdown_pct

    trade_count = len(trades)
    avg_trade_pnl_usdt = (equity - resolved_initial_balance) / trade_count if trade_count else 0.0
    avg_effective_risk_pct = (
        sum(effective_risk_values) / len(effective_risk_values) if effective_risk_values else 0.0
    )
    max_effective_risk_pct = max(effective_risk_values) if effective_risk_values else 0.0

    return {
        "model": model_name,
        "initial_balance_usdt": round(resolved_initial_balance, 4),
        "final_balance_usdt": round(equity, 4),
        "net_profit_usdt": round(equity - resolved_initial_balance, 4),
        "return_pct": round((equity / resolved_initial_balance - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "risk_per_trade_pct": round(resolved_risk_pct, 4),
        "position_sizing_mode": resolved_position_sizing_mode,
        "position_margin_allocation_pct": round(resolved_position_margin_allocation_pct, 4),
        "leverage": round(resolved_leverage, 4),
        "avg_effective_risk_pct": round(avg_effective_risk_pct, 4),
        "max_effective_risk_pct": round(max_effective_risk_pct, 4),
        "avg_trade_pnl_usdt": round(avg_trade_pnl_usdt, 4),
        "long_pnl_usdt": round(long_pnl_usdt, 4),
        "short_pnl_usdt": round(short_pnl_usdt, 4),
        "best_balance_usdt": round(peak_equity, 4),
        "worst_balance_usdt": round(trough_equity, 4),
        "best_trade_pnl_usdt": round(best_trade_pnl_usdt if trade_count else 0.0, 4),
        "best_trade_index": best_trade_index,
        "worst_trade_pnl_usdt": round(worst_trade_pnl_usdt if trade_count else 0.0, 4),
        "worst_trade_index": worst_trade_index,
    }


def build_trade_diagnostics(trades):
    return {
        "phases_by_trade_order": _build_phase_summaries(trades),
        "streaks": _build_streak_diagnostics(trades),
        "giveback": _build_giveback_diagnostics(trades),
        "monthly": _build_monthly_diagnostics(trades),
        "equity": _build_equity_diagnostics(trades),
        "reason_breakdown": _build_reason_breakdown(trades),
    }


def save_detailed_report(
    trades,
    summary,
    params,
    symbol,
    timeframe,
    days,
    execution_profile: str,
    *,
    initial_balance: float,
    risk_per_trade_pct: float,
    leverage: float,
    position_sizing_mode: str,
    position_margin_allocation_pct: float,
):
    os.makedirs("reports/backtests", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reports/backtests/backtest_{symbol.replace('/', '_')}_{timeframe}_{days}d_{timestamp}.json"
    strategy_snapshot = config.build_runtime_strategy_snapshot()
    report = {
        "metadata": {
            "symbol": symbol,
            "timeframe": timeframe,
            "days": days,
            "execution_profile": execution_profile,
            "params": vars(params),
            "strategy_snapshot": strategy_snapshot,
            "account_model": {
                "initial_balance_usdt": round(float(initial_balance), 4),
                "risk_per_trade_pct": round(float(risk_per_trade_pct), 4),
                "position_sizing_mode": str(position_sizing_mode),
                "position_margin_allocation_pct": round(float(position_margin_allocation_pct), 4),
                "leverage": round(float(leverage), 4),
                "model": (
                    "fixed_margin_allocation"
                    if str(position_sizing_mode).strip().lower() == "allocation"
                    else "fixed_risk_per_trade"
                ),
            },
        },
        "summary": summary,
        "diagnostics": build_trade_diagnostics(trades),
        "trades": trades
    }
    with open(filename, 'w') as f:
        json.dump(report, f, indent=4)
    print(f"Relatorio detalhado salvo em: {filename}")

def format_timestamp(ts):
    if isinstance(ts, pd.Timestamp):
        return ts.tz_convert(timezone.utc).isoformat() if ts.tzinfo else ts.tz_localize(timezone.utc).isoformat()
    return str(ts)


def _resolve_execution_profile(explicit_profile: str | None = None) -> str:
    if explicit_profile is not None and str(explicit_profile).strip():
        return str(explicit_profile).strip().lower()
    configured_profile = str(getattr(config, "EXECUTION_PROFILE", "") or "").strip().lower()
    if configured_profile:
        return configured_profile
    return "managed"


def _create_backtest_position(
    *,
    signal: str,
    entry_price: float,
    timestamp,
    atr: float,
    execution_profile: str,
    signal_result: dict | None = None,
    candle_window=None,
):
    setup_payload = (signal_result or {}).get("setup") or {}
    entry_setup = str(setup_payload.get("setup") or "")
    entry_source_setup = str(setup_payload.get("source_setup") or "")
    if execution_profile == "native_bracket":
        return create_native_bracket_position(
            signal=signal,
            entry_price=entry_price,
            timestamp=timestamp,
            atr=atr,
        )
    return create_position(
        signal=signal,
        entry_price=entry_price,
        timestamp=timestamp,
        atr=atr,
        entry_setup=entry_setup,
        entry_source_setup=entry_source_setup,
        candle_window=candle_window,
    )


def _finalize_closed_trade(
    *,
    position_before_close: dict,
    closed_position: dict,
    realized_partial_pct: float,
    fee_pct: float,
    slippage_pct: float,
) -> dict:
    trade = dict(closed_position)
    trade["entry_timestamp"] = format_timestamp(trade["entry_timestamp"])
    trade["exit_timestamp"] = format_timestamp(trade["exit_timestamp"])
    for field_name in (
        "entry_signal_reason",
        "entry_setup",
        "entry_source_setup",
        "entry_regime",
        "signal_timestamp",
        "signal_hour_utc",
        "signal_rsi",
        "signal_adx",
        "signal_atr_pct",
        "signal_trend_strength_pct",
        "signal_context_gap_pct",
        "partial_taken",
        "break_even_active",
        "current_stop",
        "initial_stop",
        "partial_target",
        "trailing_trigger_price",
        "management_profile",
    ):
        if field_name in position_before_close:
            trade[field_name] = position_before_close.get(field_name)

    if position_before_close.get("partial_taken", False):
        final_gross_pct = trade["gross_pct"] * 0.5 + realized_partial_pct
    else:
        final_gross_pct = trade["gross_pct"]

    trade["gross_pct"] = final_gross_pct
    trade["net_pct"] = final_gross_pct - (fee_pct * 2) - slippage_pct
    return trade


def _build_forced_backtest_close(position: dict, row: pd.Series, *, reason: str) -> dict:
    exit_price = float(row["close"])
    if str(position.get("side") or "").strip().lower() == "long":
        gross_pct = ((exit_price - float(position["entry_price"])) / float(position["entry_price"])) * 100
    else:
        gross_pct = ((float(position["entry_price"]) - exit_price) / float(position["entry_price"])) * 100
    return {
        "side": position["side"],
        "entry_price": float(position["entry_price"]),
        "exit_price": exit_price,
        "entry_timestamp": position["entry_timestamp"],
        "exit_timestamp": row["timestamp"],
        "best_price": float(position["best_price"]),
        "gross_pct": gross_pct,
        "mfe_pct": float(position.get("mfe_pct", 0.0) or 0.0),
        "mae_pct": float(position.get("mae_pct", 0.0) or 0.0),
        "reason": str(reason or "ai_forced_exit"),
    }


def _attach_backtest_entry_context(
    *,
    position: dict,
    signal: dict,
    signal_row: pd.Series,
) -> dict:
    enriched = position.copy()
    setup = signal.get("setup") or {}
    close_price = float(signal_row.get("close") or 0.0)
    ema_fast = float(signal_row.get("ema_fast") or close_price or 0.0)
    ema_slow = float(signal_row.get("ema_slow") or close_price or 0.0)
    ema_trend = float(signal_row.get("ema_trend") or close_price or 0.0)
    trend_strength_pct = abs(ema_fast - ema_slow) / max(abs(close_price), 1e-9) * 100
    trend_context_gap_pct = abs(ema_slow - ema_trend) / max(abs(close_price), 1e-9) * 100
    signal_timestamp = signal_row.get("timestamp")
    signal_timestamp_iso = format_timestamp(signal_timestamp) if signal_timestamp is not None else None
    signal_hour_utc = None
    if signal_timestamp is not None:
        ts = pd.Timestamp(signal_timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize(timezone.utc)
        else:
            ts = ts.tz_convert(timezone.utc)
        signal_hour_utc = int(ts.hour)

    enriched.update(
        {
            "entry_signal_reason": str(signal.get("reason") or ""),
            "entry_setup": str(setup.get("setup") or ""),
            "entry_source_setup": str(setup.get("source_setup") or ""),
            "entry_regime": str((setup.get("regime") or {}).get("regime") or ""),
            "signal_timestamp": signal_timestamp_iso,
            "signal_hour_utc": signal_hour_utc,
            "signal_rsi": round(float(signal_row.get("rsi") or 0.0), 4),
            "signal_adx": round(float(signal_row.get("adx") or 0.0), 4),
            "signal_atr_pct": round(float(signal_row.get("atr_pct") or 0.0), 4),
            "signal_trend_strength_pct": round(float(trend_strength_pct), 4),
            "signal_context_gap_pct": round(float(trend_context_gap_pct), 4),
        }
    )
    return enriched


def _evaluate_backtest_position_on_candle(
    *,
    position: dict,
    row: pd.Series,
    realized_partial_pct: float,
) -> dict:
    return evaluate_managed_position_on_candle(
        position=position,
        candle=row,
        realized_partial_pct=realized_partial_pct,
    )


def _manage_backtest_position_on_candle(
    *,
    position: dict,
    row: pd.Series,
    realized_partial_pct: float,
    execution_profile: str,
) -> dict:
    if execution_profile == "native_bracket":
        management = evaluate_native_bracket_position_on_candle(position, row)
        if management["action"] == "close":
            return {
                "action": "close",
                "position_before_close": position,
                "closed_position": management["closed_position"],
                "realized_partial_pct": 0.0,
            }
        return {
            "action": "hold",
            "position": management["position"],
            "realized_partial_pct": 0.0,
        }
    return evaluate_managed_position_on_candle(
        position=position,
        candle=row,
        realized_partial_pct=realized_partial_pct,
    )


def summarize_trades(trades):
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    losses = len(trades) - wins
    net_pct = sum(t["net_pct"] for t in trades)

    gross_wins = sum(t["net_pct"] for t in trades if t["net_pct"] > 0)
    gross_losses = abs(sum(t["net_pct"] for t in trades if t["net_pct"] <= 0))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

    long_trades = [t for t in trades if t["side"] == "long"]
    short_trades = [t for t in trades if t["side"] == "short"]

    equity = 100.0
    peak = 100.0
    max_dd = 0.0
    for trade in trades:
        equity *= (1 + trade["net_pct"] / 100)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

    def get_side_summary(side_list):
        if not side_list:
            return {"trades": 0, "win_rate": 0, "net": 0}
        wins_count = sum(1 for t in side_list if t["net_pct"] > 0)
        return {
            "trades": len(side_list),
            "win_rate": round(wins_count / len(side_list) * 100, 2),
            "net": round(sum(t["net_pct"] for t in side_list), 4),
            "avg_mfe": round(sum(t.get("mfe_pct", 0) for t in side_list) / len(side_list), 2),
            "avg_mae": round(sum(t.get("mae_pct", 0) for t in side_list) / len(side_list), 2),
        }

    summary = {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / len(trades) * 100), 2) if trades else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown": round(max_dd, 2),
        "net_pct": round(net_pct, 4),
        "avg_trade_pct": round(net_pct / len(trades), 4) if trades else 0.0,
        "long_stats": get_side_summary(long_trades),
        "short_stats": get_side_summary(short_trades),
    }
    if trades:
        summary["periodo_testado"] = f"{trades[0]['entry_timestamp']} ate {trades[-1]['exit_timestamp']}"
    return summary


def check_governance_readiness(summary: dict, period_days: int) -> bool:
    profile = config.get_backtest_governance_profile(
        symbol=getattr(config, "SYMBOL", "BTC/USDT"),
        timeframe=getattr(config, "TIMEFRAME", "15m"),
        period_days=period_days,
    )
    min_trades = float(profile["min_trades"])
    pass_pf = summary["profit_factor"] >= float(profile["min_profit_factor"])
    pass_trades = summary["trades"] >= min_trades
    pass_dd = summary["max_drawdown"] <= float(profile["max_drawdown_pct"])
    pass_exp = summary["avg_trade_pct"] >= float(profile["min_expectancy_pct"])

    print(f"\n--- GOVERNANCE CHECK ({period_days}d) ---")
    print(f"Profit Factor: {summary['profit_factor']} / {float(profile['min_profit_factor'])} {'[OK]' if pass_pf else '[X]'}")
    print(f"Total Trades:  {summary['trades']} / {int(min_trades)} {'[OK]' if pass_trades else '[X]'}")
    print(f"Max Drawdown:  {summary['max_drawdown']}% / {float(profile['max_drawdown_pct'])}% {'[OK]' if pass_dd else '[X]'}")
    print(f"Expectancy:    {summary['avg_trade_pct']}% / {float(profile['min_expectancy_pct'])}% {'[OK]' if pass_exp else '[X]'}")

    is_ready = all([pass_pf, pass_trades, pass_dd, pass_exp])
    if is_ready:
        print(">>> STATUS: PRONTO PARA PRODUCAO (BASELINE VERDE) <<<")
    else:
        print(">>> STATUS: REPROVADO NA GOVERNANCA <<<")
    return is_ready


def load_backtest_websocket_db(
    *,
    symbol: str,
    timeframe: str,
    candles: int,
    days: int,
):
    del candles, days
    coverage = db.get_backtest_websocket_candle_coverage(symbol=symbol, timeframe=timeframe)
    if int(coverage.get("total") or 0) <= 0:
        raise RuntimeError("Nenhum historico websocket persistido encontrado para este ativo/timeframe.")

    rows = db.get_backtest_websocket_candles(symbol=symbol, timeframe=timeframe)
    if not rows:
        raise RuntimeError("Cobertura websocket encontrada, mas sem candles persistidos para carregar.")

    df = pd.DataFrame(rows)
    if "candle_timestamp" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"candle_timestamp": "timestamp"})

    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"]).sort_values("timestamp")
    return df.reset_index(drop=True), coverage


def run_backtest(
    symbol: str,
    timeframe: str,
    candles: int,
    fee_pct: float,
    testnet: bool = False,
    use_local_csv: bool = True,
    slippage_pct: float = 0.02,
    preloaded_df: pd.DataFrame | None = None,
    execution_profile: str | None = None,
    precomputed_indicators: bool = False,
    verbose: bool = True,
    save_report: bool = True,
    initial_balance: float | None = None,
    risk_per_trade_pct: float | None = None,
    position_sizing_mode: str | None = None,
    position_margin_allocation_pct: float | None = None,
    order_balance_usage_pct: float | None = None,
    leverage: float | None = None,
    strategy_params: StrategyParams | None = None,
):
    symbol_override_report = config.apply_symbol_strategy_overrides(symbol)
    params = strategy_params or StrategyParams()
    resolved_execution_profile = _resolve_execution_profile(execution_profile)
    resolved_initial_balance = float(
        initial_balance
        if initial_balance is not None
        else getattr(config.ProductionConfig, "PAPER_ACCOUNT_BALANCE", 0.0)
    )
    resolved_risk_per_trade_pct = float(
        risk_per_trade_pct
        if risk_per_trade_pct is not None
        else getattr(config.ProductionConfig, "RISK_PER_TRADE_PCT", getattr(config, "RISK_PER_TRADE_PCT", 0.0))
    )
    resolved_position_sizing_mode = str(
        position_sizing_mode
        if position_sizing_mode is not None
        else getattr(config.ProductionConfig, "POSITION_SIZING_MODE", getattr(config, "POSITION_SIZING_MODE", "risk"))
    ).strip().lower()
    resolved_position_margin_allocation_pct = max(
        float(
            (
                order_balance_usage_pct
                if order_balance_usage_pct is not None
                else getattr(
                    config.ProductionConfig,
                    "ORDER_BALANCE_USAGE_PCT",
                    getattr(config, "ORDER_BALANCE_USAGE_PCT", 100.0),
                )
            )
            if resolved_position_sizing_mode == "order_value"
            else (
                position_margin_allocation_pct
                if position_margin_allocation_pct is not None
                else getattr(
                    config.ProductionConfig,
                    "POSITION_MARGIN_ALLOCATION_PCT",
                    getattr(config, "POSITION_MARGIN_ALLOCATION_PCT", 50.0),
                )
            )
        ),
        0.0,
    )
    resolved_leverage = max(
        float(leverage if leverage is not None else getattr(config, "LEVERAGE", 1) or 1),
        1.0,
    )
    memory_symbol = symbol.replace("/", "_").replace(":", "_")
    backtest_memory_path = os.path.join("reports", "backtests", f"_learning_{memory_symbol}_{timeframe}.json")
    unified_engine = UnifiedDecisionEngine(
        symbol=symbol,
        timeframe=timeframe,
        use_live_context=False,
    )
    unified_engine.learning_service.path = Path(backtest_memory_path)
    unified_engine.learning_service.reset()

    if preloaded_df is not None:
        df = preloaded_df
    elif use_local_csv:
        try:
            df = fetch_historical_candles_from_csv(symbol, timeframe, total_limit=candles)
        except FileNotFoundError:
            print("Aviso: arquivo local nao encontrado. Usando API...")
            df = fetch_historical_candles(symbol, timeframe, total_limit=candles, testnet=testnet)
    else:
        df = fetch_historical_candles(symbol, timeframe, total_limit=candles, testnet=testnet)

    if not precomputed_indicators:
        df = calculate_indicators(df, params)
    ai_feature_df = prepare_feature_frame(df.copy(), params)

    position = None
    pending_signal = None
    trades = []
    realized_partial_pct = 0.0

    start_index = get_min_required_rows(params)

    for i in range(start_index, len(df) - 1):
        row = df.iloc[i]
        next_row = df.iloc[i + 1]
        candle_slice = df.iloc[: i + 1]
        feature_row = ai_feature_df.iloc[i]

        if position is not None:
            management = _manage_backtest_position_on_candle(
                position=position,
                row=row,
                realized_partial_pct=realized_partial_pct,
                execution_profile=resolved_execution_profile,
            )

            if management["action"] == "close":
                closed_trade = _finalize_closed_trade(
                    position_before_close=management["position_before_close"],
                    closed_position=management["closed_position"],
                    realized_partial_pct=management["realized_partial_pct"],
                    fee_pct=fee_pct,
                    slippage_pct=slippage_pct,
                )
                trades.append(closed_trade)
                unified_engine.register_trade_outcome(closed_trade)
                position = None
                realized_partial_pct = 0.0
            else:
                position = management["position"]
                realized_partial_pct = management["realized_partial_pct"]

        if position is not None:
            ai_exit = unified_engine.should_exit_position(
                position=position,
                candle_slice=candle_slice,
                feature_row=feature_row,
            )
            if ai_exit.get("exit"):
                closed_trade = _finalize_closed_trade(
                    position_before_close=position,
                    closed_position=_build_forced_backtest_close(
                        position,
                        row,
                        reason=str(ai_exit.get("reason") or "ai_forced_exit"),
                    ),
                    realized_partial_pct=realized_partial_pct,
                    fee_pct=fee_pct,
                    slippage_pct=slippage_pct,
                )
                trades.append(closed_trade)
                unified_engine.register_trade_outcome(closed_trade)
                position = None
                realized_partial_pct = 0.0

        # Evita recriar/copiar slices do DataFrame inteiro a cada candle.
        # O motor ja aceita um indice explicito, entao mantemos a mesma logica
        # com custo bem menor para pesquisa e auditoria.
        signal = unified_engine.decide_entry(candle_slice, params, feature_row=feature_row)

        if position is None and signal.get("signal") in {"buy", "sell"}:
            pending_signal = signal
        elif position is not None:
            pending_signal = None

        if position is None and pending_signal is not None:
            if resolved_execution_profile == "native_bracket":
                entry_price = float(row["close"])
                entry_timestamp = row["timestamp"]
            elif getattr(config, "USE_NEXT_CANDLE_OPEN_FOR_BACKTEST", True):
                entry_price = float(next_row["open"])
                entry_timestamp = next_row["timestamp"]
            else:
                entry_price = float(row["close"])
                entry_timestamp = row["timestamp"]

            position = _create_backtest_position(
                signal=pending_signal["signal"],
                entry_price=entry_price,
                timestamp=entry_timestamp,
                atr=float(pending_signal["atr"]),
                execution_profile=resolved_execution_profile,
                signal_result=pending_signal,
                candle_window=candle_slice,
            )
            position = _attach_backtest_entry_context(
                position=position,
                signal=pending_signal,
                signal_row=row,
            )
            pending_signal = None

    if position is not None:
        last_row = df.iloc[-1]
        management = _manage_backtest_position_on_candle(
            position=position,
            row=last_row,
            realized_partial_pct=realized_partial_pct,
            execution_profile=resolved_execution_profile,
        )

        if management["action"] == "close":
            closed_trade = _finalize_closed_trade(
                position_before_close=management["position_before_close"],
                closed_position=management["closed_position"],
                realized_partial_pct=management["realized_partial_pct"],
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
            )
            trades.append(closed_trade)
            unified_engine.register_trade_outcome(closed_trade)
        else:
            position = management["position"]
            realized_partial_pct = management["realized_partial_pct"]
            exit_price = float(last_row["close"])

            gross_pct = (
                (exit_price - position["entry_price"]) / position["entry_price"] * 100
                if position["side"] == "long"
                else (position["entry_price"] - exit_price) / position["entry_price"] * 100
            )

            if resolved_execution_profile != "native_bracket" and position.get("partial_taken", False):
                final_gross_pct = gross_pct * 0.5 + realized_partial_pct
            else:
                final_gross_pct = gross_pct

            closing_trade = {
                "side": position["side"],
                "entry_price": float(position["entry_price"]),
                "exit_price": exit_price,
                "entry_timestamp": format_timestamp(position["entry_timestamp"]),
                "exit_timestamp": format_timestamp(last_row["timestamp"]),
                "best_price": float(position["best_price"]),
                "gross_pct": final_gross_pct,
                "net_pct": final_gross_pct - (fee_pct * 2) - slippage_pct,
                "reason": "encerramento_backtest",
                "entry_signal_reason": position.get("entry_signal_reason"),
                "entry_setup": position.get("entry_setup"),
                "entry_source_setup": position.get("entry_source_setup"),
                "entry_regime": position.get("entry_regime"),
                "signal_timestamp": position.get("signal_timestamp"),
                "signal_hour_utc": position.get("signal_hour_utc"),
                "signal_rsi": position.get("signal_rsi"),
                "signal_adx": position.get("signal_adx"),
                "signal_atr_pct": position.get("signal_atr_pct"),
                "signal_trend_strength_pct": position.get("signal_trend_strength_pct"),
                "signal_context_gap_pct": position.get("signal_context_gap_pct"),
                "partial_taken": bool(position.get("partial_taken", False)),
                "break_even_active": bool(position.get("break_even_active", False)),
                "current_stop": position.get("current_stop"),
                "initial_stop": position.get("initial_stop"),
                "partial_target": position.get("partial_target"),
                "trailing_trigger_price": position.get("trailing_trigger_price"),
                "management_profile": position.get("management_profile"),
            }
            trades.append(closing_trade)
            unified_engine.register_trade_outcome(closing_trade)

    summary = summarize_trades(trades)
    summary["account_risk_model"] = build_account_risk_summary(
        trades,
        initial_balance=resolved_initial_balance,
        risk_per_trade_pct=resolved_risk_per_trade_pct,
        leverage=resolved_leverage,
        position_sizing_mode=resolved_position_sizing_mode,
        position_margin_allocation_pct=resolved_position_margin_allocation_pct,
        order_balance_usage_pct=(
            resolved_position_margin_allocation_pct if resolved_position_sizing_mode == "order_value" else None
        ),
    )
    if verbose:
        applied = symbol_override_report.get("applied") or {}
        if applied:
            print(f"Overrides simbolo aplicados para {symbol}: {applied}")
        print("Resumo:", summary)

    if trades:
        if verbose:
            print("Primeiro trade:", trades[0])
            print("Ultimo trade:", trades[-1])

    if save_report:
        # NOVO: Garante que o relatorio seja salvo sempre, facilitando minha leitura posterior
        days_simulated = (df.iloc[-1]['timestamp'] - df.iloc[0]['timestamp']).days
        save_detailed_report(
            trades,
            summary,
            params,
            symbol,
            timeframe,
            days_simulated,
            resolved_execution_profile,
            initial_balance=resolved_initial_balance,
            risk_per_trade_pct=resolved_risk_per_trade_pct,
            leverage=resolved_leverage,
            position_sizing_mode=resolved_position_sizing_mode,
            position_margin_allocation_pct=resolved_position_margin_allocation_pct,
        )

    return trades, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--timeframe", default=config.TIMEFRAME)
    parser.add_argument("--candles", type=int, default=3000)
    parser.add_argument("--fee-pct", type=float, default=config.FEE_PCT)
    parser.add_argument("--slippage-pct", type=float, default=config.SLIPPAGE_PCT)
    parser.add_argument("--initial-balance", type=float, default=config.ProductionConfig.PAPER_ACCOUNT_BALANCE)
    parser.add_argument("--risk-per-trade-pct", type=float, default=config.ProductionConfig.RISK_PER_TRADE_PCT)
    parser.add_argument(
        "--position-sizing-mode",
        choices=["risk", "allocation", "hybrid", "order_value"],
        default=getattr(config.ProductionConfig, "POSITION_SIZING_MODE", "risk"),
    )
    parser.add_argument(
        "--position-margin-allocation-pct",
        type=float,
        default=getattr(config.ProductionConfig, "POSITION_MARGIN_ALLOCATION_PCT", 50.0),
    )
    parser.add_argument(
        "--order-balance-usage-pct",
        type=float,
        default=getattr(config.ProductionConfig, "ORDER_BALANCE_USAGE_PCT", getattr(config, "ORDER_BALANCE_USAGE_PCT", 100.0)),
    )
    parser.add_argument("--leverage", type=float, default=float(getattr(config, "LEVERAGE", 1) or 1))
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument(
        "--use-local-csv",
        action="store_true",
        default=True,
        help="Usar historico CSV local quando disponivel.",
    )
    parser.add_argument(
        "--no-local-csv",
        dest="use_local_csv",
        action="store_false",
        help="Usar API em vez do CSV local.",
    )
    args = parser.parse_args()

    print(f"Iniciando bateria de testes para {args.symbol}...")
    periods = {30: 2880, 90: 8640, 180: 17280, 365: 35040}

    results_summary = []
    for days, candle_count in periods.items():
        print(f"\n{'=' * 40}\nTESTE DE {days} DIAS ({candle_count} candles)\n{'=' * 40}")
        try:
            trades, summary = run_backtest(
                args.symbol,
                args.timeframe,
                candle_count,
                args.fee_pct,
                testnet=args.testnet,
                use_local_csv=args.use_local_csv,
                slippage_pct=args.slippage_pct,
                initial_balance=args.initial_balance,
                risk_per_trade_pct=args.risk_per_trade_pct,
                position_sizing_mode=args.position_sizing_mode,
                position_margin_allocation_pct=args.position_margin_allocation_pct,
                order_balance_usage_pct=args.order_balance_usage_pct,
                leverage=args.leverage,
            )
            ready = check_governance_readiness(summary, days)
            results_summary.append({"days": days, "net": summary["net_pct"], "ready": ready})
        except Exception as exc:
            print(f"Erro no periodo {days}d: {exc}")

    print("\n\nRESUMO FINAL DA BATERIA:")
    for result in results_summary:
        status = "PRONTO" if result["ready"] else "REPROVADO"
        print(f"{result['days']}d: {result['net']:>8}% | {status}")
