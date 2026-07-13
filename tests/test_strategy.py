import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import backtest
import bot_runner
import config
import strategy_engine
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal
from position_manager import (
    create_native_bracket_position,
    create_position,
    evaluate_managed_position_on_candle,
    evaluate_open_position,
)
from services.risk_management_service import RiskManagementService


def _ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "timestamp" in out.columns:
        if pd.api.types.is_numeric_dtype(out["timestamp"]):
            out["timestamp"] = pd.to_datetime(out["timestamp"], unit="ms", utc=True, errors="coerce")
        else:
            out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        out = out.set_index("timestamp")
    else:
        out.index = pd.to_datetime(out.index, utc=True, errors="coerce")
    return out.sort_index()


def _build_strategy_params(
    buy_rsi_threshold=None,
    sell_rsi_threshold=None,
) -> StrategyParams:
    return StrategyParams(
        buy_rsi_floor=float(
            config.BUY_RSI_SIGNAL if buy_rsi_threshold is None else buy_rsi_threshold
        ),
        sell_rsi_ceiling=float(
            config.SELL_RSI_SIGNAL if sell_rsi_threshold is None else sell_rsi_threshold
        ),
    )


def prepare_candle_features(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_datetime_index(df)
    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close", "volume"])

    prepared = calculate_indicators(out, _build_strategy_params())
    if "is_closed" not in prepared.columns:
        prepared["is_closed"] = True
    prepared["is_closed"] = prepared["is_closed"].fillna(True).astype(bool)
    return prepared


def analyze_prepared_candle(
    df: pd.DataFrame,
    index: int = -1,
    buy_rsi_threshold=None,
    sell_rsi_threshold=None,
):
    if df is None or df.empty:
        return {"signal": "hold", "reason": "dados insuficientes"}

    resolved_df = df.copy()
    if index != -1:
        effective_index = len(resolved_df) + index if index < 0 else index
        effective_index = max(min(effective_index, len(resolved_df) - 1), 0)
        resolved_df = resolved_df.iloc[: effective_index + 1].copy()
    if len(resolved_df) < 3:
        return {"signal": "hold", "reason": "dados insuficientes"}

    required_columns = {"ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "atr_pct"}
    if not required_columns.issubset(set(resolved_df.columns)):
        ohlcv_columns = {"open", "high", "low", "close", "volume"}
        if not ohlcv_columns.issubset(set(resolved_df.columns)):
            return {"signal": "hold", "reason": "dados insuficientes"}
        resolved_df = prepare_candle_features(resolved_df)

    result = generate_entry_signal(
        resolved_df,
        _build_strategy_params(
            buy_rsi_threshold=buy_rsi_threshold,
            sell_rsi_threshold=sell_rsi_threshold,
        ),
        index=-1,
    )
    return {
        "signal": str(result.get("signal") or "hold"),
        "reason": str(result.get("reason") or "sem gatilho"),
    }


class StrategyTests(unittest.TestCase):
    @staticmethod
    def _build_trend_df(start_price: float, step: float, length: int = 80) -> pd.DataFrame:
        rows = []
        offset_cycle = [0.0, 0.7, 1.2, 1.6, -0.8, -1.4]
        for i in range(length):
            close_price = start_price + (step * i) + (offset_cycle[i % len(offset_cycle)] * abs(step))
            candle_range = max(abs(step) * 1.2, 1.2)
            rows.append(
                {
                    "timestamp": i,
                    "open": close_price - (step * 0.25),
                    "high": close_price + candle_range,
                    "low": close_price - candle_range,
                    "close": close_price,
                    "volume": 1000 + (i * 10),
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _build_buy_signal_df() -> pd.DataFrame:
        df = StrategyTests._build_trend_df(start_price=100.0, step=0.8, length=260)
        tail_rows = [
            (238, 290.6979787890897, 291.5835340814979, 290.0695769354732, 290.8426287389139, 8480),
            (239, 291.3220383581431, 292.55054505431207, 290.8909359134129, 291.4072087333261, 7809),
            (240, 291.3707128846601, 292.22491908733457, 290.7522060753569, 291.21723036216576, 9421),
            (241, 291.98442472740516, 293.13887516685554, 291.27017239928455, 291.7982943858072, 10230),
            (242, 291.58969535855437, 292.13902895980874, 290.5871537162588, 291.3794110627988, 8691),
            (243, 291.4235322658642, 292.5894563295704, 290.13437461910473, 291.35700008179793, 9196),
            (244, 291.3567749573504, 291.62266392171114, 290.1722687965063, 291.3439627768589, 7301),
            (245, 291.15061655757984, 292.10203229432057, 289.9160170938345, 290.9465618276466, 9806),
            (246, 290.44396564877167, 291.56570310522824, 289.9859190626439, 290.44781892889245, 8578),
            (247, 290.45961629423124, 291.89198998562944, 290.02506368108243, 290.7218257296745, 7830),
            (248, 291.07410488874365, 291.3904311538181, 290.535501710549, 291.1053231458823, 9328),
            (249, 291.696664008779, 292.2220089010529, 291.18870335014003, 291.70309194007706, 7760),
            (250, 291.99395706051314, 292.7655524813439, 290.5855797091018, 291.80527694253834, 9720),
            (251, 291.77982441821433, 292.20417263799294, 290.7356455512634, 291.90723005968823, 8549),
            (252, 291.91464387671124, 292.4408135651924, 291.5170925501343, 292.0404461752696, 9761),
            (253, 292.47590961500424, 292.8929649326294, 291.55785061387235, 292.61987588981765, 7786),
            (254, 293.3383519072904, 294.42963640966366, 291.93409708937946, 293.0843232977227, 9468),
            (255, 293.5388906604901, 294.29386596904146, 292.58414138728665, 293.3361715217548, 8219),
            (256, 294.0076294639158, 294.8316122228920, 292.5657981540659, 293.7706060834598, 9630),
            (257, 293.56893121299686, 294.4516949848274, 292.62812453684523, 293.57333798110915, 8720),
            (258, 293.2316181311499, 293.94625391640795, 292.29728111483604, 293.16394826812547, 8037),
            (259, 293.9689078932189, 294.1077151851788, 293.5356706975650, 294.0813163841296, 12698),
        ]
        for idx, open_price, high_price, low_price, close_price, volume in tail_rows:
            df.loc[idx, "open"] = open_price
            df.loc[idx, "high"] = high_price
            df.loc[idx, "low"] = low_price
            df.loc[idx, "close"] = close_price
            df.loc[idx, "volume"] = volume
        return df

    @staticmethod
    def _build_sell_signal_df() -> pd.DataFrame:
        df = StrategyTests._build_trend_df(start_price=620.0, step=-0.8, length=260)
        tail_rows = [
            (240, 427.39359698316366, 428.11554612656465, 426.8006127520131, 427.50952640625275, 9611),
            (241, 427.77406227413, 428.50719567465853, 426.8988230148689, 427.89237667632653, 9612),
            (242, 427.9359274977398, 428.6538555805382, 426.7330872398614, 428.18070114383323, 8887),
            (243, 427.69098428404106, 427.9789726694399, 427.3670453041922, 427.6602614310918, 9845),
            (244, 427.2216073346315, 427.86726847575477, 426.2894710665398, 427.07117009078763, 7875),
            (245, 426.74884553861347, 427.3559225347024, 425.75032908076895, 426.5821979935366, 9969),
            (246, 426.3384897025370, 427.76048401068596, 424.9534386864035, 426.1727643127454, 8555),
            (247, 426.0345438715991, 427.27017153032733, 424.9621604844668, 425.9089494768809, 7764),
            (248, 426.07709254256224, 427.62175019964036, 425.83768688126264, 426.29208415434624, 8542),
            (249, 426.66933352617076, 427.7033531934287, 425.7091509384260, 426.59521697964186, 10073),
            (250, 426.1005503746130, 427.0880798162058, 425.8403017959776, 426.2211945471952, 9985),
            (251, 426.05988704179987, 427.10616206844196, 425.13899122607035, 425.8932211223868, 9012),
            (252, 425.9222446381096, 427.17847283776433, 425.1349810806231, 425.8974180469186, 8872),
            (253, 426.0202487129787, 427.0200660220899, 424.83626301744823, 426.2173381499659, 8959),
            (254, 426.4026279357165, 427.74950166205446, 425.4829143527605, 426.6912813508794, 9685),
            (255, 426.08439104904295, 427.45741077086086, 425.3908301898655, 426.22892326769346, 8897),
            (256, 425.57186324663434, 426.62130779404100, 425.1515359545888, 425.8183149095613, 8555),
            (257, 425.85860824647875, 426.60244322503050, 424.71752394988937, 425.76930449107425, 10252),
            (258, 425.30539905422506, 426.46992991338850, 423.9185871900256, 425.4176081262111, 10177),
            (259, 423.81275808433287, 425.15138832419615, 423.58732951287374, 423.72430104350457, 13127),
        ]
        for idx, open_price, high_price, low_price, close_price, volume in tail_rows:
            df.loc[idx, "open"] = open_price
            df.loc[idx, "high"] = high_price
            df.loc[idx, "low"] = low_price
            df.loc[idx, "close"] = close_price
            df.loc[idx, "volume"] = volume
        return df

    @staticmethod
    def _build_reversal_rebound_df(final_volume: float = 2500.0, final_adx: float = 38.0) -> pd.DataFrame:
        rows = []
        for i in range(260):
            close_price = 103.0 - (i * 0.012)
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-07-06 00:00:00+00:00") + pd.Timedelta(minutes=15 * i),
                    "open": close_price + 0.05,
                    "high": close_price + 0.35,
                    "low": close_price - 0.35,
                    "close": close_price,
                    "volume": 1000.0,
                    "ema_fast": 100.5,
                    "ema_slow": 101.0,
                    "ema_trend": 102.0,
                    "rsi": 38.0,
                    "adx": 34.0,
                    "vol_ma": 1000.0,
                    "atr": 0.6,
                    "atr_pct": 0.6,
                    "macd": -0.8,
                    "macd_signal": -0.4,
                    "macd_hist": -0.4,
                    "is_closed": True,
                }
            )
        tail_rows = {
            252: (99.6, 100.4, 98.8, 99.0, 1300.0, 34.0, -0.52),
            253: (99.0, 99.4, 97.4, 97.8, 1600.0, 30.0, -0.62),
            254: (97.8, 98.0, 96.0, 96.4, 2100.0, 26.0, -0.78),
            255: (96.4, 97.1, 96.1, 96.8, 1800.0, 33.0, -0.70),
            256: (96.8, 97.9, 96.6, 97.5, 1900.0, 40.0, -0.58),
            257: (97.5, 98.8, 97.2, 98.4, 2100.0, 48.0, -0.44),
            258: (98.4, 99.2, 98.0, 98.8, 2200.0, 54.0, -0.34),
            259: (98.8, 100.0, 98.8, 99.6, final_volume, 62.0, -0.18),
        }
        for idx, (open_price, high_price, low_price, close_price, volume, rsi, macd_hist) in tail_rows.items():
            rows[idx].update(
                {
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                    "rsi": rsi,
                    "adx": final_adx if idx == 259 else rows[idx]["adx"],
                    "macd_hist": macd_hist,
                }
            )
        return pd.DataFrame(rows).set_index("timestamp")

    @staticmethod
    def _build_short_reversal_rejection_df(final_volume: float = 2500.0, final_rsi: float = 48.0, final_adx: float = 38.0) -> pd.DataFrame:
        rows = []
        for i in range(260):
            close_price = 97.0 + (i * 0.012)
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-07-06 00:00:00+00:00") + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.05,
                    "high": close_price + 0.35,
                    "low": close_price - 0.35,
                    "close": close_price,
                    "volume": 1000.0,
                    "ema_fast": 99.5,
                    "ema_slow": 99.0,
                    "ema_trend": 98.0,
                    "rsi": 62.0,
                    "adx": 34.0,
                    "vol_ma": 1000.0,
                    "atr": 0.6,
                    "atr_pct": 0.6,
                    "macd": 0.8,
                    "macd_signal": 0.4,
                    "macd_hist": 0.4,
                    "is_closed": True,
                }
            )
        tail_rows = {
            252: (100.4, 101.2, 100.0, 101.0, 1300.0, 66.0, 0.52),
            253: (101.0, 102.6, 100.8, 102.2, 1600.0, 70.0, 0.62),
            254: (102.2, 104.0, 102.0, 103.6, 2100.0, 72.0, 0.78),
            255: (103.6, 103.9, 102.9, 103.2, 1800.0, 65.0, 0.70),
            256: (103.2, 103.4, 102.1, 102.5, 1900.0, 58.0, 0.58),
            257: (102.5, 102.8, 101.2, 101.6, 2100.0, 52.0, 0.44),
            258: (101.6, 102.0, 100.8, 101.2, 2200.0, 50.0, 0.34),
            259: (101.2, 101.2, 100.0, 100.4, final_volume, final_rsi, 0.18),
        }
        for idx, (open_price, high_price, low_price, close_price, volume, rsi, macd_hist) in tail_rows.items():
            rows[idx].update(
                {
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                    "rsi": rsi,
                    "adx": final_adx if idx == 259 else rows[idx]["adx"],
                    "macd_hist": macd_hist,
                }
            )
        return pd.DataFrame(rows).set_index("timestamp")

    @staticmethod
    def _build_liquidity_sweep_df(side: str = "long", *, recover: bool = True, extended: bool = False) -> pd.DataFrame:
        rows = []
        for i in range(80):
            base = 102.0 if side == "long" else 98.0
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-07-07 00:00:00+00:00") + pd.Timedelta(minutes=15 * i),
                    "open": base,
                    "high": 105.0 if side == "long" else 100.0,
                    "low": 100.0 if side == "long" else 95.0,
                    "close": base,
                    "volume": 1000.0,
                    "ema_fast": 101.0 if side == "long" else 99.0,
                    "ema_slow": 101.5 if side == "long" else 98.5,
                    "ema_trend": 102.5 if side == "long" else 97.5,
                    "rsi": 38.0 if side == "long" else 62.0,
                    "adx": 36.0,
                    "vol_ma": 1000.0,
                    "atr": 0.8,
                    "atr_pct": 0.8,
                    "macd": -0.3 if side == "long" else 0.3,
                    "macd_signal": -0.2 if side == "long" else 0.2,
                    "macd_hist": -0.20 if side == "long" else 0.20,
                    "is_closed": True,
                }
            )
        if side == "long":
            close = 104.5 if extended else (100.35 if recover else 99.85)
            rows[-2].update({"close": 100.4, "rsi": 39.0, "macd_hist": -0.22})
            rows[-1].update(
                {
                    "open": 100.20,
                    "high": max(100.60, close + 0.15),
                    "low": 99.70,
                    "close": close,
                    "volume": 1800.0,
                    "ema_fast": 100.10,
                    "ema_slow": 101.0,
                    "ema_trend": 102.0,
                    "rsi": 42.0,
                    "macd_hist": -0.10,
                }
            )
        else:
            close = 95.5 if extended else (99.65 if recover else 100.15)
            rows[-2].update({"close": 99.6, "rsi": 61.0, "macd_hist": 0.22})
            rows[-1].update(
                {
                    "open": 99.90,
                    "high": 100.35,
                    "low": min(99.40, close - 0.15),
                    "close": close,
                    "volume": 1800.0,
                    "ema_fast": 99.90,
                    "ema_slow": 99.0,
                    "ema_trend": 98.0,
                    "rsi": 58.0,
                    "macd_hist": 0.10,
                }
            )
        return pd.DataFrame(rows).set_index("timestamp")

    def test_prepare_candle_features_adds_expected_columns(self):
        df = self._build_trend_df(start_price=100.0, step=1.0, length=80)
        features = prepare_candle_features(df)
        expected = {"ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "atr_pct", "is_closed"}
        self.assertTrue(expected.issubset(set(features.columns)))

    def test_generate_entry_signal_catches_strong_long_reversal_rebound(self):
        df = self._build_reversal_rebound_df(final_volume=2500.0)

        with (
            mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["setup"]["setup"], "reversal_rebound_long")
        self.assertIn("reversal_rebound_long_score", result["reason"])

    def test_long_reversal_rebound_requires_volume_confirmation(self):
        df = self._build_reversal_rebound_df(final_volume=900.0)

        with mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", True):
            setup = strategy_engine.detect_setup(df, StrategyParams(), index=-1)

        self.assertNotEqual(setup.get("setup"), "reversal_rebound_long")

    def test_long_reversal_rebound_blocks_high_rsi_without_adx_confirmation(self):
        df = self._build_reversal_rebound_df(final_volume=2500.0, final_adx=24.0)

        with mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", True):
            setup = strategy_engine.detect_setup(df, StrategyParams(), index=-1)

        self.assertNotEqual(setup.get("setup"), "reversal_rebound_long")

    def test_generate_entry_signal_catches_strong_short_reversal_rejection(self):
        df = self._build_short_reversal_rejection_df(final_volume=2500.0)

        with (
            mock.patch.object(config, "ENABLE_SHORT_REVERSAL_REJECTION", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "sell")
        self.assertEqual(result["setup"]["setup"], "reversal_rejection_short")
        self.assertIn("reversal_rejection_short_score", result["reason"])

    def test_short_reversal_rejection_requires_volume_confirmation(self):
        df = self._build_short_reversal_rejection_df(final_volume=900.0)

        with mock.patch.object(config, "ENABLE_SHORT_REVERSAL_REJECTION", True):
            setup = strategy_engine.detect_setup(df, StrategyParams(), index=-1)

        self.assertNotEqual(setup.get("setup"), "reversal_rejection_short")

    def test_short_reversal_rejection_blocks_low_rsi_without_adx_confirmation(self):
        df = self._build_short_reversal_rejection_df(final_volume=2500.0, final_rsi=38.0, final_adx=24.0)

        with mock.patch.object(config, "ENABLE_SHORT_REVERSAL_REJECTION", True):
            setup = strategy_engine.detect_setup(df, StrategyParams(), index=-1)

        self.assertNotEqual(setup.get("setup"), "reversal_rejection_short")

    def test_short_reversal_rejection_blocks_configured_bad_hour(self):
        df = self._build_short_reversal_rejection_df(final_volume=2500.0)
        df = df.copy()
        shifted_index = list(df.index[:-1]) + [pd.Timestamp("2026-07-06 14:45:00+00:00")]
        df.index = pd.DatetimeIndex(shifted_index)

        with (
            mock.patch.object(config, "ENABLE_SHORT_REVERSAL_REJECTION", True),
            mock.patch.object(config, "SHORT_REVERSAL_BLOCKED_ENTRY_HOURS_UTC", [14], create=True),
        ):
            setup = strategy_engine.detect_setup(df, StrategyParams(), index=-1)

        self.assertNotEqual(setup.get("setup"), "reversal_rejection_short")

    def test_detects_liquidity_sweep_reversal_long_with_reclaim(self):
        df = self._build_liquidity_sweep_df("long", recover=True)

        with (
            mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", False),
            mock.patch.object(config, "MARKET_STRUCTURE_GUARD_ENABLED", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["setup"]["setup"], "liquidity_sweep_reversal_long")
        self.assertIn("sweep_detected", result["reason"])

    def test_liquidity_sweep_long_without_reclaim_does_not_buy(self):
        df = self._build_liquidity_sweep_df("long", recover=False)

        with mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", False):
            setup = strategy_engine.detect_setup(df, StrategyParams(), index=-1)

        self.assertNotEqual(setup.get("setup"), "liquidity_sweep_reversal_long")

    def test_liquidity_sweep_long_blocks_late_entry_far_from_low(self):
        df = self._build_liquidity_sweep_df("long", recover=True, extended=True)

        with (
            mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", False),
            mock.patch.object(config, "LIQUIDITY_SWEEP_MAX_BOUNCE_FROM_LOW_PCT", 4.0),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "hold")
        self.assertIn(result["reason"], {"long_entrada_tardia", "long_rr_insuficiente"})

    def test_market_structure_guard_blocks_long_near_resistance_without_volume_breakout(self):
        df = self._build_liquidity_sweep_df("long", recover=True)
        df.iloc[-1, df.columns.get_loc("close")] = 104.90
        df.iloc[-1, df.columns.get_loc("high")] = 105.00
        setup = {"setup": "trend_resume_long", "direction": "long", "regime": {"regime": "trend_bull"}}

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=setup),
            mock.patch.object(config, "MIN_LONG_SCORE", 1),
            mock.patch.object(config, "MARKET_STRUCTURE_GUARD_ENABLED", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "hold")
        self.assertEqual(result["reason"], "long_near_resistance")

    def test_detects_liquidity_sweep_reversal_short_with_rejection(self):
        df = self._build_liquidity_sweep_df("short", recover=True)

        with (
            mock.patch.object(config, "ENABLE_SHORT_REVERSAL_REJECTION", False),
            mock.patch.object(config, "MARKET_STRUCTURE_GUARD_ENABLED", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "sell")
        self.assertEqual(result["setup"]["setup"], "liquidity_sweep_reversal_short")
        self.assertIn("sweep_detected", result["reason"])

    def test_market_structure_guard_blocks_short_near_support_without_breakdown(self):
        df = self._build_liquidity_sweep_df("short", recover=True)
        df.iloc[-1, df.columns.get_loc("close")] = 95.10
        df.iloc[-1, df.columns.get_loc("low")] = 95.00
        setup = {"setup": "trend_resume_short", "direction": "short", "regime": {"regime": "trend_bear"}}

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=setup),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", True),
            mock.patch.object(config, "MARKET_STRUCTURE_GUARD_ENABLED", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "hold")
        self.assertEqual(result["reason"], "short_near_support")

    def test_legacy_reversal_rebound_setup_still_works_with_structure_guard_disabled(self):
        df = self._build_reversal_rebound_df(final_volume=2500.0)

        with (
            mock.patch.object(config, "ENABLE_LONG_REVERSAL_REBOUND", True),
            mock.patch.object(config, "MARKET_STRUCTURE_GUARD_ENABLED", False),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["setup"]["setup"], "reversal_rebound_long")

    def test_analyze_prepared_candle_hold_when_data_is_insufficient(self):
        df = pd.DataFrame(
            [
                {"close": 100.0, "ema_fast": 99.5, "ema_slow": 99.0, "ema_trend": 98.0, "rsi": 53.0},
                {"close": 101.0, "ema_fast": 100.5, "ema_slow": 100.0, "ema_trend": 99.0, "rsi": 54.0},
            ]
        )
        result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "hold")

    def test_analyze_prepared_candle_buy_signal(self):
        df = self._build_buy_signal_df()
        with mock.patch.object(config, "MIN_LONG_SCORE", 4):
            result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "buy")

    def test_generate_entry_signal_matches_slice_and_indexed_dataframe(self):
        df = self._build_buy_signal_df()
        params = StrategyParams()
        featured = calculate_indicators(df, params)

        with mock.patch.object(config, "MIN_LONG_SCORE", 4):
            sliced_signal = generate_entry_signal(featured.iloc[: len(featured)], params)
            indexed_signal = generate_entry_signal(featured, params, index=len(featured) - 1)

        self.assertEqual(sliced_signal["signal"], indexed_signal["signal"])
        self.assertEqual(sliced_signal["reason"], indexed_signal["reason"])

    def test_calculate_indicators_adds_macd_features(self):
        df = self._build_trend_df(start_price=100.0, step=1.0, length=80)
        featured = calculate_indicators(df, StrategyParams())

        self.assertIn("macd", featured.columns)
        self.assertIn("macd_signal", featured.columns)
        self.assertIn("macd_hist", featured.columns)
        self.assertTrue(featured["macd_hist"].notna().any())

    def test_macd_entry_filter_blocks_opposite_direction(self):
        row = pd.Series({"macd": -1.0, "macd_signal": -0.5, "macd_hist": -0.5})

        with mock.patch.object(config, "ENABLE_MACD_ENTRY_FILTER", True), mock.patch.object(
            config, "MACD_ENTRY_FILTER_MODE", "histogram"
        ):
            self.assertFalse(strategy_engine._macd_direction_ok(row, "long"))
            self.assertTrue(strategy_engine._macd_direction_ok(row, "short"))

    def test_volume_ma_entry_filter_requires_volume_above_moving_average(self):
        below_ma = pd.Series({"volume": 990.0, "vol_ma": 1000.0})
        above_ma = pd.Series({"volume": 1001.0, "vol_ma": 1000.0})

        with (
            mock.patch.object(config, "ENABLE_VOLUME_MA_ENTRY_FILTER", True),
            mock.patch.object(config, "VOLUME_MA_ENTRY_MULTIPLIER", 1.0),
        ):
            self.assertFalse(strategy_engine._volume_ma_entry_ok(below_ma))
            self.assertTrue(strategy_engine._volume_ma_entry_ok(above_ma))

    def test_generate_entry_signal_can_bypass_weak_regime_gate(self):
        df = self._build_buy_signal_df()
        params = StrategyParams()
        featured = calculate_indicators(df, params)
        weak_regime_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "weak_bull"},
        }

        with (
            mock.patch("strategy_engine.detect_setup", return_value=weak_regime_setup),
            mock.patch.object(config, "BYPASS_WEAK_REGIME_GATE", True),
            mock.patch.object(config, "MIN_LONG_SCORE", 4),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 0.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.0),
        ):
            result = generate_entry_signal(featured, params)

        self.assertEqual(result["signal"], "buy")

    def test_generate_entry_signal_can_allow_only_weak_bull_atr_entries(self):
        df = self._build_buy_signal_df()
        params = StrategyParams()
        featured = calculate_indicators(df, params)
        weak_regime_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "weak_bull", "regime_detail": "weak_bull_atr"},
        }

        with (
            mock.patch("strategy_engine.detect_setup", return_value=weak_regime_setup),
            mock.patch.object(config, "ALLOW_WEAK_BULL_ATR_LONG_ENTRIES", True),
            mock.patch.object(config, "MIN_LONG_SCORE", 4),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 0.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.0),
        ):
            result = generate_entry_signal(featured, params)

        self.assertEqual(result["signal"], "buy")

    def test_generate_entry_signal_can_use_triggerless_fallback_entry(self):
        df = self._build_buy_signal_df()
        params = StrategyParams()
        featured = calculate_indicators(df, params)
        no_trigger_setup = {
            "setup": None,
            "direction": None,
            "regime": {"regime": "range"},
        }

        with (
            mock.patch("strategy_engine.detect_setup", return_value=no_trigger_setup),
            mock.patch.object(config, "ALLOW_TRIGGERLESS_ENTRIES", True),
            mock.patch.object(config, "BYPASS_WEAK_REGIME_GATE", True),
            mock.patch.object(config, "MIN_LONG_SCORE", 4),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 0.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.0),
        ):
            result = generate_entry_signal(featured, params)

        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["setup"]["direction"], "long")

    def test_pullback_long_rsi_filters_can_block_entry(self):
        df = self._build_buy_signal_df()
        params = StrategyParams()
        featured = calculate_indicators(df, params)
        pullback_setup = {
            "setup": "pullback_long",
            "direction": "long",
            "regime": {"regime": "trend_bull"},
        }

        with (
            mock.patch("strategy_engine.detect_setup", return_value=pullback_setup),
            mock.patch.object(config, "MIN_LONG_SCORE", 4),
            mock.patch.object(config, "PULLBACK_LONG_MIN_ADX", 0.0),
            mock.patch.object(config, "PULLBACK_LONG_MAX_CONTEXT_GAP_PCT", 0.0),
            mock.patch.object(config, "PULLBACK_LONG_MIN_RSI", 0.0),
            mock.patch.object(config, "PULLBACK_LONG_MAX_RSI", 50.0),
        ):
            result = generate_entry_signal(featured, params)

        self.assertEqual(result["signal"], "hold")
        self.assertIn("pullback_long_rsi_alto", result["reason"])

    def test_generate_entry_signal_blocks_pullback_long_above_max_score(self):
        rows = []
        for i in range(12):
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00") + pd.Timedelta(minutes=15 * i),
                    "open": 100.8 + i,
                    "high": 101.0 + i,
                    "low": 99.95 + i,
                    "close": 101.4 + i,
                    "volume": 2000.0 + i,
                    "ema_fast": 100.0 + i,
                    "ema_slow": 99.0 + i,
                    "ema_trend": 98.0 + i,
                    "rsi": 60.0 + (i * 0.1),
                    "adx": 30.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(
                strategy_engine,
                "detect_setup",
                return_value={
                    "setup": "pullback_long",
                    "direction": "long",
                    "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
                },
            ),
            mock.patch.object(config, "MIN_LONG_SCORE", 8),
            mock.patch.object(config, "MAX_PULLBACK_LONG_SCORE", 8),
            mock.patch.object(config, "PULLBACK_LONG_COUNT_BREAKOUT_SCORE", True),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertEqual(result["reason"], "pullback_long_score_alto=9")

    def test_generate_entry_signal_can_ignore_breakout_score_for_pullback_long(self):
        rows = []
        for i in range(12):
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00") + pd.Timedelta(minutes=15 * i),
                    "open": 100.8 + i,
                    "high": 101.0 + i,
                    "low": 99.95 + i,
                    "close": 101.4 + i,
                    "volume": 2000.0 + i,
                    "ema_fast": 100.0 + i,
                    "ema_slow": 99.0 + i,
                    "ema_trend": 98.0 + i,
                    "rsi": 60.0 + (i * 0.1),
                    "adx": 30.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(
                strategy_engine,
                "detect_setup",
                return_value={
                    "setup": "pullback_long",
                    "direction": "long",
                    "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
                },
            ),
            mock.patch.object(config, "MIN_LONG_SCORE", 8),
            mock.patch.object(config, "MAX_PULLBACK_LONG_SCORE", 99),
            mock.patch.object(config, "PULLBACK_LONG_COUNT_BREAKOUT_SCORE", False),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "buy")

    def test_generate_entry_signal_blocks_pullback_long_late_breakout_in_hot_context(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 100.6 + i,
                    "high": 101.2 + i,
                    "low": 99.95 + i,
                    "close": 101.4 + i,
                    "volume": 2000.0 + i,
                    "ema_fast": 100.8 + i,
                    "ema_slow": 99.6 + i,
                    "ema_trend": 98.4 + i,
                    "rsi": 59.0,
                    "adx": 30.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-1]["close"] = 112.5
        rows[-1]["high"] = 112.8
        rows[-1]["low"] = 110.7
        rows[-1]["ema_fast"] = 111.2
        rows[-1]["ema_slow"] = 109.9
        rows[-1]["ema_trend"] = 108.7
        rows[-2]["high"] = 111.0
        df = pd.DataFrame(rows)
        params = StrategyParams()

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(
                strategy_engine,
                "detect_setup",
                return_value={
                    "setup": "pullback_long",
                    "direction": "long",
                    "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
                },
            ),
            mock.patch.object(config, "MIN_LONG_SCORE", 7),
            mock.patch.object(config, "MAX_PULLBACK_LONG_SCORE", 99),
            mock.patch.object(config, "PULLBACK_LONG_COUNT_BREAKOUT_SCORE", True),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("pullback_long_breakout_tardio="))

    def test_experimental_long_side_logic_blocks_hot_pullback_context(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 100.5 + i,
                    "high": 101.3 + i,
                    "low": 99.9 + i,
                    "close": 101.1 + i,
                    "volume": 1000.0 + i,
                    "ema_fast": 100.6 + i,
                    "ema_slow": 99.6 + i,
                    "ema_trend": 98.6 + i,
                    "rsi": 58.0,
                    "adx": 30.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-1]["open"] = 111.1
        rows[-1]["high"] = 112.0
        rows[-1]["low"] = 110.5
        rows[-1]["close"] = 111.8
        rows[-1]["ema_fast"] = 111.0
        rows[-1]["ema_slow"] = 109.8
        rows[-1]["ema_trend"] = 108.6
        rows[-2]["low"] = 110.4
        rows[-2]["ema_fast"] = 110.8
        rows[-2]["high"] = 112.5
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "pullback_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_LONG_SCORE", 5),
            mock.patch.object(config, "MAX_PULLBACK_LONG_SCORE", 99),
            mock.patch.object(config, "PULLBACK_LONG_COUNT_BREAKOUT_SCORE", False),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False),
        ):
            baseline_result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_LONG_SCORE", 5),
            mock.patch.object(config, "MAX_PULLBACK_LONG_SCORE", 99),
            mock.patch.object(config, "PULLBACK_LONG_COUNT_BREAKOUT_SCORE", False),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", True),
            mock.patch.object(config, "LONG_PULLBACK_HOT_CONTEXT_GAP_PCT", 0.85),
            mock.patch.object(config, "LONG_PULLBACK_HOT_ATR_PCT", 0.40),
        ):
            experimental_result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(baseline_result["signal"], "buy")
        self.assertEqual(experimental_result["signal"], "hold")
        self.assertIn("pullback_long_contexto_quente_exp|", str(experimental_result["reason"]))

    def test_experimental_long_side_logic_blocks_hot_trend_resume_context(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 100.5 + i,
                    "high": 101.3 + i,
                    "low": 100.1 + i,
                    "close": 101.1 + i,
                    "volume": 1000.0 + i,
                    "ema_fast": 100.6 + i,
                    "ema_slow": 99.6 + i,
                    "ema_trend": 98.6 + i,
                    "rsi": 61.0,
                    "adx": 31.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 0.9,
                }
            )
        rows[-2]["high"] = 112.0
        rows[-2]["rsi"] = 58.0
        rows[-1]["open"] = 111.8
        rows[-1]["high"] = 113.4
        rows[-1]["low"] = 112.2
        rows[-1]["close"] = 113.2
        rows[-1]["volume"] = 1700.0
        rows[-1]["ema_fast"] = 111.0
        rows[-1]["ema_slow"] = 109.9
        rows[-1]["ema_trend"] = 108.6
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_LONG_SCORE", 5),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", False),
        ):
            baseline_result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_LONG_SCORE", 5),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_LONG_SIDE_LOGIC", True),
            mock.patch.object(config, "LONG_RESUME_HOT_CONTEXT_GAP_PCT", 0.88),
        ):
            experimental_result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(baseline_result["signal"], "buy")
        self.assertEqual(experimental_result["signal"], "hold")
        self.assertIn("trend_resume_long_contexto_quente_exp=", str(experimental_result["reason"]))

    def test_experimental_short_side_logic_can_upgrade_failed_rally_quality(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 120.0 - i,
                    "high": 120.8 - i,
                    "low": 118.8 - i,
                    "close": 119.4 - i,
                    "volume": 1000.0 + i,
                    "ema_fast": 119.5 - i,
                    "ema_slow": 120.1 - i,
                    "ema_trend": 121.4 - i,
                    "rsi": 39.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-2]["low"] = 108.0
        rows[-2]["close"] = 108.6
        rows[-1]["open"] = 109.4
        rows[-1]["high"] = 109.7
        rows[-1]["low"] = 108.1
        rows[-1]["close"] = 108.3
        rows[-1]["ema_fast"] = 108.9
        rows[-1]["ema_slow"] = 110.0
        rows[-1]["ema_trend"] = 111.7
        rows[-2]["close"] = 108.9
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "pullback_short",
            "direction": "short",
            "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_SHORT_SCORE", 8),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", False),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_SHORT_SIDE_LOGIC", False),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", 0.40),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_ADX", 38.0),
        ):
            baseline_result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_SHORT_SCORE", 8),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", False),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_SHORT_SIDE_LOGIC", True),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", 0.40),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_ADX", 38.0),
        ):
            experimental_result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(baseline_result["signal"], "hold")
        self.assertEqual(experimental_result["signal"], "hold")
        self.assertEqual(baseline_result["reason"], "short_score_baixo=5")
        self.assertEqual(experimental_result["reason"], "short_score_baixo=6")

    def test_generate_entry_signal_short_can_bypass_score_gate_when_disabled(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 120.0 - i,
                    "high": 120.8 - i,
                    "low": 118.8 - i,
                    "close": 119.4 - i,
                    "volume": 1000.0 + i,
                    "ema_fast": 119.5 - i,
                    "ema_slow": 120.1 - i,
                    "ema_trend": 121.4 - i,
                    "rsi": 39.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-2]["low"] = 108.0
        rows[-2]["close"] = 108.6
        rows[-1]["open"] = 109.4
        rows[-1]["high"] = 109.7
        rows[-1]["low"] = 108.1
        rows[-1]["close"] = 108.3
        rows[-1]["ema_fast"] = 108.9
        rows[-1]["ema_slow"] = 110.0
        rows[-1]["ema_trend"] = 111.7
        rows[-2]["close"] = 108.9
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "pullback_short",
            "direction": "short",
            "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "MIN_SHORT_SCORE", 8),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "EXPERIMENTAL_SHORT_SIDE_LOGIC", False),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", 0.40),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_ADX", 40.0),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT", 0.10),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "sell")
        self.assertTrue(str(result["reason"]).startswith("short_score="))

    def test_generate_entry_signal_relief_rally_short_no_longer_blocks_when_enabled(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 120.0 - i,
                    "high": 120.8 - i,
                    "low": 118.8 - i,
                    "close": 119.4 - i,
                    "volume": 1000.0 + i,
                    "ema_fast": 119.5 - i,
                    "ema_slow": 120.1 - i,
                    "ema_trend": 121.4 - i,
                    "rsi": 39.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-2]["close"] = 108.9
        rows[-1]["open"] = 109.4
        rows[-1]["high"] = 109.7
        rows[-1]["low"] = 108.1
        rows[-1]["close"] = 108.3
        rows[-1]["ema_fast"] = 108.9
        rows[-1]["ema_slow"] = 110.0
        rows[-1]["ema_trend"] = 111.7
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "relief_rally_short",
            "direction": "short",
            "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "ENABLE_SHORT_RELIEF_RALLY", True),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT", 0.10),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_MIN_ADX", 20.0),
            mock.patch.object(config, "SHORT_RSI_MIN_RELIEF_RALLY", 35.0),
            mock.patch.object(config, "SHORT_RSI_MAX_RELIEF_RALLY", 45.0),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "sell")
        self.assertNotEqual(result["reason"], "relief_rally_short bloqueado")

    def test_generate_entry_signal_pullback_short_quality_filter_blocks_weak_adx(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 120.0 - i,
                    "high": 120.8 - i,
                    "low": 118.8 - i,
                    "close": 119.4 - i,
                    "volume": 1000.0 + i,
                    "ema_fast": 119.5 - i,
                    "ema_slow": 120.1 - i,
                    "ema_trend": 121.4 - i,
                    "rsi": 39.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-2]["close"] = 108.9
        rows[-1]["open"] = 109.4
        rows[-1]["high"] = 109.7
        rows[-1]["low"] = 108.1
        rows[-1]["close"] = 108.3
        rows[-1]["ema_fast"] = 108.9
        rows[-1]["ema_slow"] = 110.0
        rows[-1]["ema_trend"] = 111.7
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "pullback_short",
            "direction": "short",
            "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", 0.40),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_ADX", 50.0),
            mock.patch.object(config, "SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT", 0.20),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("pullback_short_adx_fraco="))

    def test_generate_entry_signal_relief_rally_short_quality_filter_blocks_rsi_band(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 120.0 - i,
                    "high": 120.8 - i,
                    "low": 118.8 - i,
                    "close": 119.4 - i,
                    "volume": 1000.0 + i,
                    "ema_fast": 119.5 - i,
                    "ema_slow": 120.1 - i,
                    "ema_trend": 121.4 - i,
                    "rsi": 54.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-2]["close"] = 108.9
        rows[-1]["open"] = 109.4
        rows[-1]["high"] = 109.7
        rows[-1]["low"] = 108.1
        rows[-1]["close"] = 108.3
        rows[-1]["ema_fast"] = 108.9
        rows[-1]["ema_slow"] = 110.0
        rows[-1]["ema_trend"] = 111.7
        rows[-1]["rsi"] = 54.0
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "relief_rally_short",
            "direction": "short",
            "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "ENABLE_SHORT_RELIEF_RALLY", True),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT", 0.10),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_MIN_ADX", 20.0),
            mock.patch.object(config, "SHORT_RSI_MIN_RELIEF_RALLY", 55.0),
            mock.patch.object(config, "SHORT_RSI_MAX_RELIEF_RALLY", 58.0),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("relief_rally_short_rsi_fora="))

    def test_generate_entry_signal_trend_resume_long_quality_filter_blocks_weak_context(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 + i
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.2,
                    "high": close_price + 0.4,
                    "low": close_price - 0.6,
                    "close": close_price,
                    "volume": 1000.0 + i,
                    "ema_fast": close_price - 0.1,
                    "ema_slow": close_price - 0.4,
                    "ema_trend": close_price - 0.8,
                    "rsi": 59.0,
                    "adx": 30.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.8,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.60),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 23.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.10),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("trend_resume_long_contexto_fraco="))

    def test_generate_entry_signal_blocks_trend_resume_long_when_disabled(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 + i
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.2,
                    "high": close_price + 0.4,
                    "low": close_price - 0.6,
                    "close": close_price,
                    "volume": 1000.0 + i,
                    "ema_fast": close_price - 0.1,
                    "ema_slow": close_price - 0.4,
                    "ema_trend": close_price - 1.1,
                    "rsi": 59.0,
                    "adx": 32.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.8,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "ENABLE_LONG_RESUME", False),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertEqual(result["reason"], "trend_resume_long bloqueado")

    def test_generate_entry_signal_reroutes_pullback_long_to_resume_when_enabled(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 + i
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.3,
                    "high": close_price + 0.6,
                    "low": close_price - 0.8,
                    "close": close_price,
                    "volume": 1500.0 + i,
                    "ema_fast": close_price - 0.1,
                    "ema_slow": close_price - 0.4,
                    "ema_trend": close_price - 1.1,
                    "rsi": 60.0,
                    "adx": 35.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.8,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "pullback_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "ENABLE_LONG_PULLBACK", False),
            mock.patch.object(config, "LONG_PULLBACK_AS_RESUME_WHEN_DISABLED", True),
            mock.patch.object(config, "ENABLE_LONG_RESUME", True),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.10),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 20.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.10),
            mock.patch.object(config, "TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE", False),
            mock.patch.object(config, "MIN_LONG_SCORE", 1),
            mock.patch.object(config, "LONG_ADX_THRESHOLD", 20.0),
            mock.patch.object(config, "LONG_VOLUME_RATIO_REQUIRED", 0.5),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["setup"]["setup"], "trend_resume_long")
        self.assertEqual(result["setup"]["source_setup"], "pullback_long")

    def test_generate_entry_signal_pullback_long_blocks_weak_adx(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 + i
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.2,
                    "high": close_price + 0.4,
                    "low": close_price - 0.6,
                    "close": close_price,
                    "volume": 1000.0 + i,
                    "ema_fast": close_price - 0.1,
                    "ema_slow": close_price - 0.4,
                    "ema_trend": close_price - 1.0,
                    "rsi": 58.0,
                    "adx": 30.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.8,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "pullback_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "PULLBACK_LONG_MIN_ADX", 35.0),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("pullback_long_adx_fraco="))

    def test_generate_entry_signal_trend_resume_long_blocks_hot_context(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 + i
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.2,
                    "high": close_price + 0.4,
                    "low": close_price - 0.6,
                    "close": close_price,
                    "volume": 1000.0 + i,
                    "ema_fast": close_price - 0.1,
                    "ema_slow": close_price - 0.4,
                    "ema_trend": close_price - 1.4,
                    "rsi": 59.0,
                    "adx": 32.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.8,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.10),
            mock.patch.object(config, "TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT", 0.80),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 20.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.10),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("trend_resume_long_contexto_quente="))

    def test_generate_entry_signal_trend_resume_long_blocks_stretched_rsi(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 + i
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price - 0.2,
                    "high": close_price + 0.4,
                    "low": close_price - 0.6,
                    "close": close_price,
                    "volume": 1000.0 + i,
                    "ema_fast": close_price - 0.1,
                    "ema_slow": close_price - 0.4,
                    "ema_trend": close_price - 1.0,
                    "rsi": 81.0,
                    "adx": 32.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.8,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "trend_resume_long",
            "direction": "long",
            "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.10),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_ADX", 20.0),
            mock.patch.object(config, "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.10),
            mock.patch.object(config, "TREND_RESUME_LONG_MAX_RSI", 78.0),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("trend_resume_long_rsi_esticado="))

    def test_generate_entry_signal_trend_resume_short_requires_breakdown_when_enabled(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            close_price = 100.0 - (i * 0.05)
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": close_price + 0.2,
                    "high": close_price + 0.4,
                    "low": close_price - 0.05,
                    "close": close_price,
                    "volume": 2000.0 + i,
                    "ema_fast": close_price + 0.2,
                    "ema_slow": close_price + 0.6,
                    "ema_trend": close_price + 1.2,
                    "rsi": 33.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 1.0,
                    "atr_pct": 0.9,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        patched_setup = {
            "setup": "trend_resume_short",
            "direction": "short",
            "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
        }
        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=patched_setup),
            mock.patch.object(config, "TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT", 0.10),
            mock.patch.object(config, "TREND_RESUME_SHORT_MIN_ADX", 20.0),
            mock.patch.object(config, "TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION", True),
            mock.patch.object(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.12),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertTrue(str(result["reason"]).startswith("trend_resume_short_sem_breakdown="))

    def test_generate_entry_signal_short_handles_prev_rsi_na_without_crashing(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 100.0 - i,
                    "high": 100.4 - i,
                    "low": 99.6 - i,
                    "close": 99.8 - i,
                    "volume": 2000.0 + i,
                    "ema_fast": 99.5 - i,
                    "ema_slow": 100.0 - i,
                    "ema_trend": 100.5 - i,
                    "rsi": 45.0,
                    "adx": 25.0,
                    "vol_ma": 1000.0,
                    "atr": 1.2,
                    "atr_pct": 1.0,
                }
            )
        rows[-2]["rsi"] = pd.NA
        df = pd.DataFrame(rows)
        params = StrategyParams()

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(
                strategy_engine,
                "detect_setup",
                return_value={
                    "setup": "trend_resume_short",
                    "direction": "short",
                    "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": True},
                },
            ),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertIn(result["signal"], {"hold", "sell"})

    def test_detect_market_regime_keeps_trend_bear_even_with_low_short_atr(self):
        df = pd.DataFrame(
            [
                {
                    "close": 100.0,
                    "ema_fast": 99.0,
                    "ema_slow": 100.0,
                    "ema_trend": 101.0,
                    "atr_pct": 0.18,
                },
                {
                    "close": 99.0,
                    "ema_fast": 98.0,
                    "ema_slow": 99.4,
                    "ema_trend": 100.8,
                    "atr_pct": 0.18,
                },
            ]
        )
        params = StrategyParams(short_min_atr_pct=0.30, short_regime_gap_pct=0.34)

        regime = strategy_engine.detect_market_regime(df, params, index=-1)

        self.assertEqual(regime["regime"], "trend_bear")
        self.assertEqual(regime["regime_detail"], "trend_bear_atr_low")
        self.assertFalse(bool(regime["tradeable_short"]))

    def test_detect_market_regime_explains_weak_bull_blockers(self):
        df = pd.DataFrame(
            [
                {
                    "close": 100.0,
                    "ema_fast": 101.0,
                    "ema_slow": 100.5,
                    "ema_trend": 100.0,
                    "atr_pct": 0.10,
                },
                {
                    "close": 100.2,
                    "ema_fast": 101.2,
                    "ema_slow": 100.6,
                    "ema_trend": 100.1,
                    "atr_pct": 0.10,
                },
            ]
        )
        params = StrategyParams(long_min_atr_pct=0.15, long_regime_gap_pct=0.8)

        regime = strategy_engine.detect_market_regime(df, params, index=-1)

        self.assertEqual(regime["regime"], "weak_bull")
        self.assertEqual(regime["regime_detail"], "weak_bull_gap_atr")
        self.assertFalse(bool(regime["tradeable_long"]))
        self.assertFalse(bool(regime["gap_tradeable_long"]))

    def test_generate_entry_signal_short_no_longer_blocks_only_due_to_low_atr(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 120.0 - i,
                    "high": 120.5 - i,
                    "low": 118.5 - i,
                    "close": 119.2 - i,
                    "volume": 1500.0 + i,
                    "ema_fast": 119.0 - i,
                    "ema_slow": 120.0 - i,
                    "ema_trend": 121.2 - i,
                    "rsi": 38.0,
                    "adx": 42.0,
                    "vol_ma": 1000.0,
                    "atr": 0.2,
                    "atr_pct": 0.18,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams(short_min_atr_pct=0.30)

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(
                strategy_engine,
                "detect_setup",
                return_value={
                    "setup": "trend_resume_short",
                    "direction": "short",
                    "regime": {"regime": "trend_bear", "tradeable_long": True, "tradeable_short": False},
                },
            ),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "ENABLE_SHORT_RESUME", True),
            mock.patch.object(config, "SHORT_REQUIRE_STRICT_REGIME", True),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertNotIn("ATR Short insuficiente", str(result["reason"]))
        self.assertIn(result["signal"], {"hold", "sell"})

    def test_generate_entry_signal_blocks_altcoin_when_context_is_weak(self):
        rows = []
        base_time = pd.Timestamp("2026-04-12T00:00:00+00:00")
        for i in range(12):
            rows.append(
                {
                    "timestamp": base_time + pd.Timedelta(minutes=15 * i),
                    "open": 100.0 + i * 0.1,
                    "high": 100.3 + i * 0.1,
                    "low": 99.9 + i * 0.1,
                    "close": 100.2 + i * 0.1,
                    "volume": 2000.0 + i,
                    "ema_fast": 100.5 + i * 0.1,
                    "ema_slow": 100.0 + i * 0.1,
                    "ema_trend": 99.98 + i * 0.1,
                    "rsi": 58.0,
                    "adx": 24.0,
                    "vol_ma": 1000.0,
                    "atr": 0.5,
                    "atr_pct": 0.2,
                }
            )
        df = pd.DataFrame(rows)
        params = StrategyParams()

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(
                strategy_engine,
                "detect_setup",
                return_value={
                    "setup": "pullback_long",
                    "direction": "long",
                    "regime": {"regime": "trend_bull", "tradeable_long": True, "tradeable_short": True},
                },
            ),
            mock.patch.object(config, "BLOCKED_LONG_ENTRY_HOURS_UTC", []),
            mock.patch.object(config, "get_symbol_family_key", return_value="alt_trend_strict"),
            mock.patch.object(config, "ALT_STRICT_CONTEXT_FILTER", True),
            mock.patch.object(config, "ALT_MIN_CONTEXT_GAP_PCT", 0.30),
            mock.patch.object(config, "ALT_MIN_GLOBAL_ATR_PCT", 0.14),
        ):
            result = strategy_engine.generate_entry_signal(df, params, index=len(df) - 1)

        self.assertEqual(result["signal"], "hold")
        self.assertIn("contexto alt fraco", str(result["reason"]))

    def test_generate_entry_signal_blocks_long_on_configured_hour(self):
        df = self._build_buy_signal_df()
        df["timestamp"] = pd.date_range("2026-04-12 00:00:00+00:00", periods=len(df), freq="15min")
        prepared = prepare_candle_features(df)

        with (
            mock.patch.object(config, "MIN_LONG_SCORE", 4),
            mock.patch.object(config, "BLOCKED_LONG_ENTRY_HOURS_UTC", [prepared.index[-1].hour]),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", True),
        ):
            result = generate_entry_signal(prepared, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "hold")
        self.assertIn("hora bloqueada para long", result["reason"])

    def test_runtime_defaults_keep_both_sides_with_short_hour_filter_enabled(self):
        self.assertTrue(config.ALLOW_LONG)
        self.assertTrue(config.ALLOW_SHORT)
        self.assertTrue(config.USE_ENTRY_HOUR_BLOCKS)
        self.assertEqual(
            config.BLOCKED_SHORT_ENTRY_HOURS_UTC,
            [0, 3, 6, 9, 12, 13, 15, 16, 17],
        )

    def test_analyze_prepared_candle_sell_signal(self):
        df = self._build_sell_signal_df()
        with mock.patch.object(config, "ALLOW_SHORT", True), mock.patch.object(config, "MIN_SHORT_SCORE", 4), mock.patch.object(config, "ENABLE_SHORT_RESUME", True), mock.patch.object(config, "SHORT_MAX_DISTANCE_EMA_PCT", 20.0), mock.patch.object(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.0), mock.patch.object(config, "BLOCKED_SHORT_ENTRY_HOURS_UTC", []):
            result = analyze_prepared_candle(df)
        self.assertEqual(result["signal"], "sell")

    def test_generate_entry_signal_blocks_short_on_configured_hour(self):
        df = self._build_sell_signal_df()
        df["timestamp"] = pd.date_range("2026-04-12 00:00:00+00:00", periods=len(df), freq="15min")
        prepared = prepare_candle_features(df)

        with (
            mock.patch.object(config, "ALLOW_SHORT", True),
            mock.patch.object(config, "MIN_SHORT_SCORE", 4),
            mock.patch.object(config, "ENABLE_SHORT_RESUME", True),
            mock.patch.object(config, "SHORT_MAX_DISTANCE_EMA_PCT", 20.0),
            mock.patch.object(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.0),
            mock.patch.object(config, "BLOCKED_SHORT_ENTRY_HOURS_UTC", [prepared.index[-1].hour]),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", True),
        ):
            result = generate_entry_signal(prepared, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "hold")
        self.assertIn("hora bloqueada para short", result["reason"])

    def test_generate_entry_signal_ignores_short_blocked_hour_when_gate_disabled(self):
        df = self._build_sell_signal_df()
        df["timestamp"] = pd.date_range("2026-04-12 00:00:00+00:00", periods=len(df), freq="15min")
        prepared = prepare_candle_features(df)

        with (
            mock.patch.object(config, "ALLOW_SHORT", True),
            mock.patch.object(config, "MIN_SHORT_SCORE", 4),
            mock.patch.object(config, "ENABLE_SHORT_RESUME", True),
            mock.patch.object(config, "SHORT_MAX_DISTANCE_EMA_PCT", 20.0),
            mock.patch.object(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.0),
            mock.patch.object(config, "BLOCKED_SHORT_ENTRY_HOURS_UTC", [prepared.index[-1].hour]),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(prepared, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "sell")
        self.assertNotIn("hora bloqueada para short", str(result["reason"]))

    def test_runtime_and_backtest_share_same_short_entry_engine_without_legacy_blocks(self):
        df = self._build_sell_signal_df()
        df["timestamp"] = pd.date_range("2026-04-12 00:00:00+00:00", periods=len(df), freq="15min")
        prepared = prepare_candle_features(df)
        params = StrategyParams()

        with (
            mock.patch.object(config, "ALLOW_SHORT", True),
            mock.patch.object(config, "ENABLE_SHORT_RESUME", True),
            mock.patch.object(config, "ENABLE_SHORT_RELIEF_RALLY", True),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", True),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
            mock.patch.object(config, "MIN_SHORT_SCORE", 99),
            mock.patch.object(config, "SHORT_MAX_DISTANCE_EMA_PCT", 20.0),
            mock.patch.object(config, "SHORT_BREAKDOWN_BUFFER_PCT", 0.0),
        ):
            runtime_signal = generate_entry_signal(prepared, params, index=-1)
            backtest_signal = strategy_engine.generate_entry_signal(prepared, params, index=len(prepared) - 1)

        self.assertEqual(runtime_signal["signal"], "sell")
        self.assertEqual(backtest_signal["signal"], "sell")
        self.assertEqual(runtime_signal["signal"], backtest_signal["signal"])

    def test_analyze_prepared_candle_hold_without_trigger(self):
        df = self._build_trend_df(start_price=100.0, step=0.9, length=260)
        result = analyze_prepared_candle(
            df,
            buy_rsi_threshold=float(config.BUY_RSI_SIGNAL) + 20.0,
            sell_rsi_threshold=float(config.SELL_RSI_SIGNAL) - 10.0,
        )
        self.assertEqual(result["signal"], "hold")

    def test_long_position_partial_then_stop_or_trailing_close(self):
        position = create_position("buy", 100.0, 1, atr=1.0)
        partial_result = evaluate_open_position(position, 105.0, 2)
        self.assertEqual(partial_result["action"], "partial")

        close_result = evaluate_open_position(partial_result["position"], 1.0, 3)
        self.assertEqual(close_result["action"], "close")
        self.assertEqual(close_result["closed_position"]["reason"], "stop_or_trailing")

    def test_short_position_partial_then_stop_or_trailing_close(self):
        position = create_position("sell", 100.0, 1, atr=1.0)
        partial_result = evaluate_open_position(position, 95.0, 2)
        self.assertEqual(partial_result["action"], "partial")

        close_result = evaluate_open_position(partial_result["position"], 10_000.0, 3)
        self.assertEqual(close_result["action"], "close")
        self.assertEqual(close_result["closed_position"]["reason"], "stop_or_trailing")

    def test_long_position_without_partial_activates_protection_and_keeps_full_size(self):
        position = create_position("buy", 100.0, 1, atr=1.0)
        with mock.patch.object(config, "ENABLE_PARTIAL_TAKE_PROFIT", False, create=True):
            result = evaluate_open_position(position, 101.0, 2)

        self.assertEqual(result["action"], "hold")
        self.assertFalse(result["position"]["partial_taken"])
        self.assertTrue(result["position"]["break_even_active"])
        self.assertGreater(result["position"]["current_stop"], position["entry_price"])

    def test_short_position_without_partial_activates_protection_and_keeps_full_size(self):
        position = create_position("sell", 100.0, 1, atr=1.0)
        with mock.patch.object(config, "ENABLE_PARTIAL_TAKE_PROFIT", False, create=True):
            result = evaluate_open_position(position, 98.0, 2)

        self.assertEqual(result["action"], "hold")
        self.assertFalse(result["position"]["partial_taken"])
        self.assertTrue(result["position"]["break_even_active"])
        self.assertLess(result["position"]["current_stop"], position["entry_price"])

    def test_trend_resume_short_ignores_intrabar_wick_for_profit_protection(self):
        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "TREND_RESUME_SHORT_STOP_LOSS_PCT", 2.0),
            mock.patch.object(config, "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT", 2.4),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT", 2.2),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_STOP_PCT", 0.8),
            mock.patch.object(config, "TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION", True),
        ):
            position = create_position(
                "sell",
                100.0,
                pd.Timestamp("2026-04-12T00:00:00+00:00"),
                atr=0.1,
                entry_setup="trend_resume_short",
            )
            candle = pd.Series(
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.6,
                    "low": 97.75,
                    "close": 99.4,
                }
            )

            result = evaluate_managed_position_on_candle(position, candle, realized_partial_pct=0.0)

        self.assertEqual(result["action"], "hold")
        self.assertFalse(result["position"]["break_even_active"])
        self.assertFalse(result["position"]["partial_taken"])
        self.assertAlmostEqual(result["position"]["current_stop"], position["initial_stop"], places=6)

    def test_trend_resume_short_confirms_protection_on_close_below_trigger(self):
        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "TREND_RESUME_SHORT_STOP_LOSS_PCT", 2.0),
            mock.patch.object(config, "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT", 2.4),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT", 2.2),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_STOP_PCT", 0.8),
            mock.patch.object(config, "TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION", True),
        ):
            position = create_position(
                "sell",
                100.0,
                pd.Timestamp("2026-04-12T00:00:00+00:00"),
                atr=0.1,
                entry_setup="trend_resume_short",
            )
            candle = pd.Series(
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 100.8,
                    "low": 97.75,
                    "close": 97.7,
                }
            )

            result = evaluate_managed_position_on_candle(position, candle, realized_partial_pct=0.0)

        self.assertEqual(result["action"], "hold")
        self.assertFalse(result["position"]["partial_taken"])
        self.assertTrue(result["position"]["break_even_active"])
        self.assertLess(result["position"]["current_stop"], position["initial_stop"])
        self.assertAlmostEqual(result["position"]["current_stop"], 98.4816, places=4)
        self.assertAlmostEqual(result["realized_partial_pct"], 0.0, places=6)

    def test_trend_resume_short_keeps_intrabar_protection_after_partial_target(self):
        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "TREND_RESUME_SHORT_STOP_LOSS_PCT", 2.0),
            mock.patch.object(config, "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT", 2.4),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT", 2.2),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_STOP_PCT", 0.8),
            mock.patch.object(config, "TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION", True),
        ):
            position = create_position(
                "sell",
                100.0,
                pd.Timestamp("2026-04-12T00:00:00+00:00"),
                atr=0.1,
                entry_setup="trend_resume_short",
            )
            partial_result = evaluate_open_position(position, 97.4, pd.Timestamp("2026-04-12T00:15:00+00:00"))

        self.assertEqual(partial_result["action"], "partial")
        self.assertTrue(partial_result["position"]["partial_taken"])
        self.assertTrue(partial_result["position"]["break_even_active"])
        self.assertAlmostEqual(partial_result["position"]["current_stop"], 98.1792, places=4)

    def test_short_position_can_fill_at_stop_price_for_backtest(self):
        position = create_position("sell", 100.0, 1, atr=1.0)
        close_result = evaluate_open_position(
            position,
            200.0,
            2,
            exit_at_stop_price=True,
        )
        self.assertEqual(close_result["action"], "close")
        self.assertEqual(
            close_result["closed_position"]["exit_price"],
            position["current_stop"],
        )

    def test_create_position_enforces_min_risk_reward_ratio_from_real_stop_distance(self):
        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", True),
            mock.patch.object(config, "MIN_RISK_REWARD_RATIO", 2.0),
            mock.patch.object(config, "PULLBACK_LONG_STOP_LOSS_PCT", 1.7),
            mock.patch.object(config, "PULLBACK_LONG_PARTIAL_TARGET_PCT", 1.4),
            mock.patch.object(config, "PULLBACK_LONG_TRAILING_TRIGGER_PCT", 1.4),
            mock.patch.object(config, "PULLBACK_LONG_USE_FIXED_STOP", True),
        ):
            position = create_position(
                "buy",
                100.0,
                pd.Timestamp("2026-04-12T00:00:00+00:00"),
                atr=0.1,
                entry_setup="pullback_long",
            )

        self.assertAlmostEqual(position["initial_stop"], 98.3, places=6)
        self.assertAlmostEqual(position["partial_target"], 103.4, places=6)
        self.assertAlmostEqual(position["trailing_trigger_price"], 103.4, places=6)
        self.assertAlmostEqual(position["partial_target_pct"], 3.4, places=6)
        self.assertAlmostEqual(position["trailing_trigger_pct"], 3.4, places=6)

    def test_backtest_candle_exits_short_at_open_on_gap_through_stop(self):
        position = create_position("sell", 100.0, 1, atr=1.0)
        candle = pd.Series(
            {
                "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                "open": 105.0,
                "high": 106.0,
                "low": 104.0,
                "close": 105.5,
            }
        )

        result = backtest._evaluate_backtest_position_on_candle(
            position=position,
            row=candle,
            realized_partial_pct=0.0,
        )

        self.assertEqual(result["action"], "close")
        self.assertEqual(result["closed_position"]["exit_price"], 105.0)

    def test_backtest_candle_uses_partial_target_price_instead_of_close(self):
        position = create_position("buy", 100.0, 1, atr=1.0)
        candle = pd.Series(
            {
                "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                "open": 100.1,
                "high": 103.0,
                "low": 99.9,
                "close": 102.5,
            }
        )

        result = backtest._evaluate_backtest_position_on_candle(
            position=position,
            row=candle,
            realized_partial_pct=0.0,
        )

        self.assertEqual(result["action"], "hold")
        self.assertTrue(result["position"]["partial_taken"])
        expected_partial_pct = ((position["partial_target"] - position["entry_price"]) / position["entry_price"]) * 100.0 * 0.5
        self.assertAlmostEqual(result["realized_partial_pct"], expected_partial_pct, places=6)

    def test_runtime_position_applies_trend_resume_long_fixed_stop_profile(self):
        signal_result = {
            "signal": "buy",
            "reason": "teste",
            "atr": 2.0,
            "setup": {
                "setup": "trend_resume_long",
                "direction": "long",
                "regime": {"regime": "trend_bull"},
            },
        }

        with (
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.5),
            mock.patch.object(config, "TREND_RESUME_LONG_STOP_LOSS_PCT", 0.9),
            mock.patch.object(config, "TREND_RESUME_LONG_USE_FIXED_STOP", True),
        ):
            position = bot_runner._build_runtime_position(
                signal="buy",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=2.0,
                execution_profile="managed",
                signal_result=signal_result,
            )

        self.assertAlmostEqual(position["initial_stop"], 99.1, places=6)
        self.assertAlmostEqual(position["current_stop"], 99.1, places=6)
        self.assertAlmostEqual(position["stop_loss_pct"], 0.9, places=6)
        self.assertEqual(position["management_profile"], "trend_resume_long")
        self.assertEqual(position["entry_setup"], "trend_resume_long")

    def test_runtime_position_applies_trend_resume_short_profile(self):
        signal_result = {
            "signal": "sell",
            "reason": "teste",
            "atr": 0.1,
            "setup": {
                "setup": "trend_resume_short",
                "direction": "short",
                "regime": {"regime": "trend_bear"},
            },
        }

        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "TREND_RESUME_SHORT_STOP_LOSS_PCT", 2.0),
            mock.patch.object(config, "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT", 2.4),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT", 2.2),
            mock.patch.object(config, "TREND_RESUME_SHORT_TRAILING_STOP_PCT", 0.8),
        ):
            position = bot_runner._build_runtime_position(
                signal="sell",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=0.1,
                execution_profile="managed",
                signal_result=signal_result,
            )

        self.assertAlmostEqual(position["initial_stop"], 102.0, places=6)
        self.assertAlmostEqual(position["partial_target"], 97.6, places=6)
        self.assertAlmostEqual(position["trailing_trigger_price"], 97.8, places=6)
        self.assertAlmostEqual(position["stop_loss_pct"], 2.0, places=6)
        self.assertEqual(position["management_profile"], "trend_resume_short")
        self.assertEqual(position["entry_setup"], "trend_resume_short")

    def test_trend_resume_short_default_waits_for_larger_move(self):
        signal_result = {
            "signal": "sell",
            "reason": "teste",
            "atr": 0.1,
            "setup": {
                "setup": "trend_resume_short",
                "direction": "short",
                "regime": {"regime": "trend_bear"},
            },
        }

        with mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False):
            position = bot_runner._build_runtime_position(
                signal="sell",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=0.1,
                execution_profile="managed",
                signal_result=signal_result,
            )

        self.assertAlmostEqual(position["partial_target"], 97.6, places=6)
        self.assertAlmostEqual(position["trailing_trigger_price"], 97.8, places=6)
        self.assertAlmostEqual(position["partial_target_pct"], 2.4, places=6)
        self.assertAlmostEqual(position["trailing_trigger_pct"], 2.2, places=6)

    def test_runtime_position_applies_relief_rally_short_profile(self):
        signal_result = {
            "signal": "sell",
            "reason": "teste",
            "atr": 0.1,
            "setup": {
                "setup": "relief_rally_short",
                "direction": "short",
                "regime": {"regime": "trend_bear"},
            },
        }

        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_STOP_LOSS_PCT", 1.4),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_PARTIAL_TARGET_PCT", 1.8),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_TRAILING_TRIGGER_PCT", 1.8),
            mock.patch.object(config, "RELIEF_RALLY_SHORT_TRAILING_STOP_PCT", 0.4),
        ):
            position = bot_runner._build_runtime_position(
                signal="sell",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=0.1,
                execution_profile="managed",
                signal_result=signal_result,
            )

        self.assertAlmostEqual(position["initial_stop"], 101.4, places=6)
        self.assertAlmostEqual(position["partial_target"], 98.2, places=6)
        self.assertAlmostEqual(position["trailing_trigger_price"], 98.2, places=6)
        self.assertAlmostEqual(position["stop_loss_pct"], 1.4, places=6)
        self.assertEqual(position["management_profile"], "relief_rally_short")
        self.assertEqual(position["entry_setup"], "relief_rally_short")

    def test_runtime_position_applies_pullback_long_profile(self):
        signal_result = {
            "signal": "buy",
            "reason": "teste",
            "atr": 0.1,
            "setup": {
                "setup": "pullback_long",
                "direction": "long",
                "regime": {"regime": "trend_bull"},
            },
        }

        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "PULLBACK_LONG_STOP_LOSS_PCT", 1.8),
            mock.patch.object(config, "PULLBACK_LONG_PARTIAL_TARGET_PCT", 1.4),
            mock.patch.object(config, "PULLBACK_LONG_TRAILING_TRIGGER_PCT", 1.4),
            mock.patch.object(config, "PULLBACK_LONG_TRAILING_STOP_PCT", 0.6),
        ):
            position = bot_runner._build_runtime_position(
                signal="buy",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=0.1,
                execution_profile="managed",
                signal_result=signal_result,
            )

        self.assertAlmostEqual(position["initial_stop"], 98.2, places=6)
        self.assertAlmostEqual(position["partial_target"], 101.4, places=6)
        self.assertAlmostEqual(position["trailing_trigger_price"], 101.4, places=6)
        self.assertAlmostEqual(position["stop_loss_pct"], 1.8, places=6)
        self.assertEqual(position["management_profile"], "pullback_long")
        self.assertEqual(position["entry_setup"], "pullback_long")

    def test_runtime_position_applies_market_reading_long_profile(self):
        signal_result = {
            "signal": "buy",
            "reason": "teste",
            "atr": 0.1,
            "setup": {
                "setup": "market_reading_long",
                "direction": "long",
                "regime": {"regime": "range"},
            },
        }

        with (
            mock.patch.object(config, "ENFORCE_MIN_RISK_REWARD_RATIO", False),
            mock.patch.object(config, "MARKET_READING_LONG_STOP_LOSS_PCT", 1.25),
            mock.patch.object(config, "MARKET_READING_LONG_PARTIAL_TARGET_PCT", 1.0),
            mock.patch.object(config, "MARKET_READING_LONG_TRAILING_TRIGGER_PCT", 1.1),
            mock.patch.object(config, "MARKET_READING_LONG_TRAILING_STOP_PCT", 0.45),
        ):
            position = bot_runner._build_runtime_position(
                signal="buy",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=0.1,
                execution_profile="managed",
                signal_result=signal_result,
            )

        self.assertAlmostEqual(position["initial_stop"], 98.75, places=6)
        self.assertAlmostEqual(position["partial_target"], 101.0, places=6)
        self.assertAlmostEqual(position["trailing_trigger_price"], 101.1, places=6)
        self.assertAlmostEqual(position["stop_loss_pct"], 1.25, places=6)
        self.assertEqual(position["management_profile"], "market_reading_long")
        self.assertEqual(position["entry_setup"], "market_reading_long")

    def test_run_backtest_manages_last_candle_before_forced_close(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00"),
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                    "volume": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                    "volume": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:30:00+00:00"),
                    "open": 100.0,
                    "high": 103.0,
                    "low": 99.8,
                    "close": 102.5,
                    "volume": 10.0,
                },
            ]
        )

        with (
            mock.patch.object(backtest, "calculate_indicators", return_value=df),
            mock.patch.object(backtest, "get_min_required_rows", return_value=0),
            mock.patch.object(
                backtest,
                "UnifiedDecisionEngine",
                return_value=mock.Mock(
                    decide_entry=mock.Mock(
                        side_effect=[
                            {"signal": "hold"},
                            {"signal": "sell", "atr": 1.0},
                        ]
                    )
                ),
            ),
            mock.patch.object(backtest, "save_detailed_report"),
        ):
            trades, summary = backtest.run_backtest(
                "BTC/USDT",
                "15m",
                candles=len(df),
                fee_pct=config.FEE_PCT,
                testnet=False,
                use_local_csv=True,
                slippage_pct=config.SLIPPAGE_PCT,
                preloaded_df=df,
                execution_profile="managed",
            )

        self.assertEqual(summary["trades"], 1)
        self.assertEqual(trades[0]["side"], "short")
        self.assertAlmostEqual(trades[0]["entry_price"], 100.0, places=6)
        self.assertAlmostEqual(trades[0]["exit_price"], 101.5, places=6)

    def test_run_backtest_native_bracket_enters_on_signal_close(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00"),
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                    "volume": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.5,
                    "low": 99.8,
                    "close": 101.0,
                    "volume": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:30:00+00:00"),
                    "open": 101.0,
                    "high": 103.5,
                    "low": 100.5,
                    "close": 103.0,
                    "volume": 10.0,
                },
            ]
        )

        with (
            mock.patch.object(backtest, "calculate_indicators", return_value=df),
            mock.patch.object(backtest, "get_min_required_rows", return_value=0),
            mock.patch.object(
                backtest,
                "UnifiedDecisionEngine",
                return_value=mock.Mock(
                    decide_entry=mock.Mock(
                        side_effect=[
                            {"signal": "hold"},
                            {"signal": "buy", "atr": 1.0},
                        ]
                    )
                ),
            ),
            mock.patch.object(backtest, "save_detailed_report"),
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.0),
            mock.patch.object(config, "LONG_TAKE_PROFIT_PCT", 2.0),
        ):
            trades, summary = backtest.run_backtest(
                "BTC/USDT",
                "15m",
                candles=len(df),
                fee_pct=config.FEE_PCT,
                testnet=False,
                use_local_csv=True,
                slippage_pct=config.SLIPPAGE_PCT,
                preloaded_df=df,
                execution_profile="native_bracket",
            )

        self.assertEqual(summary["trades"], 1)
        self.assertEqual(trades[0]["side"], "long")
        self.assertAlmostEqual(trades[0]["entry_price"], 101.0, places=6)
        self.assertAlmostEqual(trades[0]["exit_price"], 103.02, places=6)

    def test_run_backtest_can_reuse_precomputed_indicators(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00"),
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                    "volume": 10.0,
                    "ema_fast": 100.0,
                    "ema_slow": 100.0,
                    "ema_trend": 100.0,
                    "rsi": 50.0,
                    "atr": 1.0,
                    "atr_pct": 1.0,
                    "adx": 25.0,
                    "vol_ma": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.5,
                    "low": 99.8,
                    "close": 101.0,
                    "volume": 10.0,
                    "ema_fast": 100.0,
                    "ema_slow": 100.0,
                    "ema_trend": 100.0,
                    "rsi": 50.0,
                    "atr": 1.0,
                    "atr_pct": 1.0,
                    "adx": 25.0,
                    "vol_ma": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:30:00+00:00"),
                    "open": 101.0,
                    "high": 103.5,
                    "low": 100.5,
                    "close": 103.0,
                    "volume": 10.0,
                    "ema_fast": 100.0,
                    "ema_slow": 100.0,
                    "ema_trend": 100.0,
                    "rsi": 50.0,
                    "atr": 1.0,
                    "atr_pct": 1.0,
                    "adx": 25.0,
                    "vol_ma": 10.0,
                },
            ]
        )

        with (
            mock.patch.object(backtest, "calculate_indicators", side_effect=AssertionError("should not recalc")),
            mock.patch.object(backtest, "get_min_required_rows", return_value=0),
            mock.patch.object(
                backtest,
                "UnifiedDecisionEngine",
                return_value=mock.Mock(
                    decide_entry=mock.Mock(
                        side_effect=[
                            {"signal": "hold"},
                            {"signal": "buy", "atr": 1.0},
                        ]
                    )
                ),
            ),
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.0),
            mock.patch.object(config, "LONG_TAKE_PROFIT_PCT", 2.0),
        ):
            trades, summary = backtest.run_backtest(
                "BTC/USDT",
                "15m",
                candles=len(df),
                fee_pct=config.FEE_PCT,
                testnet=False,
                use_local_csv=True,
                slippage_pct=config.SLIPPAGE_PCT,
                preloaded_df=df,
                execution_profile="native_bracket",
                precomputed_indicators=True,
                verbose=False,
                save_report=False,
            )

        self.assertEqual(summary["trades"], 1)
        self.assertEqual(trades[0]["side"], "long")

    def test_build_account_risk_summary_uses_project_risk_sizing(self):
        trades = [
            {"side": "long", "net_pct": 2.82},
            {"side": "short", "net_pct": -1.38},
        ]

        summary = backtest.build_account_risk_summary(
            trades,
            initial_balance=50.0,
            risk_per_trade_pct=0.25,
            position_sizing_mode="risk",
        )

        self.assertEqual(summary["initial_balance_usdt"], 50.0)
        self.assertAlmostEqual(summary["final_balance_usdt"], 50.0906, places=4)
        self.assertAlmostEqual(summary["net_profit_usdt"], 0.0906, places=4)
        self.assertAlmostEqual(summary["return_pct"], 0.1811, places=4)
        self.assertGreater(summary["long_pnl_usdt"], 0.0)
        self.assertLess(summary["short_pnl_usdt"], 0.0)

    def test_calculate_position_size_supports_margin_allocation_sizing(self):
        service = RiskManagementService()

        sizing = service.calculate_position_size(
            account_balance=15.0,
            entry_price=60000.0,
            stop_loss_pct=1.2,
            risk_pct=0.25,
            leverage=5,
            sizing_mode="allocation",
            margin_allocation_pct=50.0,
        )

        self.assertEqual(sizing["sizing_mode"], "allocation")
        self.assertAlmostEqual(sizing["position_notional"], 37.5, places=2)
        self.assertAlmostEqual(sizing["margin_allocated_amount"], 7.5, places=2)
        self.assertAlmostEqual(sizing["risk_amount"], 0.45, places=2)
        self.assertAlmostEqual(sizing["effective_risk_pct"], 3.0, places=4)

    def test_calculate_position_size_order_value_uses_balance_pct_as_notional_not_margin(self):
        service = RiskManagementService()

        sizing = service.calculate_position_size(
            account_balance=21.45,
            entry_price=62500.0,
            stop_loss_pct=1.5,
            risk_pct=2.0,
            leverage=10,
            sizing_mode="order_value",
            margin_allocation_pct=100.0,
        )

        self.assertEqual(sizing["sizing_mode"], "order_value")
        self.assertAlmostEqual(sizing["position_notional"], 21.45, places=2)
        self.assertAlmostEqual(sizing["margin_allocated_amount"], 2.145, places=3)
        self.assertAlmostEqual(sizing["quantity"], 0.000343, places=6)

    def test_calculate_position_size_short_stop_price_stays_above_entry(self):
        service = RiskManagementService()

        sizing = service.calculate_position_size(
            account_balance=100.0,
            entry_price=100.0,
            stop_loss_pct=1.5,
            risk_pct=2.0,
            leverage=10,
            sizing_mode="risk",
            position_side="short",
        )

        self.assertAlmostEqual(sizing["stop_loss_price"], 101.5, places=6)

    def test_build_account_risk_summary_supports_margin_allocation_model(self):
        trades = [
            {"side": "long", "net_pct": 2.82},
            {"side": "short", "net_pct": -1.38},
        ]

        summary = backtest.build_account_risk_summary(
            trades,
            initial_balance=50.0,
            risk_per_trade_pct=0.25,
            leverage=5,
            position_sizing_mode="allocation",
            position_margin_allocation_pct=50.0,
        )

        self.assertEqual(summary["model"], "fixed_margin_allocation")
        self.assertEqual(summary["position_sizing_mode"], "allocation")
        self.assertAlmostEqual(summary["position_margin_allocation_pct"], 50.0, places=4)
        self.assertAlmostEqual(summary["avg_effective_risk_pct"], 3.375, places=3)
        self.assertAlmostEqual(summary["final_balance_usdt"], 51.6784, places=4)
        self.assertAlmostEqual(summary["net_profit_usdt"], 1.6784, places=4)

    def test_calculate_position_size_supports_hybrid_risk_capped_allocation(self):
        service = RiskManagementService()

        sizing = service.calculate_position_size(
            account_balance=15.0,
            entry_price=60000.0,
            stop_loss_pct=1.5,
            risk_pct=3.0,
            leverage=5,
            sizing_mode="hybrid",
            margin_allocation_pct=50.0,
        )

        self.assertEqual(sizing["sizing_mode"], "hybrid")
        self.assertAlmostEqual(sizing["position_notional"], 30.0, places=2)
        self.assertAlmostEqual(sizing["margin_allocated_amount"], 6.0, places=2)
        self.assertAlmostEqual(sizing["margin_allocation_pct"], 40.0, places=2)
        self.assertAlmostEqual(sizing["risk_amount"], 0.45, places=2)
        self.assertAlmostEqual(sizing["effective_risk_pct"], 3.0, places=4)

    def test_live_trade_plan_caps_configured_allocation_by_risk_for_small_btc_account(self):
        database = mock.Mock()
        database.get_user_live_portfolio_risk_summary.return_value = {
            "open_trades": 0,
            "total_open_risk_pct": 0.0,
        }
        database.get_daily_live_guardrail_summary.return_value = {
            "closed_trades": 0,
            "realized_pnl_pct": 0.0,
            "consecutive_losses": 0,
        }
        database.get_live_drawdown_summary.return_value = {
            "current_drawdown_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }
        service = RiskManagementService(database=database)

        with (
            mock.patch.object(config, "LEVERAGE", 10),
            mock.patch.object(config.ProductionConfig, "POSITION_SIZING_MODE", "allocation"),
            mock.patch.object(config.ProductionConfig, "POSITION_MARGIN_ALLOCATION_PCT", 100.0),
            mock.patch.object(config.ProductionConfig, "ENFORCE_LIVE_RISK_CAPPED_ALLOCATION", True, create=True),
        ):
            plan = service.build_trade_plan(
                entry_price=62500.0,
                stop_loss_pct=1.5,
                account_balance=25.0,
                risk_per_trade_pct=2.0,
                max_open_trades=1,
                symbol="BTC/USDT",
                timeframe="15m",
                execution_scope="live",
                live_context={
                    "user_id": 0,
                    "account_id": "env-primary",
                    "exchange_name": "binanceusdm",
                },
            )

        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["sizing_mode"], "hybrid")
        self.assertAlmostEqual(plan["position_notional"], 33.33, places=2)
        self.assertAlmostEqual(plan["risk_amount"], 0.5, places=2)
        self.assertAlmostEqual(plan["effective_risk_pct"], 2.0, places=4)

    def test_live_trade_plan_blocks_balance_below_minimum_bankroll(self):
        database = mock.Mock()
        database.get_user_live_portfolio_risk_summary.return_value = {
            "open_trades": 0,
            "total_open_risk_pct": 0.0,
        }
        database.get_daily_live_guardrail_summary.return_value = {
            "closed_trades": 0,
            "realized_pnl_pct": 0.0,
            "consecutive_losses": 0,
        }
        database.get_live_drawdown_summary.return_value = {
            "current_drawdown_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }
        service = RiskManagementService(database=database)

        with mock.patch.object(config.ProductionConfig, "MIN_LIVE_ACCOUNT_BALANCE_USDT", 20.0, create=True):
            plan = service.build_trade_plan(
                entry_price=62500.0,
                stop_loss_pct=1.5,
                account_balance=19.99,
                risk_per_trade_pct=2.0,
                max_open_trades=1,
                symbol="BTC/USDT",
                timeframe="15m",
                execution_scope="live",
                live_context={
                    "user_id": 0,
                    "account_id": "env-primary",
                    "exchange_name": "binanceusdm",
                },
            )

        self.assertFalse(plan["allowed"])
        self.assertIn("Banca minima", plan["reason"])

    def test_run_backtest_includes_account_risk_model_in_summary(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00"),
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                    "volume": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.5,
                    "low": 99.8,
                    "close": 101.0,
                    "volume": 10.0,
                },
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:30:00+00:00"),
                    "open": 101.0,
                    "high": 103.5,
                    "low": 100.5,
                    "close": 103.0,
                    "volume": 10.0,
                },
            ]
        )

        with (
            mock.patch.object(backtest, "calculate_indicators", return_value=df),
            mock.patch.object(backtest, "get_min_required_rows", return_value=0),
            mock.patch.object(backtest, "UnifiedDecisionEngine") as unified_engine_cls,
            mock.patch.object(backtest, "save_detailed_report"),
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.0),
            mock.patch.object(config, "LONG_TAKE_PROFIT_PCT", 2.0),
        ):
            unified_engine = unified_engine_cls.return_value
            unified_engine.learning_service.path = None
            unified_engine.learning_service.reset.return_value = None
            unified_engine.decide_entry.side_effect = [
                {"signal": "hold"},
                {"signal": "buy", "atr": 1.0},
            ]
            unified_engine.should_exit_position.return_value = {"exit": False}
            _, summary = backtest.run_backtest(
                "BTC/USDT",
                "15m",
                candles=len(df),
                fee_pct=config.FEE_PCT,
                testnet=False,
                use_local_csv=True,
                slippage_pct=config.SLIPPAGE_PCT,
                preloaded_df=df,
                execution_profile="native_bracket",
                initial_balance=50.0,
                risk_per_trade_pct=0.25,
            )

        self.assertIn("account_risk_model", summary)
        self.assertAlmostEqual(summary["account_risk_model"]["initial_balance_usdt"], 50.0, places=4)
        self.assertGreater(summary["account_risk_model"]["final_balance_usdt"], 50.0)

    def test_build_trade_diagnostics_reports_streaks_and_giveback(self):
        trades = [
            {
                "side": "short",
                "entry_timestamp": "2026-04-12T00:00:00+00:00",
                "exit_timestamp": "2026-04-12T00:15:00+00:00",
                "reason": "stop_loss",
                "net_pct": -1.38,
                "mfe_pct": 0.2,
            },
            {
                "side": "short",
                "entry_timestamp": "2026-04-12T01:00:00+00:00",
                "exit_timestamp": "2026-04-12T01:15:00+00:00",
                "reason": "stop_loss",
                "net_pct": -1.38,
                "mfe_pct": 1.2,
            },
            {
                "side": "long",
                "entry_timestamp": "2026-04-12T02:00:00+00:00",
                "exit_timestamp": "2026-04-12T02:15:00+00:00",
                "reason": "take_profit",
                "net_pct": 2.72,
                "mfe_pct": 2.9,
            },
            {
                "side": "long",
                "entry_timestamp": "2026-05-12T02:00:00+00:00",
                "exit_timestamp": "2026-05-12T02:15:00+00:00",
                "reason": "take_profit",
                "net_pct": 2.72,
                "mfe_pct": 2.9,
            },
        ]

        diagnostics = backtest.build_trade_diagnostics(trades)

        self.assertEqual(diagnostics["streaks"]["max_loss_streak"], 2)
        self.assertEqual(diagnostics["streaks"]["max_win_streak"], 2)
        self.assertEqual(diagnostics["giveback"]["losers"], 2)
        self.assertEqual(diagnostics["giveback"]["losses_after_1_0pct_profit"], 1)
        self.assertEqual(diagnostics["giveback"]["immediate_failures_mfe_le_0_25"], 1)
        self.assertEqual(diagnostics["reason_breakdown"]["all"]["stop_loss"], 2)
        self.assertEqual(diagnostics["reason_breakdown"]["by_side"]["long"]["take_profit"], 2)
        self.assertEqual(len(diagnostics["phases_by_trade_order"]), 4)
        self.assertEqual(diagnostics["monthly"]["best_months"][0]["month"], "2026-05")

    def test_runtime_recovery_restores_last_candle_position_and_risk_state(self):
        snapshot = config.build_runtime_strategy_snapshot()
        persisted_position = create_position("buy", 100.0, "2026-04-12T00:00:00+00:00", atr=1.0)
        persisted_position["best_price"] = 103.5
        persisted_position["current_stop"] = 100.0
        persisted_position["partial_taken"] = True
        persisted_position["break_even_active"] = True

        runtime_row = {
            "runtime_key": "primary:BTC/USDT:15m",
            "strategy_version": snapshot["strategy_version"],
            "status": "position_open",
            "last_candle_timestamp": "2026-04-12T01:15:00+00:00",
            "state_payload": {
                "risk_state": {
                    "day": "2026-04-12",
                    "daily_realized_pct": -0.4,
                    "consecutive_losses": 2,
                    "blocked": True,
                    "block_reason": "circuit breaker",
                },
                "position": bot_runner._serialize_position(persisted_position),
            },
        }

        with mock.patch.object(bot_runner.db, "get_bot_runtime_state", return_value=[runtime_row]):
            restored_timestamp, restored_position, restored_risk_state = bot_runner._load_runtime_recovery_state(snapshot)

        self.assertIsNotNone(restored_timestamp)
        self.assertEqual(str(restored_timestamp.isoformat()), "2026-04-12T01:15:00+00:00")
        self.assertIsNotNone(restored_position)
        self.assertEqual(restored_position["side"], "long")
        self.assertEqual(restored_position["entry_price"], 100.0)
        self.assertEqual(restored_position["best_price"], 103.5)
        self.assertTrue(restored_position["partial_taken"])
        self.assertTrue(restored_risk_state["blocked"])
        self.assertEqual(restored_risk_state["consecutive_losses"], 2)

    def test_runtime_recovery_upgrades_legacy_live_position_to_native_bracket(self):
        with (
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.0),
            mock.patch.object(config, "LONG_TAKE_PROFIT_PCT", 2.0),
        ):
            legacy_live_position = create_position("buy", 100.0, "2026-04-12T00:00:00+00:00", atr=1.0)
            legacy_live_position["execution_mode"] = "live"
            legacy_live_position["current_stop"] = 97.0
            legacy_live_position.pop("execution_profile", None)
            restored = bot_runner._restore_position(bot_runner._serialize_position(legacy_live_position))

        self.assertIsNotNone(restored)
        self.assertEqual(restored["execution_profile"], "native_bracket")
        self.assertAlmostEqual(restored["current_stop"], 99.0, places=6)
        self.assertAlmostEqual(restored["partial_target"], 102.0, places=6)
        self.assertFalse(restored["partial_taken"])

    def test_runtime_recovery_restores_managed_trailing_fields(self):
        managed_position = create_position("buy", 100.0, "2026-04-12T00:00:00+00:00", atr=1.0)
        managed_position["partial_taken"] = True
        raw_position = bot_runner._serialize_position(managed_position)
        raw_position.pop("trailing_trigger_price", None)
        raw_position["realized_partial_pct"] = None

        restored = bot_runner._restore_position(raw_position)

        self.assertIsNotNone(restored)
        self.assertEqual(restored["execution_profile"], "managed")
        self.assertGreater(restored["trailing_trigger_price"], 100.0)
        self.assertGreater(restored["realized_partial_pct"], 0.0)

    def test_runtime_recovery_rebuilds_setup_specific_managed_profile(self):
        signal_result = {
            "signal": "buy",
            "reason": "teste",
            "atr": 2.0,
            "setup": {
                "setup": "trend_resume_long",
                "direction": "long",
                "regime": {"regime": "trend_bull"},
            },
        }

        with (
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.5),
            mock.patch.object(config, "TREND_RESUME_LONG_STOP_LOSS_PCT", 0.9),
            mock.patch.object(config, "TREND_RESUME_LONG_USE_FIXED_STOP", True),
        ):
            managed_position = bot_runner._build_runtime_position(
                signal="buy",
                entry_price=100.0,
                timestamp="2026-04-12T00:00:00+00:00",
                atr=2.0,
                execution_profile="managed",
                signal_result=signal_result,
            )
            raw_position = bot_runner._serialize_position(managed_position)
            raw_position["initial_stop"] = None
            raw_position["current_stop"] = None
            raw_position["partial_target"] = None
            raw_position["trailing_trigger_price"] = None
            raw_position["trailing_trigger_pct"] = None
            raw_position["trailing_stop_pct"] = None
            raw_position["stop_loss_pct"] = None
            raw_position["partial_target_pct"] = None
            raw_position["management_profile"] = None

            restored = bot_runner._restore_position(raw_position)

        self.assertIsNotNone(restored)
        self.assertEqual(restored["entry_setup"], "trend_resume_long")
        self.assertEqual(restored["management_profile"], "trend_resume_long")
        self.assertAlmostEqual(restored["current_stop"], 99.1, places=6)
        self.assertAlmostEqual(restored["stop_loss_pct"], 0.9, places=6)

    def test_live_native_bracket_closes_by_reconciliation_without_market_exit(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 103.0,
                    "low": 99.8,
                    "close": 102.5,
                    "volume": 10.0,
                }
            ]
        )
        position = create_native_bracket_position(
            "buy",
            100.0,
            pd.Timestamp("2026-04-12T00:00:00+00:00"),
            atr=1.0,
        )
        position["quantity"] = 1.0
        position["execution_mode"] = "live"

        live_execution_service = mock.Mock()
        risk_state = {
            "day": None,
            "daily_realized_pct": 0.0,
            "consecutive_losses": 0,
            "blocked": False,
            "block_reason": "",
        }

        with (
            mock.patch.object(bot_runner, "_live_execution_enabled", return_value=True),
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(bot_runner, "generate_entry_signal", return_value={"signal": "hold", "reason": "none"}),
            mock.patch.object(config, "LONG_STOP_LOSS_PCT", 1.0),
            mock.patch.object(config, "LONG_TAKE_PROFIT_PCT", 2.0),
        ):
            _, restored_position, result = bot_runner._process_closed_candle(
                df=df,
                candle_index=0,
                params=object(),
                posicao_atual=position,
                risk_state=risk_state,
                runtime_snapshot={"strategy_version": "test"},
                live_execution_service=live_execution_service,
                risk_management_service=None,
                live_execution_context={"user_id": 1, "account_id": "acc", "exchange_name": "binanceusdm"},
                runtime_session={
                    "started_at_utc": "2026-04-12T00:00:00+00:00",
                    "started_at_epoch": 0.0,
                    "processed_candles": 0,
                    "actionable_signal_count": 0,
                    "entry_count": 0,
                    "first_actionable_signal_at_utc": None,
                    "first_actionable_candle_timestamp": None,
                    "first_entry_at_utc": None,
                    "first_entry_candle_timestamp": None,
                    "first_entry_delay_sec": None,
                    "last_entry": None,
                    "last_blocked_entry": None,
                },
            )

        self.assertIsNone(restored_position)
        self.assertEqual(result["signal"], "hold")
        live_execution_service.submit_market_order.assert_not_called()
        live_execution_service.reconcile_account_state.assert_called_once()

    def test_live_managed_entry_uses_only_native_stop_order(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.2,
                    "low": 99.7,
                    "close": 100.5,
                    "volume": 10.0,
                }
            ]
        )
        live_execution_service = mock.Mock()
        live_execution_service.submit_market_order.return_value = {
            "price": 100.5,
            "quantity": 0.25,
            "client_order_id": "cid-1",
            "exchange_order_id": "oid-1",
        }
        risk_state = {
            "day": None,
            "daily_realized_pct": 0.0,
            "consecutive_losses": 0,
            "blocked": False,
            "block_reason": "",
        }
        runtime_session = {
            "started_at_utc": "2026-04-12T00:00:00+00:00",
            "started_at_epoch": 0.0,
            "processed_candles": 0,
            "actionable_signal_count": 0,
            "entry_count": 0,
            "first_actionable_signal_at_utc": None,
            "first_actionable_candle_timestamp": None,
            "first_entry_at_utc": None,
            "first_entry_candle_timestamp": None,
            "first_entry_delay_sec": None,
            "last_entry": None,
            "last_blocked_entry": None,
        }

        with (
            mock.patch.object(bot_runner, "_live_execution_enabled", return_value=True),
            mock.patch.object(
                bot_runner,
                "_build_live_entry_plan",
                return_value={
                    "allowed": True,
                    "quantity": 0.25,
                    "account_balance": 50.0,
                    "risk_amount": 0.2,
                    "position_notional": 25.0,
                    "stop_loss_price": 99.0,
                    "take_profit_price": 101.5,
                    "execution_profile": "managed",
                },
            ),
            mock.patch.object(
                bot_runner,
                "generate_entry_signal",
                return_value={"signal": "buy", "reason": "managed_entry", "atr": 1.0, "score": 8.0, "setup": {}},
            ),
            mock.patch.object(config, "EXECUTION_PROFILE", "managed"),
        ):
            _, restored_position, _ = bot_runner._process_closed_candle(
                df=df,
                candle_index=0,
                params=object(),
                posicao_atual=None,
                risk_state=risk_state,
                runtime_snapshot={"strategy_version": "test"},
                live_execution_service=live_execution_service,
                risk_management_service=mock.Mock(),
                live_execution_context={"user_id": 1, "account_id": "acc", "exchange_name": "binanceusdm"},
                runtime_session=runtime_session,
            )

        self.assertIsNotNone(restored_position)
        self.assertEqual(restored_position["execution_profile"], "managed")
        live_execution_service.submit_stop_market_order.assert_called_once()
        live_execution_service.submit_take_profit_market_order.assert_not_called()

    def test_live_managed_partial_executes_reduce_only_and_refreshes_stop(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.5,
                    "low": 99.9,
                    "close": 101.0,
                    "volume": 10.0,
                }
            ]
        )
        position = create_position("buy", 100.0, pd.Timestamp("2026-04-12T00:00:00+00:00"), atr=1.0)
        position.update(
            {
                "quantity": 0.4,
                "execution_mode": "live",
                "execution_profile": "managed",
                "protective_stop_order_id": "stop-1",
                "protective_stop_price": float(position["current_stop"]),
                "planned_position_notional": 40.0,
                "account_reference_balance": 100.0,
                "live_partial_realized_pct_accounted": 0.0,
                "strategy_version": "test",
            }
        )
        managed_position = dict(position)
        managed_position.update(
            {
                "partial_taken": True,
                "break_even_active": True,
                "current_stop": 100.2,
                "realized_partial_pct": 0.55,
            }
        )
        live_execution_service = mock.Mock()
        live_execution_service.reconcile_account_state.return_value = {"ok": True}
        live_execution_service.submit_market_order.return_value = {
            "price": 101.0,
            "quantity": 0.2,
            "exchange_order_id": "partial-1",
        }
        live_execution_service.replace_stop_market_order.return_value = {
            "exchange_order_id": "stop-2",
            "client_order_id": "cid-stop-2",
            "stop_price": 100.2,
        }
        risk_state = {
            "day": None,
            "daily_realized_pct": 0.0,
            "consecutive_losses": 0,
            "blocked": False,
            "block_reason": "",
        }

        with (
            mock.patch.object(bot_runner, "_live_execution_enabled", return_value=True),
            mock.patch.object(
                bot_runner,
                "evaluate_managed_position_on_candle",
                return_value={"action": "hold", "position": managed_position, "realized_partial_pct": 0.55},
            ),
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[{"symbol": config.SYMBOL, "status": "open"}]),
            mock.patch.object(bot_runner, "generate_entry_signal", return_value={"signal": "hold", "reason": "none"}),
            mock.patch.object(bot_runner.db, "save_user_execution_event", return_value=1),
        ):
            _, restored_position, _ = bot_runner._process_closed_candle(
                df=df,
                candle_index=0,
                params=object(),
                posicao_atual=position,
                risk_state=risk_state,
                runtime_snapshot={"strategy_version": "test"},
                live_execution_service=live_execution_service,
                risk_management_service=mock.Mock(),
                live_execution_context={"user_id": 1, "account_id": "acc", "exchange_name": "binanceusdm"},
                runtime_session={
                    "started_at_utc": "2026-04-12T00:00:00+00:00",
                    "started_at_epoch": 0.0,
                    "processed_candles": 0,
                    "actionable_signal_count": 0,
                    "entry_count": 0,
                    "first_actionable_signal_at_utc": None,
                    "first_actionable_candle_timestamp": None,
                    "first_entry_at_utc": None,
                    "first_entry_candle_timestamp": None,
                    "first_entry_delay_sec": None,
                    "last_entry": None,
                    "last_blocked_entry": None,
                },
            )

        self.assertIsNotNone(restored_position)
        self.assertAlmostEqual(float(restored_position["quantity"]), 0.2, places=6)
        self.assertGreater(float(restored_position["live_partial_realized_pct_accounted"]), 0.0)
        live_execution_service.submit_market_order.assert_called_once()
        live_execution_service.replace_stop_market_order.assert_called_once()
        self.assertGreater(risk_state["daily_realized_pct"], 0.0)

    def test_live_partial_skips_reduce_only_when_half_quantity_is_below_step(self):
        is_operable, details = bot_runner._is_reduce_only_quantity_operable(
            quantity=0.0005,
            reference_price=62500.0,
            trading_rules={"min_qty": 0.001, "min_notional": 100.0, "qty_step": 0.001},
        )

        self.assertFalse(is_operable)
        self.assertEqual(details["rounded_quantity"], 0.0)
        self.assertEqual(details["reason"], "quantidade_reduce_only_zerada_apos_arredondamento")

    def test_runtime_logs_actionable_signal_ignored_when_position_already_open(self):
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00"),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.8,
                    "volume": 10.0,
                }
            ]
        )
        position = create_position("buy", 100.0, pd.Timestamp("2026-04-12T00:00:00+00:00"), atr=1.0)
        risk_state = {
            "day": None,
            "daily_realized_pct": 0.0,
            "consecutive_losses": 0,
            "blocked": False,
            "block_reason": "",
        }
        runtime_session = {
            "started_at_utc": "2026-04-12T00:00:00+00:00",
            "started_at_epoch": 0.0,
            "processed_candles": 0,
            "actionable_signal_count": 0,
            "entry_count": 0,
            "first_actionable_signal_at_utc": None,
            "first_actionable_candle_timestamp": None,
            "first_entry_at_utc": None,
            "first_entry_candle_timestamp": None,
            "first_entry_delay_sec": None,
            "last_entry": None,
            "last_blocked_entry": None,
            "ignored_actionable_signal_count": 0,
            "last_ignored_actionable_signal": None,
        }

        with (
            mock.patch.object(bot_runner, "_live_execution_enabled", return_value=False),
            mock.patch.object(
                bot_runner,
                "evaluate_managed_position_on_candle",
                return_value={"action": "hold", "position": dict(position), "realized_partial_pct": 0.0},
            ),
            mock.patch.object(
                bot_runner,
                "generate_entry_signal",
                return_value={
                    "signal": "sell",
                    "reason": "short_score=8",
                    "score": 8.0,
                    "atr": 1.0,
                    "setup": {"setup": "trend_resume_short"},
                },
            ),
            mock.patch.object(bot_runner.db, "upsert_bot_runtime_state"),
            mock.patch.object(bot_runner, "log_info") as log_info_mock,
        ):
            _, restored_position, result = bot_runner._process_closed_candle(
                df=df,
                candle_index=0,
                params=object(),
                posicao_atual=position,
                risk_state=risk_state,
                runtime_snapshot={"strategy_version": "test"},
                live_execution_service=None,
                risk_management_service=None,
                live_execution_context=None,
                runtime_session=runtime_session,
            )

        self.assertIsNotNone(restored_position)
        self.assertEqual(result["signal"], "sell")
        self.assertEqual(runtime_session["entry_count"], 0)
        self.assertEqual(runtime_session["ignored_actionable_signal_count"], 1)
        self.assertEqual(runtime_session["last_ignored_actionable_signal"]["signal"], "sell")
        self.assertEqual(runtime_session["last_ignored_actionable_signal"]["setup_name"], "trend_resume_short")
        self.assertEqual(runtime_session["last_ignored_actionable_signal"]["stage"], "position_already_open")
        self.assertEqual(
            runtime_session["last_ignored_actionable_signal"]["conflict_type"],
            "opposite_direction_position_open",
        )
        self.assertTrue(
            any(
                call.args
                and "Sinal acionavel ignorado: posicao ja aberta" in str(call.args[0])
                and "conflito=direcao_oposta" in str(call.args[0])
                for call in log_info_mock.call_args_list
            )
        )

    def test_runtime_recovery_returns_clean_defaults_when_no_state_exists(self):
        snapshot = config.build_runtime_strategy_snapshot()
        with mock.patch.object(bot_runner.db, "get_bot_runtime_state", return_value=[]):
            restored_timestamp, restored_position, restored_risk_state = bot_runner._load_runtime_recovery_state(snapshot)

        self.assertIsNone(restored_timestamp)
        self.assertIsNone(restored_position)
        self.assertFalse(restored_risk_state["blocked"])
        self.assertEqual(restored_risk_state["daily_realized_pct"], 0.0)

    def test_runtime_market_data_limit_covers_indicator_warmup(self):
        params = object()
        with (
            mock.patch.object(bot_runner, "get_min_required_rows", return_value=260),
            mock.patch.object(config, "LIMIT", 200),
            mock.patch.object(config, "BOT_BOOTSTRAP_CANDLES", 220),
        ):
            limit = bot_runner._resolve_runtime_market_data_limit(params)

        self.assertEqual(limit, 300)

    def test_runtime_feed_validation_blocks_missing_websocket_dependency(self):
        with self.assertRaises(RuntimeError):
            bot_runner._validate_stream_runtime_ready(
                {
                    "provider": "rest_fallback:none",
                    "connected": False,
                    "last_error": "Pacote websockets nao instalado; usando REST.",
                }
            )

    def test_get_pending_candle_indexes_returns_all_candles_after_last_processed(self):
        df = pd.DataFrame(
            [
                {"timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:30:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:45:00+00:00")},
            ]
        )

        pending_indexes = bot_runner._get_pending_candle_indexes(
            df,
            pd.Timestamp("2026-04-12T00:15:00+00:00"),
        )

        self.assertEqual(pending_indexes, [2, 3])

    def test_get_pending_candle_indexes_returns_empty_without_previous_timestamp(self):
        df = pd.DataFrame(
            [
                {"timestamp": pd.Timestamp("2026-04-12T00:00:00+00:00")},
                {"timestamp": pd.Timestamp("2026-04-12T00:15:00+00:00")},
            ]
        )

        pending_indexes = bot_runner._get_pending_candle_indexes(df, None)

        self.assertEqual(pending_indexes, [])

    def test_load_backtest_websocket_db_uses_persisted_candles(self):
        coverage = {
            "total": 480,
            "first_timestamp": "2026-04-08T00:00:00+00:00",
            "last_timestamp": "2026-04-13T00:00:00+00:00",
        }
        timestamps = pd.date_range("2026-04-08T00:00:00+00:00", periods=480, freq="15min", tz="UTC")
        rows = []
        for idx, ts in enumerate(timestamps):
            base = 100.0 + (idx * 0.1)
            rows.append(
                {
                    "candle_timestamp": ts.isoformat(),
                    "open": base,
                    "high": base + 1.0,
                    "low": base - 1.0,
                    "close": base + 0.5,
                    "volume": 10.0 + idx,
                }
            )

        with mock.patch.object(backtest.db, "get_backtest_websocket_candle_coverage", return_value=coverage):
            with mock.patch.object(backtest.db, "get_backtest_websocket_candles", return_value=rows):
                df, loaded_coverage = backtest.load_backtest_websocket_db(
                    symbol="BTC/USDT",
                    timeframe="15m",
                    candles=420,
                    days=2,
                )

        self.assertEqual(int(loaded_coverage["total"]), 480)
        self.assertEqual(len(df), 480)
        self.assertGreater(float(df["close"].iloc[-1]), float(df["close"].iloc[0]))

    def test_load_backtest_websocket_db_raises_when_no_history_exists(self):
        with mock.patch.object(
            backtest.db,
            "get_backtest_websocket_candle_coverage",
            return_value={"total": 0, "first_timestamp": None, "last_timestamp": None},
        ):
            with self.assertRaises(RuntimeError):
                backtest.load_backtest_websocket_db(
                    symbol="BTC/USDT",
                    timeframe="15m",
                    candles=3000,
                    days=30,
                )

    def test_runtime_paper_close_result_applies_fee_model(self):
        position = {
            "side": "long",
            "entry_price": 100.0,
            "planned_position_notional": 250.0,
        }
        closed_trade = {
            "exit_price": 101.0,
            "exit_timestamp": "2026-04-14T12:00:00+00:00",
        }

        result = bot_runner._runtime_paper_close_result(position, closed_trade)

        self.assertEqual(result["outcome"], "WIN")
        self.assertLess(result["result_pct"], 1.0)
        self.assertEqual(result["exit_price"], 101.0)
        self.assertGreater(result["result_usdt"], 0.0)

    def test_runtime_paper_close_result_uses_managed_gross_pct_after_partial(self):
        position = {
            "side": "long",
            "entry_price": 100.0,
            "planned_position_notional": 500.0,
        }
        closed_trade = {
            "exit_price": 101.184,
            "exit_timestamp": "2026-04-14T12:00:00+00:00",
            "gross_pct": 1.092,
        }

        result = bot_runner._runtime_paper_close_result(position, closed_trade)

        self.assertEqual(result["outcome"], "WIN")
        self.assertAlmostEqual(result["result_pct"], 0.9311, places=4)
        self.assertAlmostEqual(result["result_usdt"], 4.6553, places=4)

    def test_build_runtime_paper_position_metrics_uses_current_equity(self):
        position = create_position("buy", 100.0, pd.Timestamp("2026-04-12T00:00:00+00:00"), atr=1.0)
        snapshot = {"strategy_version": "test"}

        with mock.patch.object(
            bot_runner.db,
            "get_paper_drawdown_summary",
            return_value={"current_equity": 12345.67},
        ):
            metrics = bot_runner._build_runtime_paper_position_metrics(position, snapshot)

        self.assertEqual(metrics["account_reference_balance"], 12345.67)
        self.assertGreater(metrics["planned_position_notional"], 0.0)
        self.assertGreater(metrics["planned_quantity"], 0.0)
        self.assertGreater(metrics["risk_amount"], 0.0)

    def test_attach_runtime_open_paper_trade_reuses_existing_open_trade(self):
        snapshot = config.build_runtime_strategy_snapshot()
        position = {"side": "long", "entry_price": 100.0}

        with mock.patch.object(bot_runner, "_paper_tracking_enabled", return_value=True):
            with mock.patch.object(
                bot_runner.db,
                "get_open_paper_trades",
                return_value=[{"id": 77, "symbol": "BTC/USDT", "timeframe": "15m"}],
            ):
                resolved = bot_runner._attach_runtime_open_paper_trade(position, snapshot)

        self.assertEqual(resolved["paper_trade_id"], 77)

    def test_create_runtime_paper_trade_flattens_setup_payload(self):
        snapshot = config.build_runtime_strategy_snapshot()
        position = create_native_bracket_position("sell", 100.0, pd.Timestamp("2026-04-12T00:00:00+00:00"), atr=1.0)
        position["execution_mode"] = "paper"
        position["planned_risk_pct"] = 0.25
        position["risk_amount"] = 25.0
        position["planned_position_notional"] = 2083.33
        position["planned_quantity"] = 20.8333
        position["account_reference_balance"] = 10000.0
        position["risk_mode"] = "normal"
        position["size_reduced"] = False
        position["risk_reason"] = ""
        signal_result = {
            "signal": "sell",
            "reason": "short_score=8",
            "score": 8.0,
            "atr": 1.2,
            "setup": {
                "setup": "pullback_short",
                "direction": "short",
                "regime": {"regime": "trend_bear"},
            },
        }

        with mock.patch.object(bot_runner, "_paper_tracking_enabled", return_value=True):
            with mock.patch.object(bot_runner.db, "create_paper_trade", return_value=91) as create_trade_mock:
                trade_id = bot_runner._create_runtime_paper_trade(position, signal_result, snapshot)

        self.assertEqual(trade_id, 91)
        payload = create_trade_mock.call_args.args[0]
        self.assertEqual(payload["setup_name"], "pullback_short")
        self.assertEqual(payload["regime"], "trend_bear")
        self.assertEqual(payload["planned_risk_pct"], 0.25)
        self.assertEqual(payload["planned_risk_amount"], 25.0)
        self.assertEqual(payload["planned_position_notional"], 2083.33)
        self.assertEqual(payload["planned_quantity"], 20.8333)
        self.assertEqual(payload["account_reference_balance"], 10000.0)

    def test_single_user_execution_context_uses_config_defaults(self):
        with mock.patch.object(config, "SINGLE_USER_RUNTIME_USER_ID", 9):
            with mock.patch.object(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "env-main"):
                with mock.patch.object(config, "SINGLE_USER_RUNTIME_ACCOUNT_ALIAS", "Conta Runner"):
                    with mock.patch.object(config, "SINGLE_USER_RUNTIME_EXCHANGE", "binanceusdm"):
                        with mock.patch.object(config, "TESTNET", True):
                            context = bot_runner._build_single_user_execution_context()

        self.assertEqual(context["user_id"], 9)
        self.assertEqual(context["account_id"], "env-main-testnet")
        self.assertEqual(context["account_alias"], "Conta Runner")
        self.assertEqual(context["exchange_name"], "binanceusdm")
        self.assertTrue(context["paper_enabled"])
        self.assertTrue(context["use_env_credentials"])

    def test_prepare_live_execution_runtime_clears_stale_local_position_when_exchange_has_none(self):
        snapshot = config.build_runtime_strategy_snapshot()
        recovered_position = create_position("buy", 100.0, "2026-04-12T00:00:00+00:00", atr=1.0)
        service = mock.Mock()
        service.validate_account_connection.return_value = {"ok": True}
        service.reconcile_account_state.return_value = {"ok": True}
        user_stream = mock.Mock()
        user_stream.wait_until_ready.return_value = True
        service.start_user_data_stream.return_value = user_stream
        context = {
            "user_id": 0,
            "account_id": "env-primary",
            "account_alias": "Runner",
            "exchange_name": "binanceusdm",
            "exchange": "binanceusdm",
            "use_env_credentials": True,
            "credential_source": "env",
        }

        with mock.patch.object(bot_runner, "_build_single_user_execution_context", return_value=context):
            with mock.patch.object(bot_runner.db, "get_user_live_positions", return_value=[]):
                with mock.patch.object(bot_runner.db, "save_user_execution_event", return_value=1):
                    resolved_context, resolved_position, resolved_stream = bot_runner._prepare_live_execution_runtime(
                        snapshot=snapshot,
                        execution_service=service,
                        recovered_position=recovered_position,
                    )

        self.assertEqual(resolved_context["account_id"], "env-primary")
        self.assertIsNone(resolved_position)
        self.assertIs(resolved_stream, user_stream)

    def test_prepare_live_execution_runtime_blocks_unknown_exchange_position(self):
        snapshot = config.build_runtime_strategy_snapshot()
        service = mock.Mock()
        service.validate_account_connection.return_value = {"ok": True}
        service.reconcile_account_state.return_value = {"ok": True}
        context = {
            "user_id": 0,
            "account_id": "env-primary",
            "account_alias": "Runner",
            "exchange_name": "binanceusdm",
            "exchange": "binanceusdm",
            "use_env_credentials": True,
            "credential_source": "env",
        }
        open_exchange_position = [
            {
                "symbol": config.SYMBOL,
                "exchange": "binanceusdm",
                "side": "long",
                "quantity": 0.01,
                "status": "open",
            }
        ]

        with mock.patch.object(bot_runner, "_build_single_user_execution_context", return_value=context):
            with mock.patch.object(bot_runner.db, "get_user_live_positions", return_value=open_exchange_position):
                with self.assertRaises(RuntimeError):
                    bot_runner._prepare_live_execution_runtime(
                        snapshot=snapshot,
                        execution_service=service,
                        recovered_position=None,
                    )

    def test_order_value_balance_22_uses_notional_and_margin_not_balance_as_margin(self):
        service = RiskManagementService()

        with mock.patch.object(config, "ORDER_VALUE_RISK_CAP", True):
            sizing = service.calculate_position_size(
                account_balance=22.0,
                entry_price=10000.0,
                stop_loss_pct=1.1,
                risk_pct=4.0,
                leverage=10,
                sizing_mode="order_value",
                margin_allocation_pct=100.0,
            )

        self.assertEqual(sizing["sizing_mode"], "order_value")
        self.assertAlmostEqual(sizing["requested_order_notional"], 22.0, places=2)
        self.assertAlmostEqual(sizing["final_order_notional"], 22.0, places=2)
        self.assertAlmostEqual(sizing["position_notional"], 22.0, places=2)
        self.assertAlmostEqual(sizing["required_margin"], 2.2, places=2)
        self.assertFalse(sizing["risk_cap_applied"])

    def test_order_value_risk_cap_reduces_notional_when_stop_exceeds_risk_limit(self):
        service = RiskManagementService()

        with mock.patch.object(config, "ORDER_VALUE_RISK_CAP", True):
            sizing = service.calculate_position_size(
                account_balance=22.0,
                entry_price=10000.0,
                stop_loss_pct=10.0,
                risk_pct=4.0,
                leverage=10,
                sizing_mode="order_value",
                margin_allocation_pct=100.0,
            )

        self.assertAlmostEqual(sizing["requested_order_notional"], 22.0, places=2)
        self.assertAlmostEqual(sizing["final_order_notional"], 8.8, places=2)
        self.assertAlmostEqual(sizing["required_margin"], 0.88, places=2)
        self.assertTrue(sizing["risk_cap_applied"])
        self.assertEqual(sizing["risk_reason"], "order_value_risk_cap_applied")

    def test_backtest_parser_accepts_order_value_sizing_options(self):
        parser = backtest.build_arg_parser()

        args = parser.parse_args([
            "--position-sizing-mode",
            "order_value",
            "--order-balance-usage-pct",
            "100",
            "--output-dir",
            "reports/backtests/test",
        ])

        self.assertEqual(args.position_sizing_mode, "order_value")
        self.assertEqual(args.order_balance_usage_pct, 100.0)
        self.assertEqual(args.output_dir, "reports/backtests/test")

    def test_structural_stop_long_stays_below_structural_low(self):
        candles = pd.DataFrame(
            {
                "open": [101.0, 100.5, 100.0],
                "high": [102.0, 101.0, 100.5],
                "low": [99.0, 98.0, 97.5],
                "close": [100.5, 100.0, 100.0],
                "volume": [1000.0, 1000.0, 1000.0],
            }
        )

        with (
            mock.patch.object(config, "USE_STRUCTURAL_STOP", True),
            mock.patch.object(config, "STRUCTURAL_STOP_LOOKBACK", 3),
            mock.patch.object(config, "STRUCTURAL_STOP_ATR_BUFFER_MULT", 0.25),
            mock.patch.object(config, "STRUCTURAL_STOP_MIN_BUFFER_PCT", 0.10),
        ):
            position = create_position(
                signal="buy",
                entry_price=100.0,
                timestamp="2026-01-01T00:00:00Z",
                atr=1.0,
                candle_window=candles,
                entry_setup="trend_resume_long",
            )

        self.assertLess(position["initial_stop"], candles["low"].min())

    def test_structural_stop_short_stays_above_structural_high(self):
        candles = pd.DataFrame(
            {
                "open": [99.0, 99.5, 100.0],
                "high": [101.0, 102.0, 102.5],
                "low": [98.0, 99.0, 99.5],
                "close": [99.5, 100.0, 100.0],
                "volume": [1000.0, 1000.0, 1000.0],
            }
        )

        with (
            mock.patch.object(config, "USE_STRUCTURAL_STOP", True),
            mock.patch.object(config, "STRUCTURAL_STOP_LOOKBACK", 3),
            mock.patch.object(config, "STRUCTURAL_STOP_ATR_BUFFER_MULT", 0.25),
            mock.patch.object(config, "STRUCTURAL_STOP_MIN_BUFFER_PCT", 0.10),
        ):
            position = create_position(
                signal="sell",
                entry_price=100.0,
                timestamp="2026-01-01T00:00:00Z",
                atr=1.0,
                candle_window=candles,
                entry_setup="trend_resume_short",
            )

        self.assertGreater(position["initial_stop"], candles["high"].max())

    def test_short_below_min_score_does_not_sell_when_score_gate_enabled(self):
        df = self._build_liquidity_sweep_df("short", recover=True)
        df = df.copy()
        df["ema_trend"] = df["close"] + 2.0
        setup = {"setup": "trend_resume_short", "direction": "short", "regime": {"regime": "trend_bear"}}

        with (
            mock.patch.object(strategy_engine, "get_min_required_rows", return_value=0),
            mock.patch.object(strategy_engine, "detect_setup", return_value=setup),
            mock.patch.object(config, "DISABLE_SHORT_SCORE_GATE", False),
            mock.patch.object(config, "MIN_SHORT_SCORE", 99),
            mock.patch.object(config, "MARKET_STRUCTURE_GUARD_ENABLED", False),
            mock.patch.object(config, "USE_ENTRY_HOUR_BLOCKS", False),
        ):
            result = generate_entry_signal(df, StrategyParams(), index=-1)

        self.assertEqual(result["signal"], "hold")
        self.assertIn("short_score_baixo", result["reason"])

    def test_resolve_trade_stop_pct_prefers_real_initial_stop(self):
        trade = {"side": "long", "entry_price": 100.0, "initial_stop": 97.5}

        self.assertAlmostEqual(backtest._resolve_trade_stop_pct(trade), 2.5, places=4)

    def test_equity_curve_uses_order_value_sizing_and_real_stop(self):
        trades = [
            {
                "side": "long",
                "entry_price": 100.0,
                "initial_stop": 95.0,
                "net_pct": 10.0,
                "exit_timestamp": "2026-01-01T00:15:00Z",
            }
        ]

        with mock.patch.object(config, "ORDER_VALUE_RISK_CAP", True):
            rows = backtest._build_equity_curve_rows(
                trades,
                22.0,
                risk_per_trade_pct=4.0,
                leverage=10.0,
                position_sizing_mode="order_value",
                position_margin_allocation_pct=100.0,
            )

        self.assertAlmostEqual(rows[1]["position_notional"], 17.6, places=2)
        self.assertAlmostEqual(rows[1]["pnl_usdt"], 1.76, places=2)
        self.assertAlmostEqual(rows[1]["equity"], 23.76, places=2)
        self.assertTrue(rows[1]["risk_cap_applied"])

    def test_account_model_name_maps_order_value_and_hybrid(self):
        self.assertEqual(backtest._resolve_account_model_name("order_value"), "order_value_notional")
        self.assertEqual(backtest._resolve_account_model_name("hybrid"), "risk_capped_by_margin_allocation")


class SymbolGovernanceTests(unittest.TestCase):
    def test_symbol_validation_record_reads_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            approval_path = os.path.join(tmpdir, "symbol_approvals.json")
            payload = {
                "symbols": {
                    "BTC/USDT": {
                        "status": "approved",
                        "approval_label": "approved",
                        "reason": "ok",
                    }
                }
            }
            with open(approval_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            record = config.get_symbol_validation_record("BTC/USDT", approvals_path=approval_path)
            is_approved = config.is_symbol_runtime_approved("BTC/USDT", approvals_path=approval_path)

        self.assertEqual(record["status"], "approved")
        self.assertTrue(is_approved)

    def test_runtime_symbol_approval_blocks_rejected_symbol(self):
        record = {
            "status": "rejected",
            "reason": "edge insuficiente",
        }
        with mock.patch.object(config, "RUNTIME_REQUIRE_APPROVED_SYMBOL", True):
            with mock.patch.object(config, "SYMBOL", "SOL/USDT"):
                with mock.patch.object(config, "get_symbol_validation_record", return_value=record):
                    with mock.patch.object(config, "is_symbol_runtime_approved", return_value=False):
                        with self.assertRaises(RuntimeError) as ctx:
                            bot_runner._validate_runtime_symbol_approval()

        self.assertIn("SOL/USDT", str(ctx.exception))
        self.assertIn("edge insuficiente", str(ctx.exception))

    def test_runtime_symbol_approval_accepts_approved_symbol(self):
        record = {
            "status": "approved",
            "approval_label": "approved",
            "reason": "ok",
        }
        with mock.patch.object(config, "RUNTIME_REQUIRE_APPROVED_SYMBOL", True):
            with mock.patch.object(config, "SYMBOL", "BTC/USDT"):
                with mock.patch.object(config, "get_symbol_validation_record", return_value=record):
                    with mock.patch.object(config, "is_symbol_runtime_approved", return_value=True):
                        bot_runner._validate_runtime_symbol_approval()

    def test_apply_symbol_strategy_overrides_updates_runtime_constants(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            overrides_path = os.path.join(tmpdir, "symbol_strategy_overrides.json")
            payload = {
                "symbols": {
                    "ETH/USDT": {
                        "status": "tuned",
                        "recommended_timeframe": "1h",
                        "overrides": {
                            "MIN_LONG_SCORE": 6,
                            "GLOBAL_MIN_ATR_PCT": 0.1,
                            "ENABLE_LONG_RESUME": False,
                            "ENABLE_SHORT_PULLBACK": False,
                            "DISABLE_SHORT_SCORE_GATE": False,
                            "EXPERIMENTAL_LONG_SIDE_LOGIC": True,
                            "SHORT_TREND_GAP_PCT": 0.8,
                            "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT": 0.7,
                            "SHORT_PULLBACK_MIN_ADX": 60.0,
                            "EXPERIMENTAL_SHORT_SIDE_LOGIC": True,
                            "LONG_RESUME_HOT_CONTEXT_GAP_PCT": 0.8,
                            "LONG_PULLBACK_AS_RESUME_WHEN_DISABLED": True,
                            "PULLBACK_LONG_MIN_ADX": 38.0,
                            "PULLBACK_LONG_MAX_CONTEXT_GAP_PCT": 0.75,
                            "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT": 0.9,
                            "TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT": 0.82,
                            "TREND_RESUME_LONG_MIN_ADX": 28.0,
                            "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT": 0.3,
                            "TREND_RESUME_LONG_MAX_RSI": 78.0,
                            "TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE": True,
                            "TREND_RESUME_LONG_STOP_LOSS_PCT": 0.7,
                            "TREND_RESUME_LONG_PARTIAL_TARGET_PCT": 1.2,
                            "TREND_RESUME_LONG_TRAILING_TRIGGER_PCT": 1.0,
                            "TREND_RESUME_LONG_TRAILING_STOP_PCT": 0.4,
                            "TREND_RESUME_LONG_USE_FIXED_STOP": True,
                            "TREND_RESUME_SHORT_STOP_LOSS_PCT": 1.7,
                            "TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT": 0.6,
                            "TREND_RESUME_SHORT_MIN_ADX": 42.0,
                            "TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION": True,
                        },
                    }
                }
            }
            with open(overrides_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            original_long_score = config.MIN_LONG_SCORE
            original_global_atr = config.GLOBAL_MIN_ATR_PCT
            original_long_resume = config.ENABLE_LONG_RESUME
            original_short_pullback = config.ENABLE_SHORT_PULLBACK
            original_disable_short_score_gate = config.DISABLE_SHORT_SCORE_GATE
            original_experimental_long_side_logic = config.EXPERIMENTAL_LONG_SIDE_LOGIC
            original_short_trend_gap_pct = config.SHORT_TREND_GAP_PCT
            original_short_pullback_min_context_gap_pct = config.SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT
            original_short_pullback_min_adx = config.SHORT_PULLBACK_MIN_ADX
            original_experimental_short_side_logic = config.EXPERIMENTAL_SHORT_SIDE_LOGIC
            original_long_resume_hot_context_gap_pct = config.LONG_RESUME_HOT_CONTEXT_GAP_PCT
            original_long_pullback_as_resume = config.LONG_PULLBACK_AS_RESUME_WHEN_DISABLED
            original_pullback_long_min_adx = config.PULLBACK_LONG_MIN_ADX
            original_pullback_long_max_context_gap = config.PULLBACK_LONG_MAX_CONTEXT_GAP_PCT
            original_resume_long_context_gap = config.TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT
            original_resume_long_max_context_gap = config.TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT
            original_resume_long_min_adx = config.TREND_RESUME_LONG_MIN_ADX
            original_resume_long_min_trend_strength = config.TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT
            original_resume_long_max_rsi = config.TREND_RESUME_LONG_MAX_RSI
            original_resume_long_require_prev_close = config.TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE
            original_resume_long_stop = config.TREND_RESUME_LONG_STOP_LOSS_PCT
            original_resume_long_partial_target = config.TREND_RESUME_LONG_PARTIAL_TARGET_PCT
            original_resume_long_trailing_trigger = config.TREND_RESUME_LONG_TRAILING_TRIGGER_PCT
            original_resume_long_trailing_stop = config.TREND_RESUME_LONG_TRAILING_STOP_PCT
            original_resume_long_use_fixed_stop = config.TREND_RESUME_LONG_USE_FIXED_STOP
            original_resume_short_stop = config.TREND_RESUME_SHORT_STOP_LOSS_PCT
            original_resume_short_min_context_gap = config.TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT
            original_resume_short_min_adx = config.TREND_RESUME_SHORT_MIN_ADX
            original_resume_short_require_breakdown = config.TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION
            original_timeframe = config.TIMEFRAME
            try:
                report = config.apply_symbol_strategy_overrides("ETH/USDT", overrides_path=overrides_path)
                self.assertEqual(report["applied"]["TIMEFRAME"], "1h")
                self.assertEqual(report["applied"]["MIN_LONG_SCORE"], 6)
                self.assertEqual(report["applied"]["GLOBAL_MIN_ATR_PCT"], 0.1)
                self.assertFalse(report["applied"]["ENABLE_LONG_RESUME"])
                self.assertFalse(report["applied"]["ENABLE_SHORT_PULLBACK"])
                self.assertFalse(report["applied"]["DISABLE_SHORT_SCORE_GATE"])
                self.assertTrue(report["applied"]["EXPERIMENTAL_LONG_SIDE_LOGIC"])
                self.assertEqual(report["applied"]["SHORT_TREND_GAP_PCT"], 0.8)
                self.assertEqual(report["applied"]["SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT"], 0.7)
                self.assertEqual(report["applied"]["SHORT_PULLBACK_MIN_ADX"], 60.0)
                self.assertTrue(report["applied"]["EXPERIMENTAL_SHORT_SIDE_LOGIC"])
                self.assertEqual(report["applied"]["LONG_RESUME_HOT_CONTEXT_GAP_PCT"], 0.8)
                self.assertTrue(report["applied"]["LONG_PULLBACK_AS_RESUME_WHEN_DISABLED"])
                self.assertEqual(report["applied"]["PULLBACK_LONG_MIN_ADX"], 38.0)
                self.assertEqual(report["applied"]["PULLBACK_LONG_MAX_CONTEXT_GAP_PCT"], 0.75)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT"], 0.9)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT"], 0.82)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_MIN_ADX"], 28.0)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT"], 0.3)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_MAX_RSI"], 78.0)
                self.assertTrue(report["applied"]["TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE"])
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_STOP_LOSS_PCT"], 0.7)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_PARTIAL_TARGET_PCT"], 1.2)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_TRAILING_TRIGGER_PCT"], 1.0)
                self.assertEqual(report["applied"]["TREND_RESUME_LONG_TRAILING_STOP_PCT"], 0.4)
                self.assertTrue(report["applied"]["TREND_RESUME_LONG_USE_FIXED_STOP"])
                self.assertEqual(report["applied"]["TREND_RESUME_SHORT_STOP_LOSS_PCT"], 1.7)
                self.assertEqual(report["applied"]["TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT"], 0.6)
                self.assertEqual(report["applied"]["TREND_RESUME_SHORT_MIN_ADX"], 42.0)
                self.assertTrue(report["applied"]["TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION"])
                self.assertEqual(config.TIMEFRAME, "1h")
                self.assertEqual(config.MIN_LONG_SCORE, 6)
                self.assertEqual(config.GLOBAL_MIN_ATR_PCT, 0.1)
                self.assertFalse(config.ENABLE_LONG_RESUME)
                self.assertFalse(config.ENABLE_SHORT_PULLBACK)
                self.assertFalse(config.DISABLE_SHORT_SCORE_GATE)
                self.assertTrue(config.EXPERIMENTAL_LONG_SIDE_LOGIC)
                self.assertEqual(config.SHORT_TREND_GAP_PCT, 0.8)
                self.assertEqual(config.SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT, 0.7)
                self.assertEqual(config.SHORT_PULLBACK_MIN_ADX, 60.0)
                self.assertTrue(config.EXPERIMENTAL_SHORT_SIDE_LOGIC)
                self.assertEqual(config.LONG_RESUME_HOT_CONTEXT_GAP_PCT, 0.8)
                self.assertTrue(config.LONG_PULLBACK_AS_RESUME_WHEN_DISABLED)
                self.assertEqual(config.PULLBACK_LONG_MIN_ADX, 38.0)
                self.assertEqual(config.PULLBACK_LONG_MAX_CONTEXT_GAP_PCT, 0.75)
                self.assertEqual(config.TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT, 0.9)
                self.assertEqual(config.TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT, 0.82)
                self.assertEqual(config.TREND_RESUME_LONG_MIN_ADX, 28.0)
                self.assertEqual(config.TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT, 0.3)
                self.assertEqual(config.TREND_RESUME_LONG_MAX_RSI, 78.0)
                self.assertTrue(config.TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE)
                self.assertEqual(config.TREND_RESUME_LONG_STOP_LOSS_PCT, 0.7)
                self.assertEqual(config.TREND_RESUME_LONG_PARTIAL_TARGET_PCT, 1.2)
                self.assertEqual(config.TREND_RESUME_LONG_TRAILING_TRIGGER_PCT, 1.0)
                self.assertEqual(config.TREND_RESUME_LONG_TRAILING_STOP_PCT, 0.4)
                self.assertTrue(config.TREND_RESUME_LONG_USE_FIXED_STOP)
                self.assertEqual(config.TREND_RESUME_SHORT_STOP_LOSS_PCT, 1.7)
                self.assertEqual(config.TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT, 0.6)
                self.assertEqual(config.TREND_RESUME_SHORT_MIN_ADX, 42.0)
                self.assertTrue(config.TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION)
            finally:
                config.MIN_LONG_SCORE = original_long_score
                config.GLOBAL_MIN_ATR_PCT = original_global_atr
                config.ENABLE_LONG_RESUME = original_long_resume
                config.ENABLE_SHORT_PULLBACK = original_short_pullback
                config.DISABLE_SHORT_SCORE_GATE = original_disable_short_score_gate
                config.EXPERIMENTAL_LONG_SIDE_LOGIC = original_experimental_long_side_logic
                config.SHORT_TREND_GAP_PCT = original_short_trend_gap_pct
                config.SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT = original_short_pullback_min_context_gap_pct
                config.SHORT_PULLBACK_MIN_ADX = original_short_pullback_min_adx
                config.EXPERIMENTAL_SHORT_SIDE_LOGIC = original_experimental_short_side_logic
                config.LONG_RESUME_HOT_CONTEXT_GAP_PCT = original_long_resume_hot_context_gap_pct
                config.LONG_PULLBACK_AS_RESUME_WHEN_DISABLED = original_long_pullback_as_resume
                config.PULLBACK_LONG_MIN_ADX = original_pullback_long_min_adx
                config.PULLBACK_LONG_MAX_CONTEXT_GAP_PCT = original_pullback_long_max_context_gap
                config.TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT = original_resume_long_context_gap
                config.TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT = original_resume_long_max_context_gap
                config.TREND_RESUME_LONG_MIN_ADX = original_resume_long_min_adx
                config.TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT = original_resume_long_min_trend_strength
                config.TREND_RESUME_LONG_MAX_RSI = original_resume_long_max_rsi
                config.TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE = original_resume_long_require_prev_close
                config.TREND_RESUME_LONG_STOP_LOSS_PCT = original_resume_long_stop
                config.TREND_RESUME_LONG_PARTIAL_TARGET_PCT = original_resume_long_partial_target
                config.TREND_RESUME_LONG_TRAILING_TRIGGER_PCT = original_resume_long_trailing_trigger
                config.TREND_RESUME_LONG_TRAILING_STOP_PCT = original_resume_long_trailing_stop
                config.TREND_RESUME_LONG_USE_FIXED_STOP = original_resume_long_use_fixed_stop
                config.TREND_RESUME_SHORT_STOP_LOSS_PCT = original_resume_short_stop
                config.TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT = original_resume_short_min_context_gap
                config.TREND_RESUME_SHORT_MIN_ADX = original_resume_short_min_adx
                config.TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION = original_resume_short_require_breakdown
                config.TIMEFRAME = original_timeframe

    def test_get_backtest_governance_profile_scales_min_trades_for_higher_timeframes(self):
        profile_15m = config.get_backtest_governance_profile(symbol="BTC/USDT", timeframe="15m", period_days=365)
        profile_1h = config.get_backtest_governance_profile(symbol="BTC/USDT", timeframe="1h", period_days=365)

        self.assertGreater(profile_15m["min_trades"], profile_1h["min_trades"])
        self.assertEqual(profile_15m["max_drawdown_pct"], config.ProductionConfig.MAX_PROMOTION_DRAWDOWN)
        self.assertEqual(profile_1h["max_drawdown_pct"], config.ProductionConfig.MAX_PROMOTION_DRAWDOWN)

    def test_get_backtest_governance_profile_relaxes_alt_1h_drawdown_threshold(self):
        profile = config.get_backtest_governance_profile(symbol="XLM/USDT", timeframe="1h", period_days=365)

        self.assertEqual(profile["max_drawdown_pct"], 35.0)

    def test_runtime_symbol_approval_allows_watchlist_in_testnet(self):
        record = {
            "status": "watchlist",
            "approval_label": "watchlist",
            "reason": "edge parcial",
        }
        with mock.patch.object(config, "RUNTIME_REQUIRE_APPROVED_SYMBOL", True):
            with mock.patch.object(config, "RUNTIME_ALLOW_WATCHLIST_IN_TESTNET", True):
                with mock.patch.object(config, "TESTNET", True):
                    with mock.patch.object(config, "SYMBOL", "BTC/USDT"):
                        with mock.patch.object(config, "get_symbol_validation_record", return_value=record):
                            with mock.patch.object(config, "is_symbol_runtime_approved", return_value=False):
                                bot_runner._validate_runtime_symbol_approval()

    def test_build_live_execution_plan_blocks_non_operable_micro_size(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 20.0, "free": 20.0}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.05, "min_notional": 5.0}
        risk_service = mock.Mock()
        risk_service.build_trade_plan.return_value = {
            "allowed": True,
            "quantity": 0.01,
            "position_notional": 1.0,
            "risk_per_trade_pct": 0.25,
        }
        risk_service.evaluate_symbol_operability.return_value = {
            "allowed": False,
            "reason": "Notional abaixo do minimo da exchange (1.00 < 5.00).",
            "min_required_balance": 30.0,
        }

        with mock.patch.object(bot_runner, "_find_live_positions", return_value=[]):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=100.0,
            )

        self.assertFalse(result["allowed"])
        self.assertIn("Operabilidade negada", result["reason"])

    def test_build_live_execution_plan_blocks_balance_below_minimum_bankroll(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 19.99, "free": 19.99}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.0, "min_notional": 0.0}
        risk_service = mock.Mock()

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config.ProductionConfig, "MIN_LIVE_ACCOUNT_BALANCE_USDT", 20.0, create=True),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=100.0,
            )

        self.assertFalse(result["allowed"])
        self.assertIn("Banca minima", result["reason"])
        risk_service.build_trade_plan.assert_not_called()

    def test_build_live_execution_plan_order_value_checks_available_margin_not_equity(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 21.45, "free": 2.20}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.0, "min_notional": 0.0}
        risk_service = mock.Mock()
        risk_service.build_trade_plan.return_value = {
            "allowed": True,
            "quantity": 0.000343,
            "position_notional": 21.45,
            "risk_per_trade_pct": 2.0,
            "leverage": 10.0,
            "sizing_mode": "order_value",
        }
        risk_service.evaluate_symbol_operability.return_value = {
            "allowed": True,
            "rounded_quantity": 0.000343,
            "rounded_notional": 21.45,
        }

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
            mock.patch.object(config, "FEE_PCT", 0.08),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=62500.0,
            )

        self.assertTrue(result["allowed"])
        self.assertAlmostEqual(result["account_equity"], 21.45, places=2)
        self.assertAlmostEqual(result["available_balance"], 2.20, places=2)
        self.assertAlmostEqual(result["margin_check"]["order_notional"], 21.45, places=2)
        self.assertAlmostEqual(result["margin_check"]["required_margin"], 2.145, places=3)

    def test_build_live_execution_plan_order_value_blocks_only_when_available_below_required_margin(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 21.45, "free": 2.00}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.0, "min_notional": 0.0}
        risk_service = mock.Mock()
        risk_service.build_trade_plan.return_value = {
            "allowed": True,
            "quantity": 0.000343,
            "position_notional": 21.45,
            "risk_per_trade_pct": 2.0,
            "leverage": 10.0,
            "sizing_mode": "order_value",
        }
        risk_service.evaluate_symbol_operability.return_value = {
            "allowed": True,
            "rounded_quantity": 0.000343,
            "rounded_notional": 21.45,
        }

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
            mock.patch.object(config, "FEE_PCT", 0.08),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=62500.0,
            )

        self.assertFalse(result["allowed"])
        self.assertIn("Saldo disponivel insuficiente", result["reason"])
        self.assertAlmostEqual(result["margin_check"]["required_margin"], 2.145, places=3)
        self.assertGreater(result["margin_check"]["required_margin_with_buffer"], 2.145)

    def test_build_live_execution_plan_order_value_free_zero_blocks_instead_of_fallback_to_equity(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 21.45, "free": 0.0}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.0, "min_notional": 0.0}
        risk_service = mock.Mock()
        risk_service.build_trade_plan.return_value = {
            "allowed": True,
            "quantity": 0.000343,
            "position_notional": 21.45,
            "risk_per_trade_pct": 2.0,
            "leverage": 10.0,
            "sizing_mode": "order_value",
        }
        risk_service.evaluate_symbol_operability.return_value = {
            "allowed": True,
            "rounded_quantity": 0.000343,
            "rounded_notional": 21.45,
        }

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
            mock.patch.object(config, "FEE_PCT", 0.08),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=62500.0,
            )

        self.assertFalse(result["allowed"])
        self.assertEqual(result["available_balance"], 0.0)
        self.assertIn("Saldo disponivel insuficiente", result["reason"])

    def _build_mock_live_plan_services(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 100.0, "free": 100.0}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.0, "min_notional": 0.0}
        risk_service = mock.Mock()
        risk_service.build_trade_plan.return_value = {
            "allowed": True,
            "quantity": 0.01,
            "position_notional": 100.0,
            "risk_per_trade_pct": 2.0,
            "leverage": 10.0,
            "sizing_mode": "order_value",
            "margin_allocation_pct": 100.0,
        }
        risk_service.evaluate_symbol_operability.return_value = {
            "allowed": True,
            "rounded_quantity": 0.01,
            "rounded_notional": 100.0,
        }
        return execution_service, risk_service

    def test_live_long_entry_uses_structural_stop_below_recent_low(self):
        candles = pd.DataFrame({"low": [99.0, 98.0, 97.5], "high": [101.0, 101.5, 102.0]})
        execution_service, risk_service = self._build_mock_live_plan_services()

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "USE_STRUCTURAL_STOP", True),
            mock.patch.object(config, "STRUCTURAL_STOP_LOOKBACK", 3),
            mock.patch.object(config, "STRUCTURAL_STOP_ATR_BUFFER_MULT", 0.25),
            mock.patch.object(config, "STRUCTURAL_STOP_MIN_BUFFER_PCT", 0.10),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=100.0,
                atr=1.0,
                candle_window=candles,
                signal_result={"setup": {"setup": "trend_resume_long", "direction": "long"}},
            )

        self.assertTrue(result["allowed"])
        self.assertLess(result["preview_position"]["initial_stop"], candles["low"].min())
        _, kwargs = risk_service.build_trade_plan.call_args
        self.assertEqual(kwargs["position_side"], "long")

    def test_live_short_entry_uses_structural_stop_above_recent_high(self):
        candles = pd.DataFrame({"low": [98.0, 98.5, 99.0], "high": [101.0, 102.0, 102.5]})
        execution_service, risk_service = self._build_mock_live_plan_services()

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "USE_STRUCTURAL_STOP", True),
            mock.patch.object(config, "STRUCTURAL_STOP_LOOKBACK", 3),
            mock.patch.object(config, "STRUCTURAL_STOP_ATR_BUFFER_MULT", 0.25),
            mock.patch.object(config, "STRUCTURAL_STOP_MIN_BUFFER_PCT", 0.10),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="sell",
                entry_price=100.0,
                atr=1.0,
                candle_window=candles,
                signal_result={"setup": {"setup": "trend_resume_short", "direction": "short"}},
            )

        self.assertTrue(result["allowed"])
        self.assertGreater(result["preview_position"]["initial_stop"], candles["high"].max())
        _, kwargs = risk_service.build_trade_plan.call_args
        self.assertEqual(kwargs["position_side"], "short")

    def test_live_liquidity_sweep_long_prefers_sweep_low_for_structural_stop(self):
        candles = pd.DataFrame({"low": [90.0, 98.0, 99.0], "high": [101.0, 102.0, 103.0]})
        execution_service, risk_service = self._build_mock_live_plan_services()
        signal_result = {
            "setup": {
                "setup": "liquidity_sweep_reversal_long",
                "direction": "long",
                "regime": {"market_structure": {"sweep_low": 95.0}},
            }
        }

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "USE_STRUCTURAL_STOP", True),
            mock.patch.object(config, "STRUCTURAL_STOP_ATR_BUFFER_MULT", 0.25),
            mock.patch.object(config, "STRUCTURAL_STOP_MIN_BUFFER_PCT", 0.10),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=100.0,
                atr=1.0,
                candle_window=candles,
                signal_result=signal_result,
            )

        self.assertTrue(result["allowed"])
        self.assertAlmostEqual(result["preview_position"]["initial_stop"], 94.75, places=2)

    def test_live_liquidity_sweep_short_prefers_sweep_high_for_structural_stop(self):
        candles = pd.DataFrame({"low": [97.0, 98.0, 99.0], "high": [110.0, 102.0, 103.0]})
        execution_service, risk_service = self._build_mock_live_plan_services()
        signal_result = {
            "setup": {
                "setup": "liquidity_sweep_reversal_short",
                "direction": "short",
                "regime": {"market_structure": {"sweep_high": 105.0}},
            }
        }

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(config, "USE_STRUCTURAL_STOP", True),
            mock.patch.object(config, "STRUCTURAL_STOP_ATR_BUFFER_MULT", 0.25),
            mock.patch.object(config, "STRUCTURAL_STOP_MIN_BUFFER_PCT", 0.10),
            mock.patch.object(config, "POSITION_SIZING_MODE", "order_value"),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="sell",
                entry_price=100.0,
                atr=1.0,
                candle_window=candles,
                signal_result=signal_result,
            )

        self.assertTrue(result["allowed"])
        self.assertAlmostEqual(result["preview_position"]["initial_stop"], 105.25, places=2)

    def test_build_live_execution_plan_requires_trailing_stop(self):
        execution_service = mock.Mock()
        execution_service.fetch_account_balance_snapshot.return_value = {"total": 20.0, "free": 20.0}
        execution_service.fetch_symbol_trading_rules.return_value = {"min_qty": 0.0, "min_notional": 0.0}
        risk_service = mock.Mock()
        invalid_trailing_position = {
            "current_stop": 98.5,
            "partial_target": 101.0,
            "trailing_trigger_price": 0.0,
            "trailing_trigger_pct": 0.0,
            "trailing_stop_pct": 0.0,
        }

        with (
            mock.patch.object(bot_runner, "_find_live_positions", return_value=[]),
            mock.patch.object(bot_runner, "_build_runtime_position", return_value=invalid_trailing_position),
            mock.patch.object(config.ProductionConfig, "REQUIRE_LIVE_TRAILING_STOP", True, create=True),
        ):
            result = bot_runner._build_live_entry_plan(
                execution_service=execution_service,
                risk_management_service=risk_service,
                context={"account_id": "env-primary"},
                signal_side="buy",
                entry_price=100.0,
            )

        self.assertFalse(result["allowed"])
        self.assertIn("sem trailing stop valido", result["reason"])
        risk_service.build_trade_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
