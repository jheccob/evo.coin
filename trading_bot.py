import pandas as pd
import logging
from typing import Dict, Iterable, Optional
from ai_model import AIModel
from indicators import TechnicalIndicators
from config import AppConfig, ProductionConfig
from market_state_engine import MarketStateEngine
from trading_core import market_data as trading_market_data
from trading_core.block_debug import emit_block_debug
from trading_core import pipeline_engine as trading_pipeline_engine
from trading_core import pipeline_v2
from trading_core import signal_engine as trading_signal_engine
from trading_core.constants import MAX_STREAM_CLIENTS, STREAM_CLIENT_STALE_SECONDS
logger = logging.getLogger(__name__)

class TradingBot:
    # Runtime E Configuracao
    BACKTEST_SIGNAL_WINDOW_CANDLES = 64
    BACKTEST_CONTEXT_WINDOW_CANDLES = 64
    BACKTEST_MANAGEMENT_WINDOW_CANDLES = 64

    def __init__(self):
        # Usar sempre Binance WebSocket público
        self.exchange_name = "binance"
        self._exchange_testnet = False
        self._exchange = None
        self.symbol = AppConfig.DEFAULT_SYMBOL
        self.timeframe = AppConfig.DEFAULT_TIMEFRAME
        self.rsi_period = AppConfig.DEFAULT_RSI_PERIOD
        self.rsi_min = AppConfig.DEFAULT_RSI_MIN
        self.rsi_max = AppConfig.DEFAULT_RSI_MAX
        self.indicators = TechnicalIndicators()
        self.market_state_engine = MarketStateEngine()
        self.ai_model = AIModel()
        self._cache_data = {}
        self._stream_clients = {}
        self._last_context_evaluation = None
        self._last_regime_evaluation = None
        self._last_price_structure_evaluation = None
        self._last_confirmation_evaluation = None
        self._last_entry_quality_evaluation = None
        self._last_scenario_evaluation = None
        self._last_market_state_evaluation = None
        self._last_trade_decision = None
        self._last_hard_block_evaluation = None
        self._last_ai_evaluation = None
        self._last_candidate_signal = "NEUTRO"
        self._last_signal_pipeline = None

        logger.info("🚀 TradingBot inicializado com BINANCE WEBSOCKET PÚBLICO")
        logger.info("📡 Usando dados em tempo real sem necessidade de credenciais")

    def _load_exchange(self):
        if self._exchange is None:
            from config import ExchangeConfig

            self._exchange = ExchangeConfig.get_exchange_instance(
                self.exchange_name,
                testnet=self._exchange_testnet,
            )
        return self._exchange

    @property
    def exchange(self):
        return self._load_exchange()

    @exchange.setter
    def exchange(self, value):
        self._exchange = value

    def update_config(self, symbol=None, timeframe=None, rsi_period=None, rsi_min=None, rsi_max=None):
        """Update bot configuration parameters"""

        # Verificar se alguma configuração realmente mudou
        changed = False

        if symbol and symbol != self.symbol:
            self.symbol = symbol
            changed = True
            logger.info(f"✓ Symbol atualizado para: {self.symbol}")

        if timeframe and timeframe != self.timeframe:
            self.timeframe = timeframe
            changed = True
            logger.info(f"✓ Timeframe atualizado para: {self.timeframe}")

        if rsi_period is not None and rsi_period != self.rsi_period:
            self.rsi_period = rsi_period
            changed = True
            logger.info(f"✓ RSI Period atualizado para: {self.rsi_period}")

        if rsi_min is not None and rsi_min != self.rsi_min:
            self.rsi_min = rsi_min
            changed = True
            logger.info(f"✓ RSI Min atualizado para: {self.rsi_min}")

        if rsi_max is not None and rsi_max != self.rsi_max:
            self.rsi_max = rsi_max
            changed = True
            logger.info(f"✓ RSI Max atualizado para: {self.rsi_max}")

        # Só mostrar configuração final se algo mudou
        if changed:
            logger.info(f"📊 Configuração atualizada: {self.symbol} {self.timeframe} RSI({self.rsi_period}) {self.rsi_min}-{self.rsi_max}")

        return changed

    def validate_symbol(self, symbol):
        """Validate if symbol exists on the exchange"""
        try:
            markets = self.exchange.load_markets()
            # Symbol já está no formato correto para Binance (BTC/USDT)
            return symbol in markets
        except Exception:
            logger.debug("Falha ao validar simbolo %s na exchange %s.", symbol, self.exchange_name, exc_info=True)
            return False

    def format_symbol_for_binance(self, symbol):
        """Ensure symbol is in correct format for Binance"""
        # Binance usa formato BTC/USDT
        if not '/' in symbol:
            # Se não tem barra, adicionar /USDT como padrão
            return f"{symbol}/USDT"
        return symbol

    # Mercado E Infra

    def cleanup_stream_clients(
        self,
        keep_keys: Optional[Iterable[str]] = None,
        stale_after_seconds: int = STREAM_CLIENT_STALE_SECONDS,
        max_clients: int = MAX_STREAM_CLIENTS,
    ):
        trading_market_data.cleanup_stream_clients(
            self,
            keep_keys=keep_keys,
            stale_after_seconds=stale_after_seconds,
            max_clients=max_clients,
        )

    def reset_stream_client(self, symbol: Optional[str] = None, timeframe: Optional[str] = None):
        trading_market_data.reset_stream_client(self, symbol=symbol, timeframe=timeframe)

    def _fetch_public_ohlcv(self, limit=200, symbol: Optional[str] = None, timeframe: Optional[str] = None):
        """Fetch OHLCV data from Binance public APIs"""
        import requests

        symbol = symbol or self.symbol
        timeframe = timeframe or self.timeframe
        symbol_formatted = symbol.replace('/', '').replace(':USDT', '')  # BTC/USDT -> BTCUSDT

        timeframe_map = {
            '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m',
            '30m': '30m', '1h': '1h', '2h': '2h', '4h': '4h',
            '6h': '6h', '8h': '8h', '12h': '12h', '1d': '1d'
        }

        binance_timeframe = timeframe_map.get(timeframe, '5m')

        endpoints = [
            f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol_formatted}&interval={binance_timeframe}&limit={limit}",
            f"https://api.binance.com/api/v3/klines?symbol={symbol_formatted}&interval={binance_timeframe}&limit={limit}",
            f"https://api.binance.us/api/v3/klines?symbol={symbol_formatted}&interval={binance_timeframe}&limit={limit}"
        ]

        for endpoint in endpoints:
            try:
                logger.info(f"🌐 Tentando endpoint: {endpoint}")
                response = requests.get(endpoint, timeout=10)
                response.raise_for_status()
                ohlcv_data = response.json()

                if not ohlcv_data:
                    raise ValueError("Endpoint retornou resposta vazia")

                df_data = []
                for candle in ohlcv_data:
                    df_data.append([
                        int(candle[0]),
                        float(candle[1]),
                        float(candle[2]),
                        float(candle[3]),
                        float(candle[4]),
                        float(candle[5])
                    ])

                df = pd.DataFrame(df_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)

                logger.info(f"📊 Dados públicos obtidos: {len(df)} candles")
                return df

            except Exception as e:
                logger.warning(f"⚠️ Falha no endpoint {endpoint} -> {e}")
                continue

        raise ConnectionError("Não foi possível obter dados públicos de nenhum endpoint Binance")

    def _get_realtime_stream_client(self, symbol: Optional[str] = None, timeframe: Optional[str] = None):
        return trading_market_data.get_realtime_stream_client(
            self,
            symbol=symbol,
            timeframe=timeframe,
        )

    def get_market_data(self, limit=200, symbol: Optional[str] = None, timeframe: Optional[str] = None):
        """Fetch real-only OHLCV data from websocket buffers using closed candles only."""
        return trading_market_data.get_market_data(
            self,
            limit=limit,
            symbol=symbol,
            timeframe=timeframe,
        )

    def calculate_indicators(self, df):
        return trading_market_data.calculate_indicators(self, df)

    def get_market_summary(self, df):
        """Get market summary statistics"""
        if df is None or df.empty:
            return None

        df = self._prefer_closed_candles(df)

        if df.empty:
            return None

        last_candle = df.iloc[-1]

        # Calculate price change
        price_change = last_candle['close'] - last_candle['open']
        price_change_pct = (price_change / last_candle['open']) * 100

        # Calculate 24h high/low (approximation using available data)
        high_24h = df['high'].tail(288).max() if len(df) >= 288 else df['high'].max()  # 288 = 24h in 5min candles
        low_24h = df['low'].tail(288).min() if len(df) >= 288 else df['low'].min()

        return {
            'current_price': last_candle['close'],
            'price_change': price_change,
            'price_change_pct': price_change_pct,
            'high_24h': high_24h,
            'low_24h': low_24h,
            'volume': last_candle['volume'],
            'rsi': last_candle['rsi'],
            'signal': self.check_signal(df)
        }

    # Contexto E Regime

    @staticmethod
    def _calculate_context_slope(series: pd.Series, lookback: int = 5) -> float:
        if series is None:
            return float("nan")

        clean_series = series.dropna()
        if len(clean_series) < 2:
            return float("nan")

        effective_lookback = min(lookback, len(clean_series) - 1)
        start_value = float(clean_series.iloc[-(effective_lookback + 1)])
        end_value = float(clean_series.iloc[-1])
        if start_value == 0:
            return float("nan")
        return (end_value - start_value) / abs(start_value)

    @staticmethod
    def _normalize_strategy_pct(value: Optional[float], default_pct: float) -> float:
        raw_value = default_pct if value is None else float(value or 0.0)
        return raw_value / 100 if raw_value > 1 else raw_value

    @staticmethod
    def _prefer_closed_candles(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if df is None or df.empty:
            return None
        
        working_df = df
        if "is_closed" in working_df.columns:
            closed_df = working_df[working_df["is_closed"].fillna(False)]
            if not closed_df.empty:
                return closed_df
            if len(working_df) > 1:
                return working_df.iloc[:-1]
        return working_df

    def _build_resume_snapshot(
        self,
        df: Optional[pd.DataFrame],
        timeframe: Optional[str] = None,
        context_df: Optional[pd.DataFrame] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
    ) -> Dict[str, object]:
        working_df = pipeline_v2._prefer_closed_candles(self, df)
        if working_df.empty:
            reason = "Sem candles suficientes para leitura do motor EMA/RSI."
            neutral_market_state = pipeline_v2._build_wait_market_state(reason)
            return {
                "analysis": {
                    "signal": "NEUTRO",
                    "side": None,
                    "reason": reason,
                    "market_bias": "neutral",
                    "atr_pct": 0.0,
                    "confirmation_state": "weak",
                    "price_location": "mid_range",
                    "entry_score": 0.0,
                    "scenario_score": 0.0,
                    "market_pattern": None,
                    "setup_type": None,
                    "market_state": "neutral_chop",
                    "structure_state": "flat",
                    "entry_quality": "bad",
                },
                "context_evaluation": {
                    "market_bias": "neutral",
                    "bias": "neutral",
                    "context_strength": 0.0,
                    "is_tradeable": False,
                    "reason": reason,
                },
                "regime_evaluation": {
                    "timeframe": timeframe or self.timeframe,
                    "regime": "range",
                    "regime_score": 0.0,
                    "market_bias": "neutral",
                    "adx": 0.0,
                    "atr_pct": 0.0,
                    "ema_distance_pct": 0.0,
                    "ema_slope": 0.0,
                    "volatility_state": "low_volatility",
                    "trend_state": "range",
                    "parabolic": False,
                    "legacy_regime": "ranging",
                    "price_above_ema_200": False,
                    "is_tradeable": False,
                    "has_minimum_history": False,
                    "notes": [reason],
                    "reason": reason,
                },
                "structure_evaluation": {
                    "structure_state": "flat",
                    "structure_quality": 0.0,
                    "price_location": "mid_range",
                    "notes": [reason],
                    "breakout_pressure": False,
                    "breakout_pressure_side": "",
                    "trend_bias": "neutral",
                    "timeframe": timeframe or self.timeframe,
                    "has_minimum_history": False,
                },
                "confirmation_evaluation": {
                    "confirmation_state": "weak",
                    "confirmation_score": 0.0,
                    "hypothesis_side": None,
                    "notes": [reason],
                    "conflicts": [reason],
                    "has_minimum_history": False,
                },
                "entry_evaluation": {
                    "entry_quality": "bad",
                    "entry_score": 0.0,
                    "objective_passed": False,
                    "objective_quality": "bad",
                    "market_pattern": None,
                    "setup_type": None,
                    "rr_estimate": 0.0,
                    "rejection_reason": reason,
                    "notes": [reason],
                    "minimum_scenario_score": 6.0,
                    "entry_reason": None,
                    "has_minimum_history": False,
                },
                "scenario_evaluation": {
                    "scenario_score": 0.0,
                    "scenario_grade": "D",
                    "pullback_intensity": "not_applicable",
                    "pullback_score": 0.0,
                    "notes": [reason],
                    "has_minimum_history": False,
                },
                "market_state_evaluation": neutral_market_state,
                "trade_decision": {
                    "action": "wait",
                    "confidence": 0.0,
                    "market_bias": "neutral",
                    "market_state": "neutral_chop",
                    "execution_mode": "standby",
                    "market_pattern": None,
                    "setup_type": None,
                    "entry_reason": None,
                    "block_reason": reason,
                    "invalid_if": None,
                },
            }

        working_df = pipeline_v2._ensure_indicator_columns(self, working_df)
        buy_threshold, sell_threshold = pipeline_v2._resolve_resume_thresholds(self)
        analysis = pipeline_v2._analyze_resume_signal(
            working_df,
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        context_evaluation = pipeline_v2._build_resume_context_evaluation(
            self,
            context_df=context_df,
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
        ) or {
            "market_bias": analysis["market_bias"],
            "bias": analysis["market_bias"],
            "context_strength": 6.0 if analysis["market_bias"] in {"bullish", "bearish"} else 3.0,
            "is_tradeable": analysis["market_bias"] in {"bullish", "bearish"},
            "reason": analysis["reason"],
        }
        regime_evaluation = pipeline_v2._build_resume_regime_evaluation(
            working_df,
            timeframe=timeframe or self.timeframe,
        )

        rr_estimate = float(analysis.get("rr_estimate", 0.0) or 0.0)
        if rr_estimate <= 0 and float(stop_loss_pct or 0.0) > 0 and float(take_profit_pct or 0.0) > 0:
            rr_estimate = float(take_profit_pct) / float(stop_loss_pct)
        elif rr_estimate <= 0 and float(ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT or 0.0) > 0:
            rr_estimate = (
                float(ProductionConfig.DEFAULT_LIVE_TAKE_PROFIT_PCT)
                / float(ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT)
            )

        structure_evaluation = {
            "structure_state": analysis["structure_state"],
            "structure_quality": 7.0 if analysis["signal"] in {"COMPRA", "VENDA"} else 4.0,
            "price_location": analysis["price_location"],
            "notes": [analysis["reason"]],
            "breakout_pressure": False,
            "breakout_pressure_side": "",
            "trend_bias": analysis["market_bias"],
            "timeframe": timeframe or self.timeframe,
            "has_minimum_history": True,
        }
        confirmation_state = analysis["confirmation_state"]
        confirmation_evaluation = {
            "confirmation_state": confirmation_state,
            "confirmation_score": 7.4 if confirmation_state == "confirmed" else 5.6 if confirmation_state == "waiting" else 3.5,
            "hypothesis_side": analysis["market_bias"] if analysis["market_bias"] in {"bullish", "bearish"} else None,
            "notes": [analysis["reason"]],
            "conflicts": [] if analysis["signal"] in {"COMPRA", "VENDA"} else [analysis["reason"]],
            "has_minimum_history": True,
        }
        objective_gate = pipeline_v2._evaluate_indicator_objective_gate(
            analysis=analysis,
            context_evaluation=context_evaluation,
        )
        entry_evaluation = {
            "entry_quality": analysis["entry_quality"] if objective_gate["objective_passed"] else "bad",
            "entry_score": round(float(analysis["entry_score"]), 2),
            "objective_passed": bool(objective_gate["objective_passed"]),
            "objective_quality": str(objective_gate["objective_quality"]),
            "market_pattern": analysis.get("market_pattern"),
            "setup_type": analysis.get("setup_type"),
            "signal_direction": objective_gate["signal_direction"],
            "context_bias": objective_gate["context_bias"],
            "context_aligned": bool(objective_gate["context_aligned"]),
            "context_tradeable": bool(objective_gate["context_tradeable"]),
            "passes_score_floor": bool(objective_gate["passes_score_floor"]),
            "failed_flags": list(objective_gate["failed_flags"]),
            "critical_failed_flags": list(objective_gate["critical_failed_flags"]),
            "rr_estimate": round(float(rr_estimate), 2),
            "structural_stop_price": analysis.get("structural_stop_price"),
            "structural_take_profit_price": analysis.get("structural_take_profit_price"),
            "risk_distance_pct": float(analysis.get("risk_distance_pct", 0.0) or 0.0),
            "target_distance_pct": float(analysis.get("target_distance_pct", 0.0) or 0.0),
            "rejection_reason": (
                None
                if bool(objective_gate["objective_passed"])
                else objective_gate["rejection_reason"] or analysis["reason"]
            ),
            "notes": [analysis["reason"]],
            "minimum_scenario_score": 6.0,
            "entry_reason": analysis["reason"] if bool(objective_gate["objective_passed"]) else None,
            "invalid_if": analysis.get("invalid_if"),
            "target_reason": analysis.get("target_reason"),
            "has_minimum_history": True,
        }
        scenario_score = max(float(analysis["scenario_score"]), float(analysis["entry_score"]) - 0.2)
        scenario_evaluation = {
            "scenario_score": round(float(scenario_score), 2),
            "scenario_grade": "A" if scenario_score >= 7.5 else "B" if scenario_score >= 6.4 else "C" if scenario_score >= 5.0 else "D",
            "pullback_intensity": "not_applicable",
            "pullback_score": 0.0,
            "notes": [analysis["reason"]],
            "has_minimum_history": True,
        }
        market_state_evaluation = self.evaluate_market_state(
            context_result=context_evaluation,
            regime_result=regime_evaluation,
            structure_result=structure_evaluation,
            confirmation_result=confirmation_evaluation,
            entry_result=entry_evaluation,
            scenario_score_result=scenario_evaluation,
        )
        trade_decision = trading_signal_engine.make_trade_decision(
            self,
            context_result=context_evaluation,
            structure_result=structure_evaluation,
            confirmation_result=confirmation_evaluation,
            entry_result=entry_evaluation,
            hard_block_result={"hard_block": False, "block_reason": None, "block_source": None, "notes": []},
            scenario_score_result=scenario_evaluation,
            risk_result=None,
            regime_result=regime_evaluation,
        )
        return {
            "analysis": analysis,
            "context_evaluation": context_evaluation,
            "regime_evaluation": regime_evaluation,
            "structure_evaluation": structure_evaluation,
            "confirmation_evaluation": confirmation_evaluation,
            "entry_evaluation": entry_evaluation,
            "scenario_evaluation": scenario_evaluation,
            "market_state_evaluation": market_state_evaluation,
            "trade_decision": trade_decision,
        }

    def evaluate_market_regime(
        self,
        df: Optional[pd.DataFrame],
        timeframe: Optional[str] = None,
        as_of_timestamp=None,
        persist: bool = True,
    ) -> Dict[str, object]:
        del as_of_timestamp, persist
        snapshot = self._build_resume_snapshot(df, timeframe=timeframe)
        self._last_regime_evaluation = snapshot["regime_evaluation"]
        return snapshot["regime_evaluation"]

    def get_context_evaluation(
        self,
        context_df: Optional[pd.DataFrame],
        as_of_timestamp=None,
        context_timeframe: Optional[str] = None,
    ) -> Dict[str, object]:
        del as_of_timestamp
        snapshot = self._build_resume_snapshot(
            df=context_df,
            timeframe=context_timeframe,
            context_df=context_df,
        )
        self._last_context_evaluation = snapshot["context_evaluation"]
        return snapshot["context_evaluation"]

    def _fetch_context_df(self, context_timeframe: str, limit: int = 260) -> Optional[pd.DataFrame]:
        return self.get_market_data(
            limit=limit,
            symbol=self.symbol,
            timeframe=context_timeframe,
        )

    # Estrutura

    def analyze_price_structure(
        self,
        df: Optional[pd.DataFrame],
        market_bias: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> Dict[str, object]:
        del market_bias
        snapshot = self._build_resume_snapshot(df, timeframe=timeframe)
        self._last_price_structure_evaluation = snapshot["structure_evaluation"]
        return snapshot["structure_evaluation"]

    def get_price_structure_evaluation(
        self,
        df: Optional[pd.DataFrame],
        timeframe: Optional[str] = None,
        market_bias: Optional[str] = None,
    ) -> Dict[str, object]:
        return self.analyze_price_structure(
            df=df,
            market_bias=market_bias,
            timeframe=timeframe,
        )

    # Confirmacao

    @staticmethod
    def _resolve_confirmation_side(
        signal_hypothesis: Optional[str] = None,
        context_evaluation: Optional[Dict[str, object]] = None,
        last_row: Optional[pd.Series] = None,
    ) -> str:
        del last_row
        normalized_signal = str(signal_hypothesis or "").strip().upper()
        if normalized_signal == "COMPRA":
            return "bullish"
        if normalized_signal == "VENDA":
            return "bearish"
        context_bias = str(
            (context_evaluation or {}).get("market_bias")
            or (context_evaluation or {}).get("bias")
            or ""
        ).strip().lower()
        if context_bias in {"bullish", "bearish"}:
            return context_bias
        return "neutral"

    def analyze_confirmation(
        self,
        df: Optional[pd.DataFrame],
        market_bias: Optional[str] = None,
        structure_state: Optional[str] = None,
    ) -> Dict[str, object]:
        del market_bias, structure_state
        snapshot = self._build_resume_snapshot(df, timeframe=self.timeframe)
        self._last_confirmation_evaluation = snapshot["confirmation_evaluation"]
        return snapshot["confirmation_evaluation"]

    def get_confirmation_evaluation(
        self,
        df: Optional[pd.DataFrame],
        signal_hypothesis: Optional[str] = None,
        timeframe: Optional[str] = None,
        context_evaluation: Optional[Dict[str, object]] = None,
        structure_evaluation: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        del signal_hypothesis, context_evaluation, structure_evaluation
        snapshot = self._build_resume_snapshot(df, timeframe=timeframe)
        self._last_confirmation_evaluation = snapshot["confirmation_evaluation"]
        return snapshot["confirmation_evaluation"]

    # Entrada

    def evaluate_contextual_entry(
        self,
        df: Optional[pd.DataFrame],
        market_bias: Optional[str] = None,
        structure_state: Optional[str] = None,
        regime_evaluation: Optional[Dict[str, object]] = None,
        structure_evaluation: Optional[Dict[str, object]] = None,
        signal_hypothesis: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> Dict[str, object]:
        del market_bias, structure_state, regime_evaluation, structure_evaluation, signal_hypothesis
        snapshot = self._build_resume_snapshot(df, timeframe=timeframe)
        self._last_entry_quality_evaluation = snapshot["entry_evaluation"]
        return snapshot["entry_evaluation"]

    @staticmethod
    def _classify_entry_quality(
        rr_estimate: float,
        quality_score: float,
        late_entry: bool,
        stretched_price: bool,
        price_in_middle: bool,
        candle_is_acceptable: bool,
        structure_state: Optional[str] = None,
        low_volatility: bool = False,
        dead_range: bool = False,
    ) -> str:
        del late_entry, stretched_price, price_in_middle, candle_is_acceptable, structure_state, low_volatility, dead_range
        if quality_score >= 7.0 and rr_estimate >= 1.3:
            return "strong"
        if quality_score >= 5.2:
            return "acceptable"
        return "bad"

    @staticmethod
    def _evaluate_market_reading_objective_gate(
        setup_type: Optional[str],
        regime_name: Optional[str],
        structure_state: Optional[str],
        structure_quality: float,
        candle_quality: Optional[str],
        momentum_state: Optional[str],
        rsi_state: Optional[str],
        rr_estimate: float,
        low_volatility: bool,
        dead_range: bool,
        stretched_price: bool,
        late_entry: bool,
        price_in_middle: bool,
        regime_available: bool,
        aligned_trend_regime: bool,
        strong_trend_regime: bool,
        reversal_confirmed: bool,
        volatility_state: Optional[str],
        price_location: Optional[str],
        micro_breakout_recent: bool,
    ) -> Dict[str, object]:
        del regime_name, candle_quality, momentum_state, rsi_state, low_volatility, dead_range
        del stretched_price, late_entry, price_in_middle, regime_available, aligned_trend_regime
        del strong_trend_regime, reversal_confirmed, volatility_state, price_location, micro_breakout_recent
        market_pattern = str(setup_type or "").strip().lower()
        objective_passed = bool(
            market_pattern in {
                "trend_resume_long",
                "trend_resume_short",
                "pullback_long",
                "pullback_short",
                "ema_rsi_resume_long",
                "ema_rsi_resume_short",
            }
            and structure_quality >= 4.0
            and structure_state in {"trend_resume", "trend_resume_wait", "pullback"}
        )
        if not objective_passed:
            emit_block_debug(
                "trading_bot.market_reading_gate_failed",
                setup_type=setup_type,
                regime_name=regime_name,
                structure_state=structure_state,
                structure_quality=round(float(structure_quality or 0.0), 2),
                rr_estimate=round(float(rr_estimate or 0.0), 2),
                rejection_reason="Motor EMA/RSI sem gatilho objetivo valido.",
            )
        return {
            "objective_passed": objective_passed,
            "objective_quality": "strong" if objective_passed and rr_estimate >= 1.5 else "acceptable" if objective_passed else "bad",
            "rejection_reason": None if objective_passed else "Motor EMA/RSI sem gatilho objetivo valido.",
        }

    @staticmethod
    def _evaluate_setup_objective_gate(
        setup_type: Optional[str],
        regime_name: Optional[str],
        structure_state: Optional[str],
        structure_quality: float,
        candle_quality: Optional[str],
        momentum_state: Optional[str],
        rsi_state: Optional[str],
        rr_estimate: float,
        low_volatility: bool,
        dead_range: bool,
        stretched_price: bool,
        late_entry: bool,
        price_in_middle: bool,
        regime_available: bool,
        aligned_trend_regime: bool,
        strong_trend_regime: bool,
        reversal_confirmed: bool,
        volatility_state: Optional[str],
        price_location: Optional[str],
        micro_breakout_recent: bool,
    ) -> Dict[str, object]:
        return TradingBot._evaluate_market_reading_objective_gate(
            setup_type=setup_type,
            regime_name=regime_name,
            structure_state=structure_state,
            structure_quality=structure_quality,
            candle_quality=candle_quality,
            momentum_state=momentum_state,
            rsi_state=rsi_state,
            rr_estimate=rr_estimate,
            low_volatility=low_volatility,
            dead_range=dead_range,
            stretched_price=stretched_price,
            late_entry=late_entry,
            price_in_middle=price_in_middle,
            regime_available=regime_available,
            aligned_trend_regime=aligned_trend_regime,
            strong_trend_regime=strong_trend_regime,
            reversal_confirmed=reversal_confirmed,
            volatility_state=volatility_state,
            price_location=price_location,
            micro_breakout_recent=micro_breakout_recent,
        )

    def _apply_objective_entry_gate(self, evaluation: Optional[Dict[str, object]]) -> Dict[str, object]:
        return dict(evaluation or {})

    @staticmethod
    def _normalize_entry_quality_label(label: Optional[str]) -> str:
        normalized = str(label or "bad").strip().lower()
        if normalized == "good":
            return "strong"
        if normalized in {"strong", "acceptable", "bad"}:
            return normalized
        return "bad"

    @staticmethod
    def _is_soft_entry_rejection(
        entry_evaluation: Optional[Dict[str, object]],
        structure_state: Optional[str] = None,
        structure_quality: float = 0.0,
        confirmation_state: Optional[str] = None,
    ) -> bool:
        return (
            str((entry_evaluation or {}).get("entry_quality") or "bad").strip().lower() == "bad"
            and structure_state in {"trend_resume", "trend_resume_wait", "pullback"}
            and structure_quality >= 4.0
            and confirmation_state in {"confirmed", "waiting"}
        )

    def get_entry_quality_evaluation(
        self,
        df: Optional[pd.DataFrame],
        signal_hypothesis: Optional[str] = None,
        timeframe: Optional[str] = None,
        regime_evaluation: Optional[Dict[str, object]] = None,
        structure_evaluation: Optional[Dict[str, object]] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
    ) -> Dict[str, object]:
        del signal_hypothesis, regime_evaluation, structure_evaluation
        snapshot = self._build_resume_snapshot(
            df,
            timeframe=timeframe,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        self._last_entry_quality_evaluation = snapshot["entry_evaluation"]
        return snapshot["entry_evaluation"]

    # Cenario E Estado

    def build_scenario_score(
        self,
        context_result: Optional[Dict[str, object]],
        structure_result: Optional[Dict[str, object]],
        confirmation_result: Optional[Dict[str, object]],
        entry_result: Optional[Dict[str, object]],
        regime_result: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        context_score = float((context_result or {}).get("context_strength", 0.0) or 0.0)
        structure_score = float((structure_result or {}).get("structure_quality", 0.0) or 0.0)
        confirmation_score = float((confirmation_result or {}).get("confirmation_score", 0.0) or 0.0)
        entry_score = float((entry_result or {}).get("entry_score", 0.0) or 0.0)
        regime_score = float((regime_result or {}).get("regime_score", 0.0) or 0.0)
        weighted_score = (
            context_score * 0.25
            + structure_score * 0.25
            + confirmation_score * 0.20
            + entry_score * 0.20
            + min(regime_score, 10.0) * 0.10
        )
        scenario_score = round(float(max(0.0, min(10.0, weighted_score))), 2)
        evaluation = {
            "scenario_score": scenario_score,
            "scenario_grade": "A" if scenario_score >= 7.5 else "B" if scenario_score >= 6.3 else "C" if scenario_score >= 5.0 else "D",
            "pullback_intensity": "not_applicable",
            "pullback_score": 0.0,
            "notes": list(
                dict.fromkeys(
                    note
                    for note in [
                        *((context_result or {}).get("notes") or []),
                        *((structure_result or {}).get("notes") or []),
                        *((confirmation_result or {}).get("notes") or []),
                        *((entry_result or {}).get("notes") or []),
                    ]
                    if note
                )
            ),
            "has_minimum_history": any(
                result and result.get("has_minimum_history", True)
                for result in (context_result, structure_result, confirmation_result, entry_result, regime_result)
            ),
        }
        self._last_scenario_evaluation = evaluation
        return evaluation

    def evaluate_market_state(
        self,
        context_result: Optional[Dict[str, object]],
        regime_result: Optional[Dict[str, object]],
        structure_result: Optional[Dict[str, object]],
        confirmation_result: Optional[Dict[str, object]],
        entry_result: Optional[Dict[str, object]],
        scenario_score_result: Optional[Dict[str, object]],
        hard_block_result: Optional[Dict[str, object]] = None,
        risk_result: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        engine = getattr(self, "market_state_engine", None)
        if engine is None:
            engine = MarketStateEngine()
            self.market_state_engine = engine

        evaluation = engine.evaluate(
            context_result=context_result,
            regime_result=regime_result,
            structure_result=structure_result,
            confirmation_result=confirmation_result,
            entry_result=entry_result,
            scenario_score_result=scenario_score_result,
            hard_block_result=hard_block_result,
            risk_result=risk_result,
        )
        self._last_market_state_evaluation = evaluation
        return evaluation

    # Decisao E Bloqueios

    def make_trade_decision(
        self,
        context_result: Optional[Dict[str, object]],
        structure_result: Optional[Dict[str, object]],
        confirmation_result: Optional[Dict[str, object]],
        entry_result: Optional[Dict[str, object]],
        hard_block_result: Optional[Dict[str, object]],
        scenario_score_result: Optional[Dict[str, object]],
        risk_result: Optional[Dict[str, object]] = None,
        regime_result: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        return trading_signal_engine.make_trade_decision(
            self,
            context_result=context_result,
            structure_result=structure_result,
            confirmation_result=confirmation_result,
            entry_result=entry_result,
            hard_block_result=hard_block_result,
            scenario_score_result=scenario_score_result,
            risk_result=risk_result,
            regime_result=regime_result,
        )

    def _clear_hard_block(self):
        trading_pipeline_engine.clear_hard_block(self)

    def _set_hard_block(self, block_reason: str, block_source: str = "signal_engine") -> str:
        return trading_pipeline_engine.set_hard_block(
            self,
            block_reason=block_reason,
            block_source=block_source,
        )

    # Pipeline De Sinal

    @staticmethod
    def _normalize_market_pattern_allowlist(
        allowed_market_patterns: Optional[Iterable[str]],
    ) -> Optional[set[str]]:
        return trading_pipeline_engine.normalize_market_pattern_allowlist(allowed_market_patterns)

    @staticmethod
    def _normalize_setup_allowlist(allowed_execution_setups: Optional[Iterable[str]]) -> Optional[set[str]]:
        return TradingBot._normalize_market_pattern_allowlist(allowed_execution_setups)

    @staticmethod
    def _normalize_signal_direction_filter(allowed_signal_directions: Optional[Iterable[str]]) -> Optional[set[str]]:
        return trading_pipeline_engine.normalize_signal_direction_filter(allowed_signal_directions)

    def _apply_runtime_market_pattern_policy(
        self,
        analytical_signal: str,
        allowed_market_patterns: Optional[Iterable[str]] = None,
    ) -> str:
        return trading_pipeline_engine.apply_runtime_market_pattern_policy(
            self,
            analytical_signal=analytical_signal,
            allowed_market_patterns=allowed_market_patterns,
        )

    def _apply_runtime_setup_execution_policy(
        self,
        analytical_signal: str,
        allowed_execution_setups: Optional[Iterable[str]] = None,
    ) -> str:
        return self._apply_runtime_market_pattern_policy(
            analytical_signal=analytical_signal,
            allowed_market_patterns=allowed_execution_setups,
        )

    def _apply_runtime_signal_direction_policy(
        self,
        analytical_signal: str,
        allowed_signal_directions: Optional[Iterable[str]] = None,
    ) -> str:
        return trading_pipeline_engine.apply_runtime_signal_direction_policy(
            self,
            analytical_signal=analytical_signal,
            allowed_signal_directions=allowed_signal_directions,
        )

    def _apply_ai_guardrail(
        self,
        df,
        analytical_signal: str,
        timeframe: str = "5m",
        context_timeframe: Optional[str] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        require_volume: bool = True,
        require_trend: bool = False,
        avoid_ranging: bool = False,
        symbol: Optional[str] = None,
        ai_assist_mode: Optional[str] = None,
        ai_min_win_probability: Optional[float] = None,
        include_ai_explanations: bool = True,
    ) -> str:
        return trading_pipeline_engine.apply_ai_guardrail(
            self,
            df=df,
            analytical_signal=analytical_signal,
            timeframe=timeframe,
            context_timeframe=context_timeframe,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            require_volume=require_volume,
            require_trend=require_trend,
            avoid_ranging=avoid_ranging,
            symbol=symbol,
            ai_assist_mode=ai_assist_mode,
            ai_min_win_probability=ai_min_win_probability,
            include_ai_explanations=include_ai_explanations,
        )

    def _finalize_signal_pipeline(self, analytical_signal: str) -> Dict[str, object]:
        return trading_pipeline_engine.finalize_signal_pipeline(self, analytical_signal)

    def evaluate_signal_pipeline(
        self,
        df,
        min_confidence=60,
        require_volume=True,
        require_trend=False,
        avoid_ranging=False,
        crypto_optimized=True,
        timeframe="5m",
        day_trading_mode=False,
        context_df=None,
        context_timeframe: Optional[str] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        allowed_execution_setups: Optional[Iterable[str]] = None,
        allowed_signal_directions: Optional[Iterable[str]] = None,
        ai_assist_mode: Optional[str] = None,
        ai_min_win_probability: Optional[float] = None,
        include_ai_explanations: bool = True,
    ) -> Dict[str, object]:
        return trading_pipeline_engine.evaluate_signal_pipeline(
            self,
            df,
            min_confidence=min_confidence,
            require_volume=require_volume,
            require_trend=require_trend,
            avoid_ranging=avoid_ranging,
            crypto_optimized=crypto_optimized,
            timeframe=timeframe,
            day_trading_mode=day_trading_mode,
            context_df=context_df,
            context_timeframe=context_timeframe,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            allowed_execution_setups=allowed_execution_setups,
            allowed_signal_directions=allowed_signal_directions,
            ai_assist_mode=ai_assist_mode,
            ai_min_win_probability=ai_min_win_probability,
            include_ai_explanations=include_ai_explanations,
        )

    def check_signal(self, df, min_confidence=60, require_volume=True, require_trend=False, avoid_ranging=False,
                    crypto_optimized=True, timeframe="5m", day_trading_mode=False, context_df=None,
                    context_timeframe: Optional[str] = None, stop_loss_pct: Optional[float] = None,
                    take_profit_pct: Optional[float] = None):
        return trading_signal_engine.check_signal(
            self,
            df,
            min_confidence=min_confidence,
            require_volume=require_volume,
            require_trend=require_trend,
            avoid_ranging=avoid_ranging,
            crypto_optimized=crypto_optimized,
            timeframe=timeframe,
            day_trading_mode=day_trading_mode,
            context_df=context_df,
            context_timeframe=context_timeframe,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )

    def get_signal_with_confidence(self, df):
        return trading_signal_engine.get_signal_with_confidence(self, df)

    # Auxiliares De Sinal

    def _generate_advanced_signal(self, row):
        return trading_signal_engine.generate_advanced_signal(self, row)

    def _calculate_signal_confidence(self, row):
        return trading_signal_engine.calculate_signal_confidence(self, row)

    def _get_effective_min_confidence(self, min_confidence: float, timeframe: Optional[str]) -> float:
        return trading_signal_engine.get_effective_min_confidence(
            self,
            min_confidence,
            timeframe,
        )

    @staticmethod
    def _relax_low_confidence_signal(
        signal: str,
        confidence: float,
        effective_min_confidence: float,
        timeframe: Optional[str],
    ) -> Optional[str]:
        return trading_signal_engine.relax_low_confidence_signal(
            signal,
            confidence,
            effective_min_confidence,
            timeframe,
        )

    def _generate_trend_signal(self, row, rsi_min: float, rsi_max: float) -> str:
        return trading_signal_engine.generate_trend_signal(self, row, rsi_min, rsi_max)

    def calculate_advanced_score(self, row, signal=None):
        return trading_signal_engine.calculate_advanced_score(self, row, signal=signal)
