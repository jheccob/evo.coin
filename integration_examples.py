"""Exemplos de integração com seu TradingBot atual."""
from __future__ import annotations

from .strategy_engine import analyze_prepared_candle, prepare_candle_features
from .strategy_params import DEFAULT_PARAMS



def generate_signal_from_dataframe(df):
    prepared = prepare_candle_features(df, params=DEFAULT_PARAMS)
    result = analyze_prepared_candle(prepared, index=-1, params=DEFAULT_PARAMS)
    return result
