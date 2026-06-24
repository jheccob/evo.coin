from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List

import backtest
import config
from market_data import fetch_historical_candles_from_csv
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal, get_min_required_rows


SEARCH_KEYS = {
    "BUY_RSI_SIGNAL",
    "LONG_ADX_THRESHOLD",
    "LONG_VOLUME_RATIO_REQUIRED",
    "MIN_LONG_SCORE",
    "BLOCKED_LONG_ENTRY_HOURS_UTC",
    "SHORT_ADX_THRESHOLD",
    "SHORT_VOLUME_RATIO_REQUIRED",
    "MIN_SHORT_SCORE",
}


@dataclass(frozen=True)
class Candidate:
    name: str
    updates: Dict[str, object]


CONSERVATIVE_CANDIDATES: List[Candidate] = [
    Candidate("baseline", {}),
    Candidate("buy55", {"BUY_RSI_SIGNAL": 55.0}),
    Candidate("long_vol_1.4", {"LONG_VOLUME_RATIO_REQUIRED": 1.4}),
    Candidate("long_vol_1.3", {"LONG_VOLUME_RATIO_REQUIRED": 1.3}),
    Candidate("buy55_vol1.4", {"BUY_RSI_SIGNAL": 55.0, "LONG_VOLUME_RATIO_REQUIRED": 1.4}),
    Candidate("buy55_vol1.3", {"BUY_RSI_SIGNAL": 55.0, "LONG_VOLUME_RATIO_REQUIRED": 1.3}),
    Candidate("long_adx22", {"LONG_ADX_THRESHOLD": 22.0}),
    Candidate("buy55_adx22", {"BUY_RSI_SIGNAL": 55.0, "LONG_ADX_THRESHOLD": 22.0}),
    Candidate("unblock_long_14", {"BLOCKED_LONG_ENTRY_HOURS_UTC": [1, 8, 13]}),
]

AGGRESSIVE_CANDIDATES: List[Candidate] = [
    Candidate("long_score7", {"MIN_LONG_SCORE": 7}),
    Candidate("long_score7_block02323", {"MIN_LONG_SCORE": 7, "BLOCKED_LONG_ENTRY_HOURS_UTC": [0, 1, 2, 3, 8, 13, 14, 23]}),
    Candidate("short_score6_adx21", {"MIN_SHORT_SCORE": 6, "SHORT_ADX_THRESHOLD": 21.0}),
]

WINDOWS = {
    "full_35000": ("tail", 35000),
    "last_180d": ("tail", 17280),
    "first_180d": ("head", 17280),
    "last_90d": ("tail", 8640),
}


def build_runtime_params() -> StrategyParams:
    return StrategyParams(
        ema_fast=int(config.FAST_EMA),
        ema_slow=int(config.SLOW_EMA),
        ema_trend=int(config.TREND_EMA),
        rsi_period=int(config.RSI_PERIOD),
        atr_period=int(config.ATR_PERIOD),
        buy_rsi_floor=float(config.BUY_RSI_SIGNAL),
        sell_rsi_ceiling=float(config.SELL_RSI_SIGNAL),
        long_min_atr_pct=float(config.LONG_MIN_ATR_PCT),
        short_min_atr_pct=float(config.SHORT_MIN_ATR_PCT),
        long_regime_gap_pct=float(config.LONG_TREND_GAP_PCT),
        short_regime_gap_pct=float(config.SHORT_TREND_GAP_PCT),
        pullback_buffer_pct=float(config.PULLBACK_BUFFER_PCT),
        long_partial_pct=float(config.LONG_TAKE_PROFIT_PCT) * 0.55,
        short_partial_pct=float(config.SHORT_TAKE_PROFIT_PCT) * 0.55,
        long_stop_pct=float(config.LONG_STOP_LOSS_PCT),
        short_stop_pct=float(config.SHORT_STOP_LOSS_PCT),
        long_trailing_pct=float(config.LONG_TRAILING_STOP_PCT),
        short_trailing_pct=float(config.SHORT_TRAILING_STOP_PCT),
        long_max_distance_pct=float(config.LONG_MAX_DISTANCE_EMA_PCT),
        short_max_distance_pct=float(config.SHORT_MAX_DISTANCE_EMA_PCT),
        long_slope_lookback=int(config.LONG_SLOPE_LOOKBACK),
        long_trend_ema_lookback=int(config.LONG_TREND_EMA_LOOKBACK),
        long_fast_slow_gap_pct=float(config.LONG_FAST_SLOW_GAP_PCT),
        short_slope_lookback=int(config.SHORT_SLOPE_LOOKBACK),
        short_trend_ema_lookback=int(config.SHORT_TREND_EMA_LOOKBACK),
        short_fast_slow_gap_pct=float(config.SHORT_FAST_SLOW_GAP_PCT),
    )


def apply_updates(snapshot: Dict[str, object], updates: Dict[str, object]) -> None:
    for key, value in snapshot.items():
        setattr(config, key, value)
    for key, value in updates.items():
        setattr(config, key, value)


def slice_window(df, mode: str, size: int):
    if mode == "head":
        return df.head(size).reset_index(drop=True)
    return df.tail(size).reset_index(drop=True)


def run_threshold_backtest(featured_df):
    params = build_runtime_params()
    resolved_execution_profile = config.EXECUTION_PROFILE
    position = None
    pending_signal = None
    trades = []
    realized_partial_pct = 0.0
    start_index = get_min_required_rows(params)

    for i in range(start_index, len(featured_df) - 1):
        row = featured_df.iloc[i]

        if position is not None:
            management = backtest._manage_backtest_position_on_candle(
                position=position,
                row=row,
                realized_partial_pct=realized_partial_pct,
                execution_profile=resolved_execution_profile,
            )

            if management["action"] == "close":
                trades.append(
                    backtest._finalize_closed_trade(
                        position_before_close=management["position_before_close"],
                        closed_position=management["closed_position"],
                        realized_partial_pct=management["realized_partial_pct"],
                        fee_pct=config.FEE_PCT,
                        slippage_pct=config.SLIPPAGE_PCT,
                    )
                )
                position = None
                realized_partial_pct = 0.0
            else:
                position = management["position"]
                realized_partial_pct = management["realized_partial_pct"]

        signal = generate_entry_signal(featured_df, params, index=i)

        if position is None and signal.get("signal") in {"buy", "sell"}:
            pending_signal = signal
        elif position is not None:
            pending_signal = None

        if position is None and pending_signal is not None:
            position = backtest._create_backtest_position(
                signal=pending_signal["signal"],
                entry_price=float(row["close"]),
                timestamp=row["timestamp"],
                atr=float(pending_signal["atr"]),
                execution_profile=resolved_execution_profile,
            )
            pending_signal = None

    if position is not None:
        last_row = featured_df.iloc[-1]
        management = backtest._manage_backtest_position_on_candle(
            position=position,
            row=last_row,
            realized_partial_pct=realized_partial_pct,
            execution_profile=resolved_execution_profile,
        )

        if management["action"] == "close":
            trades.append(
                backtest._finalize_closed_trade(
                    position_before_close=management["position_before_close"],
                    closed_position=management["closed_position"],
                    realized_partial_pct=management["realized_partial_pct"],
                    fee_pct=config.FEE_PCT,
                    slippage_pct=config.SLIPPAGE_PCT,
                )
            )
        else:
            position = management["position"]
            exit_price = float(last_row["close"])
            gross_pct = (
                (exit_price - position["entry_price"]) / position["entry_price"] * 100
                if position["side"] == "long"
                else (position["entry_price"] - exit_price) / position["entry_price"] * 100
            )
            trades.append(
                {
                    "side": position["side"],
                    "entry_price": float(position["entry_price"]),
                    "exit_price": exit_price,
                    "entry_timestamp": backtest.format_timestamp(position["entry_timestamp"]),
                    "exit_timestamp": backtest.format_timestamp(last_row["timestamp"]),
                    "best_price": float(position["best_price"]),
                    "gross_pct": gross_pct,
                    "net_pct": gross_pct - (config.FEE_PCT * 2) - config.SLIPPAGE_PCT,
                    "reason": "encerramento_backtest",
                }
            )

    return backtest.summarize_trades(trades)


def summary_line(name: str, updates: Dict[str, object], summary: dict) -> dict:
    return {
        "name": name,
        "updates": dict(updates),
        "trades": summary["trades"],
        "win_rate_pct": summary["win_rate_pct"],
        "profit_factor": summary["profit_factor"],
        "max_drawdown": summary["max_drawdown"],
        "net_pct": summary["net_pct"],
        "long_trades": summary["long_stats"]["trades"],
        "short_trades": summary["short_stats"]["trades"],
        "long_net": summary["long_stats"]["net"],
        "short_net": summary["short_stats"]["net"],
    }


def select_shortlist(results: Iterable[dict]) -> List[dict]:
    shortlisted = []
    for result in results:
        if result["trades"] < 114:
            continue
        if result["profit_factor"] < 1.35:
            continue
        if result["max_drawdown"] > 10.0:
            continue
        if result["net_pct"] < 38.0:
            continue
        shortlisted.append(result)
    return shortlisted


def main() -> None:
    snapshot = {key: getattr(config, key) for key in SEARCH_KEYS}
    raw_df = fetch_historical_candles_from_csv("BTC/USDT", "15m", total_limit=35000)
    featured_df = calculate_indicators(raw_df, build_runtime_params())
    candidates = CONSERVATIVE_CANDIDATES + AGGRESSIVE_CANDIDATES

    annual_results: List[dict] = []
    shortlist_results: List[dict] = []

    try:
        for candidate in candidates:
            apply_updates(snapshot, candidate.updates)
            summary = run_threshold_backtest(featured_df)
            annual_results.append(summary_line(candidate.name, candidate.updates, summary))

        for item in select_shortlist(annual_results):
            apply_updates(snapshot, item["updates"])
            windows = {}
            for window_name, (mode, size) in WINDOWS.items():
                window_summary = run_threshold_backtest(slice_window(featured_df, mode, size))
                windows[window_name] = window_summary
            shortlist_results.append(
                {
                    "name": item["name"],
                    "updates": item["updates"],
                    "windows": windows,
                }
            )
    finally:
        apply_updates(snapshot, {})

    print(
        json.dumps(
            {
                "annual_results": annual_results,
                "shortlist": shortlist_results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
