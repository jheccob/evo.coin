from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from market_data import fetch_historical_candles, fetch_historical_candles_from_csv
from position_manager import calculate_trade_pct, create_position, evaluate_managed_position_on_candle
from strategy_engine import StrategyParams, calculate_indicators

LABEL_NAMES = ["short", "hold", "long"]
LABEL_TO_ID = {label: index for index, label in enumerate(LABEL_NAMES)}
LABEL_MODES = {"target_window", "trade_outcome", "trade_journal"}

FEATURE_COLUMNS = [
    "source_is_btc",
    "source_is_xlm",
    "return_1_pct",
    "return_3_pct",
    "return_6_pct",
    "return_12_pct",
    "return_24_pct",
    "return_48_pct",
    "range_pct",
    "body_pct",
    "upper_wick_pct",
    "lower_wick_pct",
    "close_location_pct",
    "ema_fast_gap_pct",
    "ema_slow_gap_pct",
    "ema_trend_gap_pct",
    "fast_slow_gap_pct",
    "slow_trend_gap_pct",
    "trend_strength_pct",
    "ema_fast_slope_pct",
    "ema_slow_slope_pct",
    "ema_trend_slope_pct",
    "macd_line_pct",
    "macd_signal_pct",
    "macd_hist_pct",
    "rsi",
    "rsi_delta",
    "adx",
    "adx_delta",
    "atr_pct",
    "atr_pct_delta",
    "bollinger_width_pct",
    "bollinger_position",
    "stoch_k",
    "stoch_d",
    "rolling_volatility_12_pct",
    "rolling_volatility_24_pct",
    "rolling_vwap_gap_pct",
    "rolling_vwap_slow_gap_pct",
    "volume_ratio",
    "volume_ratio_fast",
    "signed_volume_flow",
    "ema_regime_bias",
    "distance_to_rolling_high_pct",
    "distance_to_rolling_low_pct",
    "distance_to_rolling_high_96_pct",
    "distance_to_rolling_low_96_pct",
    "channel_width_32_pct",
    "channel_width_96_pct",
    "channel_position_32",
    "channel_position_96",
    "resistance_pressure_score",
    "support_pressure_score",
    "range_regime_score",
    "trend_regime_score",
]


def load_market_frame(
    symbol: str,
    timeframe: str,
    *,
    total_limit: int | None = 20000,
    use_local_csv: bool = True,
    testnet: bool = False,
) -> pd.DataFrame:
    resolved_limit = None if total_limit is None or int(total_limit) <= 0 else int(total_limit)
    if use_local_csv:
        if resolved_limit is None:
            resolved_limit = 999999999
        return fetch_historical_candles_from_csv(symbol, timeframe, total_limit=resolved_limit)
    if resolved_limit is None:
        resolved_limit = 200000
    return fetch_historical_candles(symbol, timeframe, total_limit=resolved_limit, testnet=testnet)


def prepare_feature_frame(df: pd.DataFrame, params: StrategyParams | None = None) -> pd.DataFrame:
    params = params or StrategyParams()
    feature_df = calculate_indicators(df.copy(), params)

    close = feature_df["close"].replace(0, pd.NA)
    open_price = feature_df["open"].replace(0, pd.NA)
    candle_range = (feature_df["high"] - feature_df["low"]).replace(0, pd.NA)
    upper_body = feature_df[["open", "close"]].max(axis=1)
    lower_body = feature_df[["open", "close"]].min(axis=1)

    feature_df["return_1_pct"] = feature_df["close"].pct_change(1) * 100
    feature_df["return_3_pct"] = feature_df["close"].pct_change(3) * 100
    feature_df["return_6_pct"] = feature_df["close"].pct_change(6) * 100
    feature_df["return_12_pct"] = feature_df["close"].pct_change(12) * 100
    feature_df["return_24_pct"] = feature_df["close"].pct_change(24) * 100
    feature_df["return_48_pct"] = feature_df["close"].pct_change(48) * 100
    feature_df["range_pct"] = ((feature_df["high"] - feature_df["low"]) / close) * 100
    feature_df["body_pct"] = ((feature_df["close"] - feature_df["open"]) / open_price) * 100
    feature_df["upper_wick_pct"] = ((feature_df["high"] - upper_body) / close) * 100
    feature_df["lower_wick_pct"] = ((lower_body - feature_df["low"]) / close) * 100
    feature_df["close_location_pct"] = ((feature_df["close"] - feature_df["low"]) / candle_range).clip(lower=0.0, upper=1.0)
    feature_df["ema_fast_gap_pct"] = ((feature_df["close"] - feature_df["ema_fast"]) / close) * 100
    feature_df["ema_slow_gap_pct"] = ((feature_df["close"] - feature_df["ema_slow"]) / close) * 100
    feature_df["ema_trend_gap_pct"] = ((feature_df["close"] - feature_df["ema_trend"]) / close) * 100
    feature_df["fast_slow_gap_pct"] = ((feature_df["ema_fast"] - feature_df["ema_slow"]) / close) * 100
    feature_df["slow_trend_gap_pct"] = ((feature_df["ema_slow"] - feature_df["ema_trend"]) / close) * 100
    feature_df["trend_strength_pct"] = ((feature_df["ema_fast"] - feature_df["ema_slow"]).abs() / close) * 100
    feature_df["ema_fast_slope_pct"] = feature_df["ema_fast"].pct_change(3) * 100
    feature_df["ema_slow_slope_pct"] = feature_df["ema_slow"].pct_change(3) * 100
    feature_df["ema_trend_slope_pct"] = feature_df["ema_trend"].pct_change(3) * 100

    macd_fast = feature_df["close"].ewm(span=12, adjust=False).mean()
    macd_slow = feature_df["close"].ewm(span=26, adjust=False).mean()
    macd_line = macd_fast - macd_slow
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    feature_df["macd_line_pct"] = (macd_line / close) * 100
    feature_df["macd_signal_pct"] = (macd_signal / close) * 100
    feature_df["macd_hist_pct"] = (macd_hist / close) * 100

    feature_df["rsi_delta"] = feature_df["rsi"].diff(3)
    feature_df["adx_delta"] = feature_df["adx"].diff(3)
    feature_df["atr_pct_delta"] = feature_df["atr_pct"].diff(3)

    rolling_mean_20 = feature_df["close"].rolling(20).mean()
    rolling_std_20 = feature_df["close"].rolling(20).std()
    bollinger_upper = rolling_mean_20 + (rolling_std_20 * 2.0)
    bollinger_lower = rolling_mean_20 - (rolling_std_20 * 2.0)
    bollinger_range = (bollinger_upper - bollinger_lower).replace(0, pd.NA)
    feature_df["bollinger_width_pct"] = (bollinger_range / close) * 100
    feature_df["bollinger_position"] = ((feature_df["close"] - bollinger_lower) / bollinger_range).clip(lower=0.0, upper=1.0)

    stochastic_low = feature_df["low"].rolling(14).min()
    stochastic_high = feature_df["high"].rolling(14).max()
    stochastic_range = (stochastic_high - stochastic_low).replace(0, pd.NA)
    feature_df["stoch_k"] = ((feature_df["close"] - stochastic_low) / stochastic_range) * 100
    feature_df["stoch_d"] = feature_df["stoch_k"].rolling(3).mean()

    log_returns = np.log(feature_df["close"] / feature_df["close"].shift(1))
    feature_df["rolling_volatility_12_pct"] = log_returns.rolling(12).std() * np.sqrt(12) * 100
    feature_df["rolling_volatility_24_pct"] = log_returns.rolling(24).std() * np.sqrt(24) * 100

    typical_price = (feature_df["high"] + feature_df["low"] + feature_df["close"]) / 3.0
    rolling_vwap_num_fast = (typical_price * feature_df["volume"]).rolling(32).sum()
    rolling_vwap_den_fast = feature_df["volume"].rolling(32).sum().replace(0, pd.NA)
    rolling_vwap_fast = rolling_vwap_num_fast / rolling_vwap_den_fast
    rolling_vwap_num_slow = (typical_price * feature_df["volume"]).rolling(96).sum()
    rolling_vwap_den_slow = feature_df["volume"].rolling(96).sum().replace(0, pd.NA)
    rolling_vwap_slow = rolling_vwap_num_slow / rolling_vwap_den_slow
    feature_df["rolling_vwap_gap_pct"] = ((feature_df["close"] - rolling_vwap_fast) / close) * 100
    feature_df["rolling_vwap_slow_gap_pct"] = ((feature_df["close"] - rolling_vwap_slow) / close) * 100

    feature_df["volume_ratio"] = feature_df["volume"] / feature_df["vol_ma"].replace(0, pd.NA)
    feature_df["volume_ratio_fast"] = feature_df["volume"] / feature_df["volume"].rolling(8).mean().replace(0, pd.NA)
    feature_df["signed_volume_flow"] = np.sign(feature_df["close"].diff().fillna(0.0)) * feature_df["volume_ratio"]
    feature_df["ema_regime_bias"] = 0.0
    feature_df.loc[
        (feature_df["ema_fast"] > feature_df["ema_slow"]) & (feature_df["ema_slow"] > feature_df["ema_trend"]),
        "ema_regime_bias",
    ] = 1.0
    feature_df.loc[
        (feature_df["ema_fast"] < feature_df["ema_slow"]) & (feature_df["ema_slow"] < feature_df["ema_trend"]),
        "ema_regime_bias",
    ] = -1.0

    rolling_high = feature_df["high"].rolling(32).max().replace(0, pd.NA)
    rolling_low = feature_df["low"].rolling(32).min().replace(0, pd.NA)
    rolling_high_96 = feature_df["high"].rolling(96).max().replace(0, pd.NA)
    rolling_low_96 = feature_df["low"].rolling(96).min().replace(0, pd.NA)
    channel_width_32 = (rolling_high - rolling_low).replace(0, pd.NA)
    channel_width_96 = (rolling_high_96 - rolling_low_96).replace(0, pd.NA)
    feature_df["distance_to_rolling_high_pct"] = ((rolling_high - feature_df["close"]) / close) * 100
    feature_df["distance_to_rolling_low_pct"] = ((feature_df["close"] - rolling_low) / close) * 100
    feature_df["distance_to_rolling_high_96_pct"] = ((rolling_high_96 - feature_df["close"]) / close) * 100
    feature_df["distance_to_rolling_low_96_pct"] = ((feature_df["close"] - rolling_low_96) / close) * 100
    feature_df["channel_width_32_pct"] = (channel_width_32 / close) * 100
    feature_df["channel_width_96_pct"] = (channel_width_96 / close) * 100
    feature_df["channel_position_32"] = ((feature_df["close"] - rolling_low) / channel_width_32).clip(lower=0.0, upper=1.0)
    feature_df["channel_position_96"] = ((feature_df["close"] - rolling_low_96) / channel_width_96).clip(lower=0.0, upper=1.0)
    feature_df["resistance_pressure_score"] = (
        1.0 - ((rolling_high - feature_df["close"]) / channel_width_32)
    ).clip(lower=0.0, upper=1.0)
    feature_df["support_pressure_score"] = (
        1.0 - ((feature_df["close"] - rolling_low) / channel_width_32)
    ).clip(lower=0.0, upper=1.0)

    adx_norm = (feature_df["adx"] / 50.0).clip(lower=0.0, upper=1.0)
    slope_energy = (
        feature_df["ema_fast_slope_pct"].abs()
        + feature_df["ema_slow_slope_pct"].abs()
        + feature_df["ema_trend_slope_pct"].abs()
    ) / 3.0
    slope_norm = (slope_energy / 1.2).clip(lower=0.0, upper=1.0)
    width_ratio = (feature_df["channel_width_32_pct"] / feature_df["channel_width_96_pct"].replace(0, pd.NA)).clip(
        lower=0.0,
        upper=2.0,
    )
    width_ratio_norm = (width_ratio / 2.0).clip(lower=0.0, upper=1.0)
    trend_gap_norm = (feature_df["fast_slow_gap_pct"].abs() / 1.0).clip(lower=0.0, upper=1.0)
    feature_df["range_regime_score"] = (
        ((1.0 - adx_norm) * 0.45)
        + ((1.0 - slope_norm) * 0.35)
        + ((1.0 - width_ratio_norm) * 0.20)
    ).clip(lower=0.0, upper=1.0)
    feature_df["trend_regime_score"] = (
        (adx_norm * 0.45)
        + (slope_norm * 0.35)
        + (trend_gap_norm * 0.20)
    ).clip(lower=0.0, upper=1.0)
    return feature_df


def assign_target_labels(
    feature_df: pd.DataFrame,
    *,
    horizon_candles: int = 8,
    target_pct: float = 0.45,
    risk_buffer_pct: float = 0.30,
) -> pd.DataFrame:
    labeled = feature_df.copy()
    future_highs = pd.concat([labeled["high"].shift(-offset) for offset in range(1, horizon_candles + 1)], axis=1)
    future_lows = pd.concat([labeled["low"].shift(-offset) for offset in range(1, horizon_candles + 1)], axis=1)
    entry_price = labeled["close"].replace(0, pd.NA)

    labeled["future_up_pct"] = ((future_highs.max(axis=1) / entry_price) - 1.0) * 100
    labeled["future_down_pct"] = ((future_lows.min(axis=1) / entry_price) - 1.0) * 100

    long_mask = (labeled["future_up_pct"] >= target_pct) & (labeled["future_down_pct"] > (-risk_buffer_pct))
    short_mask = (labeled["future_down_pct"] <= (-target_pct)) & (labeled["future_up_pct"] < risk_buffer_pct)
    conflict_mask = long_mask & short_mask

    labeled["label_name"] = "hold"
    labeled.loc[long_mask & ~short_mask, "label_name"] = "long"
    labeled.loc[short_mask & ~long_mask, "label_name"] = "short"

    long_advantage = labeled["future_up_pct"] - labeled["future_down_pct"].abs()
    short_advantage = labeled["future_down_pct"].abs() - labeled["future_up_pct"]
    labeled.loc[conflict_mask & (long_advantage >= short_advantage), "label_name"] = "long"
    labeled.loc[conflict_mask & (short_advantage > long_advantage), "label_name"] = "short"
    labeled["label_id"] = labeled["label_name"].map(LABEL_TO_ID)
    return labeled


def _finalize_simulated_trade(
    *,
    position: dict,
    exit_price: float,
    exit_timestamp,
    reason: str,
    realized_partial_pct: float,
    fee_pct: float,
    slippage_pct: float,
) -> dict[str, float | str | bool]:
    side = str(position.get("side") or "")
    entry_price = float(position["entry_price"])
    gross_pct = calculate_trade_pct(side, entry_price, float(exit_price))
    if bool(position.get("partial_taken", False)):
        gross_pct = gross_pct * 0.5 + float(realized_partial_pct)
    net_pct = gross_pct - (float(fee_pct) * 2.0) - float(slippage_pct)
    return {
        "side": side,
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "entry_timestamp": position.get("entry_timestamp"),
        "exit_timestamp": exit_timestamp,
        "net_pct": float(net_pct),
        "gross_pct": float(gross_pct),
        "reason": str(reason or "forced_close"),
        "partial_taken": bool(position.get("partial_taken", False)),
        "break_even_active": bool(position.get("break_even_active", False)),
        "best_price": float(position.get("best_price") or entry_price),
        "mfe_pct": float(position.get("mfe_pct", 0.0) or 0.0),
        "mae_pct": float(position.get("mae_pct", 0.0) or 0.0),
    }


def _simulate_trade_outcome(
    labeled: pd.DataFrame,
    *,
    signal_index: int,
    signal: str,
    max_holding_candles: int,
    fee_pct: float,
    slippage_pct: float,
) -> dict[str, float | str | bool] | None:
    entry_index = int(signal_index) + 1
    if entry_index >= len(labeled):
        return None

    signal_row = labeled.iloc[int(signal_index)]
    entry_row = labeled.iloc[entry_index]
    entry_price = float(entry_row.get("open") or 0.0)
    if entry_price <= 0:
        return None

    atr_value = float(signal_row.get("atr") or 0.0)
    position = create_position(
        signal,
        entry_price,
        entry_row["timestamp"],
        atr=atr_value,
    )
    realized_partial_pct = 0.0
    final_index = min(len(labeled) - 1, entry_index + max(int(max_holding_candles), 1) - 1)

    for candle_index in range(entry_index, final_index + 1):
        candle = labeled.iloc[candle_index]
        management = evaluate_managed_position_on_candle(
            position=position,
            candle=candle,
            realized_partial_pct=realized_partial_pct,
        )
        if management["action"] == "close":
            closed_position = dict(management["closed_position"])
            position_before_close = dict(management["position_before_close"])
            gross_pct = float(closed_position.get("gross_pct", 0.0) or 0.0)
            if bool(position_before_close.get("partial_taken", False)):
                gross_pct = gross_pct * 0.5 + float(management["realized_partial_pct"] or 0.0)
            net_pct = gross_pct - (float(fee_pct) * 2.0) - float(slippage_pct)
            return {
                "side": str(position_before_close.get("side") or ""),
                "entry_price": float(position_before_close["entry_price"]),
                "exit_price": float(closed_position["exit_price"]),
                "entry_timestamp": position_before_close.get("entry_timestamp"),
                "exit_timestamp": closed_position.get("exit_timestamp"),
                "net_pct": float(net_pct),
                "gross_pct": float(gross_pct),
                "reason": str(closed_position.get("reason") or ""),
                "partial_taken": bool(position_before_close.get("partial_taken", False)),
                "break_even_active": bool(position_before_close.get("break_even_active", False)),
                "best_price": float(position_before_close.get("best_price") or position_before_close["entry_price"]),
                "mfe_pct": float(position_before_close.get("mfe_pct", 0.0) or 0.0),
                "mae_pct": float(position_before_close.get("mae_pct", 0.0) or 0.0),
                "bars_held": int(candle_index - entry_index + 1),
            }
        position = dict(management["position"])
        realized_partial_pct = float(management["realized_partial_pct"] or 0.0)

    exit_row = labeled.iloc[final_index]
    forced = _finalize_simulated_trade(
        position=position,
        exit_price=float(exit_row["close"]),
        exit_timestamp=exit_row["timestamp"],
        reason="forced_horizon_close",
        realized_partial_pct=realized_partial_pct,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )
    forced["bars_held"] = int(final_index - entry_index + 1)
    return forced


def assign_trade_outcome_labels(
    feature_df: pd.DataFrame,
    *,
    max_holding_candles: int = 24,
    min_trade_net_pct: float = 0.12,
    decision_edge_pct: float = 0.08,
    sample_stride: int = 1,
    fee_pct: float | None = None,
    slippage_pct: float | None = None,
) -> pd.DataFrame:
    labeled = feature_df.copy()
    resolved_fee_pct = float(config.FEE_PCT if fee_pct is None else fee_pct)
    resolved_slippage_pct = float(config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct)
    resolved_stride = max(int(sample_stride), 1)

    selected_indexes = list(range(0, len(labeled), resolved_stride))
    long_results: list[dict[str, float | str | bool] | None] = []
    short_results: list[dict[str, float | str | bool] | None] = []
    label_names: list[str] = []

    for signal_index in selected_indexes:
        long_result = _simulate_trade_outcome(
            labeled,
            signal_index=signal_index,
            signal="buy",
            max_holding_candles=max_holding_candles,
            fee_pct=resolved_fee_pct,
            slippage_pct=resolved_slippage_pct,
        )
        short_result = _simulate_trade_outcome(
            labeled,
            signal_index=signal_index,
            signal="sell",
            max_holding_candles=max_holding_candles,
            fee_pct=resolved_fee_pct,
            slippage_pct=resolved_slippage_pct,
        )
        long_results.append(long_result)
        short_results.append(short_result)

        long_net = float((long_result or {}).get("net_pct", -999.0) or -999.0)
        short_net = float((short_result or {}).get("net_pct", -999.0) or -999.0)

        label_name = "hold"
        if long_net >= float(min_trade_net_pct) and long_net >= short_net + float(decision_edge_pct):
            label_name = "long"
        elif short_net >= float(min_trade_net_pct) and short_net >= long_net + float(decision_edge_pct):
            label_name = "short"
        elif long_net >= float(min_trade_net_pct) and short_net < float(min_trade_net_pct):
            label_name = "long"
        elif short_net >= float(min_trade_net_pct) and long_net < float(min_trade_net_pct):
            label_name = "short"
        label_names.append(label_name)

    labeled = labeled.iloc[selected_indexes].copy().reset_index(drop=True)
    labeled["long_trade_net_pct"] = [float((result or {}).get("net_pct", np.nan)) for result in long_results]
    labeled["short_trade_net_pct"] = [float((result or {}).get("net_pct", np.nan)) for result in short_results]
    labeled["long_trade_gross_pct"] = [float((result or {}).get("gross_pct", np.nan)) for result in long_results]
    labeled["short_trade_gross_pct"] = [float((result or {}).get("gross_pct", np.nan)) for result in short_results]
    labeled["long_trade_reason"] = [str((result or {}).get("reason", "")) for result in long_results]
    labeled["short_trade_reason"] = [str((result or {}).get("reason", "")) for result in short_results]
    labeled["long_trade_bars_held"] = [float((result or {}).get("bars_held", np.nan)) for result in long_results]
    labeled["short_trade_bars_held"] = [float((result or {}).get("bars_held", np.nan)) for result in short_results]
    labeled["label_name"] = label_names
    labeled["label_id"] = labeled["label_name"].map(LABEL_TO_ID)
    return labeled


def assign_trade_journal_labels(
    feature_df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    min_trade_net_pct: float = 0.12,
    params: StrategyParams | None = None,
) -> pd.DataFrame:
    import backtest  # import tardio para evitar ciclo na carga do modulo

    labeled = feature_df.copy()
    resolved_params = params or StrategyParams()
    original_ai_enabled = bool(getattr(config.ProductionConfig, "ENABLE_AI_ASSISTANT", True))
    original_ai_mode = str(getattr(config.ProductionConfig, "AI_ASSIST_MODE", "hybrid") or "hybrid")
    try:
        config.ProductionConfig.ENABLE_AI_ASSISTANT = False
        config.ProductionConfig.AI_ASSIST_MODE = "hybrid"
        trades, summary = backtest.run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            candles=len(labeled),
            fee_pct=config.FEE_PCT,
            testnet=False,
            use_local_csv=True,
            slippage_pct=config.SLIPPAGE_PCT,
            preloaded_df=labeled.copy(),
            execution_profile="managed",
            precomputed_indicators=True,
            verbose=False,
            save_report=False,
            strategy_params=resolved_params,
        )
    finally:
        config.ProductionConfig.ENABLE_AI_ASSISTANT = original_ai_enabled
        config.ProductionConfig.AI_ASSIST_MODE = original_ai_mode

    timestamp_to_trade: dict[str, dict[str, Any]] = {}
    for trade in trades:
        signal_timestamp = trade.get("signal_timestamp")
        if not signal_timestamp:
            continue
        try:
            key = pd.Timestamp(signal_timestamp).isoformat()
        except Exception:
            key = str(signal_timestamp)
        existing = timestamp_to_trade.get(key)
        if existing is None or float(trade.get("net_pct", 0.0) or 0.0) > float(existing.get("net_pct", 0.0) or 0.0):
            timestamp_to_trade[key] = dict(trade)

    label_names: list[str] = []
    trade_net_pcts: list[float] = []
    trade_reasons: list[str] = []
    trade_sides: list[str] = []
    trade_win_flags: list[float] = []

    for timestamp_value in labeled["timestamp"]:
        try:
            key = pd.Timestamp(timestamp_value).isoformat()
        except Exception:
            key = str(timestamp_value)
        matched_trade = timestamp_to_trade.get(key)
        net_pct = float((matched_trade or {}).get("net_pct", np.nan))
        reason = str((matched_trade or {}).get("reason", ""))
        side = str((matched_trade or {}).get("side", ""))
        if matched_trade is not None and net_pct >= float(min_trade_net_pct):
            label_name = "long" if side == "long" else "short"
        else:
            label_name = "hold"
        label_names.append(label_name)
        trade_net_pcts.append(net_pct)
        trade_reasons.append(reason)
        trade_sides.append(side)
        trade_win_flags.append(1.0 if matched_trade is not None and net_pct > 0 else 0.0)

    labeled["journal_trade_net_pct"] = trade_net_pcts
    labeled["journal_trade_reason"] = trade_reasons
    labeled["journal_trade_side"] = trade_sides
    labeled["journal_trade_win_flag"] = trade_win_flags
    labeled["label_name"] = label_names
    labeled["label_id"] = labeled["label_name"].map(LABEL_TO_ID)
    labeled.attrs["journal_summary"] = {
        "trades": int(summary.get("trades", 0) or 0),
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "profit_factor": float(summary.get("profit_factor", 0.0) or 0.0),
        "avg_trade_pct": float(summary.get("avg_trade_pct", 0.0) or 0.0),
    }
    return labeled


def build_supervised_dataset_from_frame(
    df: pd.DataFrame,
    *,
    label_mode: str = "trade_journal",
    horizon_candles: int = 8,
    target_pct: float = 0.45,
    risk_buffer_pct: float = 0.30,
    max_holding_candles: int = 24,
    min_trade_net_pct: float = 0.12,
    decision_edge_pct: float = 0.08,
    sample_stride: int = 1,
    params: StrategyParams | None = None,
    source_symbol: str = "",
    timeframe: str = "",
) -> dict[str, Any]:
    feature_df = prepare_feature_frame(df, params=params)
    resolved_symbol = str(source_symbol or "").strip().upper()
    feature_df["source_symbol"] = resolved_symbol
    feature_df["source_timeframe"] = str(timeframe or "").strip()
    feature_df["source_is_btc"] = 1.0 if resolved_symbol.startswith("BTC/") else 0.0
    feature_df["source_is_xlm"] = 1.0 if resolved_symbol.startswith("XLM/") else 0.0
    resolved_label_mode = str(label_mode or "trade_outcome").strip().lower()
    if resolved_label_mode not in LABEL_MODES:
        raise ValueError(f"Modo de rotulo invalido: {resolved_label_mode}")
    if resolved_label_mode == "trade_outcome":
        labeled_df = assign_trade_outcome_labels(
            feature_df,
            max_holding_candles=max_holding_candles,
            min_trade_net_pct=min_trade_net_pct,
            decision_edge_pct=decision_edge_pct,
            sample_stride=sample_stride,
        )
    elif resolved_label_mode == "trade_journal":
        if not resolved_symbol or not str(timeframe or "").strip():
            raise ValueError("trade_journal exige source_symbol e timeframe para reconstruir o diario de trades.")
        labeled_df = assign_trade_journal_labels(
            feature_df,
            symbol=resolved_symbol,
            timeframe=str(timeframe or "").strip(),
            min_trade_net_pct=min_trade_net_pct,
            params=params,
        )
    else:
        labeled_df = assign_target_labels(
            feature_df,
            horizon_candles=horizon_candles,
            target_pct=target_pct,
            risk_buffer_pct=risk_buffer_pct,
        )

    required_columns = FEATURE_COLUMNS + ["label_id", "label_name", "timestamp", "close"]
    if resolved_label_mode == "target_window":
        required_columns += ["future_up_pct", "future_down_pct"]
    elif resolved_label_mode == "trade_journal":
        required_columns += ["journal_trade_win_flag"]
    else:
        required_columns += ["long_trade_net_pct", "short_trade_net_pct"]
    cleaned_df = labeled_df.dropna(subset=required_columns).reset_index(drop=True)
    if cleaned_df.empty:
        raise ValueError("Nao ha linhas suficientes para montar o dataset supervisionado.")

    features = cleaned_df[FEATURE_COLUMNS].astype("float32").to_numpy()
    labels = cleaned_df["label_id"].astype("int32").to_numpy()
    timestamps = cleaned_df["timestamp"].astype(str).to_numpy()
    symbols = cleaned_df["source_symbol"].astype(str).to_numpy(dtype="U32")
    timeframes = cleaned_df["source_timeframe"].astype(str).to_numpy(dtype="U8")

    label_distribution = {
        label: int((cleaned_df["label_name"] == label).sum())
        for label in LABEL_NAMES
    }

    return {
        "features": features,
        "labels": labels,
        "feature_names": np.array(FEATURE_COLUMNS),
        "label_names": np.array(LABEL_NAMES),
        "timestamps": timestamps,
        "symbols": symbols,
        "timeframes": timeframes,
        "metadata": {
            "rows": int(len(cleaned_df)),
            "label_mode": resolved_label_mode,
            "horizon_candles": int(horizon_candles),
            "target_pct": float(target_pct),
            "risk_buffer_pct": float(risk_buffer_pct),
            "max_holding_candles": int(max_holding_candles),
            "min_trade_net_pct": float(min_trade_net_pct),
            "decision_edge_pct": float(decision_edge_pct),
            "sample_stride": int(sample_stride),
            "label_distribution": label_distribution,
            "source_start": str(cleaned_df.iloc[0]["timestamp"]),
            "source_end": str(cleaned_df.iloc[-1]["timestamp"]),
            "journal_summary": dict(labeled_df.attrs.get("journal_summary") or {}),
        },
        "frame": cleaned_df,
    }


def build_supervised_dataset(
    symbol: str,
    timeframe: str,
    *,
    total_limit: int | None = 20000,
    use_local_csv: bool = True,
    testnet: bool = False,
    label_mode: str = "trade_journal",
    horizon_candles: int = 8,
    target_pct: float = 0.45,
    risk_buffer_pct: float = 0.30,
    max_holding_candles: int = 24,
    min_trade_net_pct: float = 0.12,
    decision_edge_pct: float = 0.08,
    sample_stride: int = 1,
) -> dict[str, Any]:
    market_df = load_market_frame(
        symbol,
        timeframe,
        total_limit=total_limit,
        use_local_csv=use_local_csv,
        testnet=testnet,
    )
    dataset = build_supervised_dataset_from_frame(
        market_df,
        label_mode=label_mode,
        horizon_candles=horizon_candles,
        target_pct=target_pct,
        risk_buffer_pct=risk_buffer_pct,
        max_holding_candles=max_holding_candles,
        min_trade_net_pct=min_trade_net_pct,
        decision_edge_pct=decision_edge_pct,
        sample_stride=sample_stride,
        source_symbol=symbol,
        timeframe=timeframe,
    )
    dataset["metadata"]["symbol"] = symbol
    dataset["metadata"]["timeframe"] = timeframe
    dataset["metadata"]["total_limit"] = int(total_limit or 0)
    return dataset


def build_multi_symbol_dataset(
    sources: list[dict[str, Any]],
    *,
    label_mode: str = "trade_journal",
    horizon_candles: int = 8,
    target_pct: float = 0.45,
    risk_buffer_pct: float = 0.30,
    max_holding_candles: int = 24,
    min_trade_net_pct: float = 0.12,
    decision_edge_pct: float = 0.08,
    sample_stride: int = 1,
) -> dict[str, Any]:
    frames = []
    source_meta = []
    for source in sources:
        symbol = str(source["symbol"])
        timeframe = str(source.get("timeframe") or "15m")
        total_limit = source.get("total_limit")
        use_local_csv = bool(source.get("use_local_csv", True))
        testnet = bool(source.get("testnet", False))

        market_df = load_market_frame(
            symbol,
            timeframe,
            total_limit=total_limit,
            use_local_csv=use_local_csv,
            testnet=testnet,
        )
        dataset = build_supervised_dataset_from_frame(
            market_df,
            label_mode=label_mode,
            horizon_candles=horizon_candles,
            target_pct=target_pct,
            risk_buffer_pct=risk_buffer_pct,
            max_holding_candles=max_holding_candles,
            min_trade_net_pct=min_trade_net_pct,
            decision_edge_pct=decision_edge_pct,
            sample_stride=sample_stride,
            source_symbol=symbol,
            timeframe=timeframe,
        )
        frames.append(dataset["frame"])
        source_meta.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "rows": int(dataset["metadata"]["rows"]),
                "source_start": dataset["metadata"]["source_start"],
                "source_end": dataset["metadata"]["source_end"],
            }
        )

    if not frames:
        raise ValueError("Nenhuma fonte foi fornecida para o dataset multiativo.")

    merged_frame = pd.concat(frames, ignore_index=True)
    merged_features = merged_frame[FEATURE_COLUMNS].astype("float32").to_numpy()
    merged_labels = merged_frame["label_id"].astype("int32").to_numpy()
    merged_timestamps = merged_frame["timestamp"].astype(str).to_numpy()
    merged_symbols = merged_frame["source_symbol"].astype(str).to_numpy(dtype="U32")
    merged_timeframes = merged_frame["source_timeframe"].astype(str).to_numpy(dtype="U8")

    label_distribution = {
        label: int((merged_frame["label_name"] == label).sum())
        for label in LABEL_NAMES
    }

    return {
        "features": merged_features,
        "labels": merged_labels,
        "feature_names": np.array(FEATURE_COLUMNS),
        "label_names": np.array(LABEL_NAMES),
        "timestamps": merged_timestamps,
        "symbols": merged_symbols,
        "timeframes": merged_timeframes,
        "metadata": {
            "rows": int(len(merged_frame)),
            "label_mode": str(label_mode or "trade_outcome").strip().lower(),
            "horizon_candles": int(horizon_candles),
            "target_pct": float(target_pct),
            "risk_buffer_pct": float(risk_buffer_pct),
            "max_holding_candles": int(max_holding_candles),
            "min_trade_net_pct": float(min_trade_net_pct),
            "decision_edge_pct": float(decision_edge_pct),
            "sample_stride": int(sample_stride),
            "label_distribution": label_distribution,
            "sources": source_meta,
        },
        "frame": merged_frame.reset_index(drop=True),
    }


def save_dataset(dataset: dict[str, Any], output_path: str | Path) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = dict(dataset["metadata"])
    metadata["feature_names"] = FEATURE_COLUMNS
    metadata["label_names"] = LABEL_NAMES

    np.savez_compressed(
        destination,
        features=dataset["features"],
        labels=dataset["labels"],
        feature_names=np.asarray(dataset["feature_names"], dtype="U64"),
        label_names=np.asarray(dataset["label_names"], dtype="U16"),
        timestamps=dataset["timestamps"],
        symbols=np.asarray(dataset["symbols"], dtype="U32"),
        timeframes=np.asarray(dataset["timeframes"], dtype="U8"),
        metadata_json=json.dumps(metadata, ensure_ascii=True),
    )
    return destination


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a supervised dataset for TensorFlow Lite experiments.")
    parser.add_argument("--symbol", default="XLM/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--label-mode", default="trade_journal", choices=sorted(LABEL_MODES))
    parser.add_argument("--total-limit", type=int, default=20000)
    parser.add_argument("--horizon-candles", type=int, default=8)
    parser.add_argument("--target-pct", type=float, default=0.45)
    parser.add_argument("--risk-buffer-pct", type=float, default=0.30)
    parser.add_argument("--max-holding-candles", type=int, default=24)
    parser.add_argument("--min-trade-net-pct", type=float, default=0.12)
    parser.add_argument("--decision-edge-pct", type=float, default=0.08)
    parser.add_argument("--sample-stride", type=int, default=1)
    parser.add_argument("--output", required=True)
    parser.add_argument("--use-exchange", action="store_true")
    parser.add_argument("--testnet", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if str(args.symbols or "").strip():
        symbols = [item.strip() for item in str(args.symbols).split(",") if item.strip()]
        dataset = build_multi_symbol_dataset(
            [
                {
                    "symbol": symbol,
                    "timeframe": args.timeframe,
                    "total_limit": args.total_limit,
                    "use_local_csv": not args.use_exchange,
                    "testnet": args.testnet,
                }
                for symbol in symbols
            ],
            label_mode=args.label_mode,
            horizon_candles=args.horizon_candles,
            target_pct=args.target_pct,
            risk_buffer_pct=args.risk_buffer_pct,
            max_holding_candles=args.max_holding_candles,
            min_trade_net_pct=args.min_trade_net_pct,
            decision_edge_pct=args.decision_edge_pct,
            sample_stride=args.sample_stride,
        )
        dataset["metadata"]["symbols"] = symbols
        dataset["metadata"]["timeframe"] = args.timeframe
        dataset["metadata"]["total_limit"] = int(args.total_limit)
    else:
        dataset = build_supervised_dataset(
            args.symbol,
            args.timeframe,
            total_limit=args.total_limit,
            use_local_csv=not args.use_exchange,
            testnet=args.testnet,
            label_mode=args.label_mode,
            horizon_candles=args.horizon_candles,
            target_pct=args.target_pct,
            risk_buffer_pct=args.risk_buffer_pct,
            max_holding_candles=args.max_holding_candles,
            min_trade_net_pct=args.min_trade_net_pct,
            decision_edge_pct=args.decision_edge_pct,
            sample_stride=args.sample_stride,
        )
    saved_path = save_dataset(dataset, args.output)
    summary = {
        "output": str(saved_path),
        **dataset["metadata"],
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
