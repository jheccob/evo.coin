from __future__ import annotations

import pandas as pd

from strategy import prepare_candle_features


class TechnicalIndicators:
    """Camada de compatibilidade para o runtime atual."""

    def calculate_all(self, df: pd.DataFrame) -> pd.DataFrame:
        return prepare_candle_features(df)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.calculate_all(df)
