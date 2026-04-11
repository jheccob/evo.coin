from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"

    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 200
    rsi_period: int = 14
    atr_period: int = 14

    long_rsi_floor: float = 52.0
    short_rsi_ceiling: float = 48.0
    long_rsi_pullback_floor: float = 45.0
    short_rsi_pullback_ceiling: float = 55.0

    min_atr_pct_long: float = 0.30
    min_atr_pct_short: float = 0.30
    max_stretch_pct: float = 1.20
    pullback_tolerance_pct: float = 0.30

    trend_gap_pct: float = 0.15
    fast_slow_gap_pct: float = 0.05
    slope_lookback: int = 5

    atr_stop_mult: float = 1.5
    atr_target_mult: float = 2.8
    atr_trailing_mult: float = 2.0
    break_even_trigger_r: float = 1.0
    partial_tp_r: float = 1.2
    partial_close_pct: float = 0.50

    fee_pct_round_turn: float = 0.08
    slippage_pct_round_turn: float = 0.04

    risk_per_trade_pct: float = 1.0
    max_bars_in_trade: int = 48


DEFAULT_PARAMS = StrategyParams()
