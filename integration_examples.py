"""Exemplos de integração com seu TradingBot atual."""
from __future__ import annotations

from .strategy import analyze_prepared_candle, prepare_candle_features


def generate_signal_from_dataframe(df):
    prepared = prepare_candle_features(df)
    result = analyze_prepared_candle(prepared, index=-1)
    return result
