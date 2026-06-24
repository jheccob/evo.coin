import pandas as pd
import logging
from typing import Dict, Iterable, Optional
from ai_model import AIModel
from config import AppConfig
from market_state_engine import MarketStateEngine
from trading_core import market_data as trading_market_data
from trading_core import runtime_snapshot
from trading_core.block_debug import emit_block_debug
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

        logger.info("TradingBot inicializado com Binance WebSocket publico")
        logger.info("Usando dados em tempo real sem necessidade de credenciais")

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
            logger.info("Symbol atualizado para: %s", self.symbol)

        if timeframe and timeframe != self.timeframe:
            self.timeframe = timeframe
            changed = True
            logger.info("Timeframe atualizado para: %s", self.timeframe)

        if rsi_period is not None and rsi_period != self.rsi_period:
            self.rsi_period = rsi_period
            changed = True
            logger.info("RSI period atualizado para: %s", self.rsi_period)

        if rsi_min is not None and rsi_min != self.rsi_min:
            self.rsi_min = rsi_min
            changed = True
            logger.info("RSI min atualizado para: %s", self.rsi_min)

        if rsi_max is not None and rsi_max != self.rsi_max:
            self.rsi_max = rsi_max
            changed = True
            logger.info("RSI max atualizado para: %s", self.rsi_max)

        # Só mostrar configuração final se algo mudou
        if changed:
            logger.info(
                "Configuracao atualizada: %s %s RSI(%s) %s-%s",
                self.symbol,
                self.timeframe,
                self.rsi_period,
                self.rsi_min,
                self.rsi_max,
            )

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
                logger.info("Tentando endpoint publico: %s", endpoint)
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

                logger.info("Dados publicos obtidos: %s candles", len(df))
                return df

            except Exception as e:
                logger.warning("Falha no endpoint %s -> %s", endpoint, e)
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
        return runtime_snapshot.build_runtime_snapshot(
            self,
            df=df,
            timeframe=timeframe,
            context_df=context_df,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )

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
        del structure_result, confirmation_result
        context_result = context_result or {}
        entry_result = entry_result or {}
        hard_block_result = hard_block_result or {}
        scenario_score_result = scenario_score_result or {}
        risk_result = risk_result or {}
        regime_result = regime_result or {}

        block_reason = None
        action = "wait"
        if hard_block_result.get("hard_block"):
            emit_block_debug(
                "signal_engine.hard_block",
                block_source=hard_block_result.get("block_source"),
                block_reason=hard_block_result.get("block_reason"),
                entry_score=entry_result.get("entry_score"),
                scenario_score=scenario_score_result.get("scenario_score"),
            )
            block_reason = hard_block_result.get("block_reason") or "Hard block ativo."
        elif risk_result and not bool(risk_result.get("allowed", True)):
            emit_block_debug(
                "signal_engine.risk_block",
                risk_reason=risk_result.get("risk_reason") or risk_result.get("reason"),
                risk_allowed=risk_result.get("allowed"),
                risk_score=risk_result.get("risk_score"),
                signal_direction=entry_result.get("signal_direction"),
            )
            block_reason = risk_result.get("risk_reason") or risk_result.get("reason") or "Risco bloqueou a operacao."
        elif not bool(entry_result.get("objective_passed")):
            emit_block_debug(
                "signal_engine.objective_gate_block",
                objective_passed=entry_result.get("objective_passed"),
                rejection_reason=entry_result.get("rejection_reason"),
                failed_flags=entry_result.get("failed_flags"),
                entry_score=entry_result.get("entry_score"),
                signal_direction=entry_result.get("signal_direction"),
                context_bias=entry_result.get("context_bias"),
            )
            block_reason = entry_result.get("rejection_reason") or "Setup nao aprovado."
        else:
            signal_direction = str(entry_result.get("signal_direction") or "").upper()
            if signal_direction == "COMPRA":
                action = "buy"
            elif signal_direction == "VENDA":
                action = "sell"

        entry_score = float(entry_result.get("entry_score", 0.0) or 0.0)
        scenario_score = float(scenario_score_result.get("scenario_score", 0.0) or 0.0)
        confidence = round(min((entry_score * 0.6) + (scenario_score * 0.4), 10.0), 2)

        market_pattern = entry_result.get("market_pattern") or entry_result.get("setup_type")
        return {
            "action": action,
            "confidence": confidence,
            "market_bias": context_result.get("market_bias") or regime_result.get("market_bias") or "neutral",
            "market_state": regime_result.get("regime") or "range",
            "execution_mode": "ready" if action in {"buy", "sell"} else "standby",
            "market_pattern": market_pattern,
            "setup_type": market_pattern,
            "entry_reason": entry_result.get("entry_reason") if action in {"buy", "sell"} else None,
            "block_reason": block_reason,
            "invalid_if": entry_result.get("invalid_if"),
        }

    def _clear_hard_block(self):
        self._last_hard_block_evaluation = {"hard_block": False, "block_reason": None, "block_source": None, "notes": []}

    def _set_hard_block(self, block_reason: str, block_source: str = "signal_engine") -> str:
        emit_block_debug(
            "pipeline_engine.set_hard_block",
            block_source=block_source,
            block_reason=block_reason,
        )
        self._last_hard_block_evaluation = {
            "hard_block": True,
            "block_reason": block_reason,
            "block_source": block_source,
            "notes": [block_reason],
        }
        return block_reason

    # Pipeline De Sinal

    @staticmethod
    def _normalize_market_pattern_allowlist(
        allowed_market_patterns: Optional[Iterable[str]],
    ) -> Optional[set[str]]:
        if allowed_market_patterns is None:
            return None
        normalized = {
            str(value or "").strip().lower()
            for value in allowed_market_patterns
            if str(value or "").strip()
        }
        return normalized or None

    @staticmethod
    def _normalize_setup_allowlist(allowed_execution_setups: Optional[Iterable[str]]) -> Optional[set[str]]:
        return TradingBot._normalize_market_pattern_allowlist(allowed_execution_setups)

    @staticmethod
    def _normalize_signal_direction_filter(allowed_signal_directions: Optional[Iterable[str]]) -> Optional[set[str]]:
        if allowed_signal_directions is None:
            return None
        normalized: set[str] = set()
        for value in allowed_signal_directions:
            token = str(value or "").strip().lower()
            if token in {"compra", "buy", "long", "bull", "bullish"}:
                normalized.add("COMPRA")
            elif token in {"venda", "sell", "short", "bear", "bearish"}:
                normalized.add("VENDA")
        return normalized or None

    def _apply_runtime_market_pattern_policy(
        self,
        analytical_signal: str,
        allowed_market_patterns: Optional[Iterable[str]] = None,
    ) -> str:
        normalized_patterns = self._normalize_market_pattern_allowlist(allowed_market_patterns)
        if not normalized_patterns:
            return analytical_signal

        latest_entry = getattr(self, "_last_entry_quality_evaluation", None) or {}
        market_pattern = str(
            latest_entry.get("market_pattern") or latest_entry.get("setup_type") or ""
        ).strip().lower()
        if market_pattern and market_pattern in normalized_patterns:
            return analytical_signal
        return "NEUTRO"

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
        normalized_directions = self._normalize_signal_direction_filter(allowed_signal_directions)
        if not normalized_directions or analytical_signal == "NEUTRO":
            return analytical_signal
        return analytical_signal if analytical_signal in normalized_directions else "NEUTRO"

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
        del self, df, timeframe, context_timeframe, stop_loss_pct, take_profit_pct
        del require_volume, require_trend, avoid_ranging, symbol
        del ai_assist_mode, ai_min_win_probability, include_ai_explanations
        return analytical_signal

    def _finalize_signal_pipeline(self, analytical_signal: str) -> Dict[str, object]:
        hard_block = getattr(self, "_last_hard_block_evaluation", None) or {
            "hard_block": False,
            "block_reason": None,
            "block_source": None,
            "notes": [],
        }
        return {
            "approved_signal": analytical_signal,
            "blocked_signal": None,
            "block_reason": hard_block.get("block_reason"),
            "block_source": hard_block.get("block_source"),
            "hard_block_evaluation": hard_block,
        }

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
        del require_volume, require_trend, avoid_ranging, crypto_optimized, day_trading_mode
        del ai_assist_mode, ai_min_win_probability, include_ai_explanations

        if context_df is None and context_timeframe and context_timeframe != timeframe:
            try:
                context_df = self._fetch_context_df(context_timeframe)
            except Exception:
                context_df = None

        snapshot = self._build_resume_snapshot(
            df=df,
            timeframe=timeframe,
            context_df=context_df,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        analysis = snapshot.get("analysis") or {}
        context_evaluation = snapshot.get("context_evaluation") or {}
        regime_evaluation = snapshot.get("regime_evaluation") or {}
        structure_evaluation = snapshot.get("structure_evaluation") or {}
        confirmation_evaluation = snapshot.get("confirmation_evaluation") or {}
        entry_quality_evaluation = snapshot.get("entry_evaluation") or {}
        scenario_evaluation = snapshot.get("scenario_evaluation") or {}
        market_state_evaluation = snapshot.get("market_state_evaluation") or {}
        trade_decision = snapshot.get("trade_decision") or {}

        self._last_context_evaluation = context_evaluation
        self._last_regime_evaluation = regime_evaluation
        self._last_price_structure_evaluation = structure_evaluation
        self._last_confirmation_evaluation = confirmation_evaluation
        self._last_entry_quality_evaluation = entry_quality_evaluation
        self._last_scenario_evaluation = scenario_evaluation
        self._last_market_state_evaluation = market_state_evaluation
        self._last_trade_decision = trade_decision

        candidate_signal = analysis.get("signal") or "NEUTRO"
        analytical_signal = candidate_signal
        block_reason = None
        block_source = None

        resolved_regime = str(
            regime_evaluation.get("regime")
            or analysis.get("market_regime")
            or ""
        ).strip().lower()
        if candidate_signal != "NEUTRO" and resolved_regime in {"", "unknown", "none", "null"}:
            emit_block_debug(
                "pipeline_engine.regime_unknown_filter",
                candidate_signal=candidate_signal,
                resolved_regime=resolved_regime,
                timeframe=timeframe,
                context_timeframe=context_timeframe,
            )
            analytical_signal = "NEUTRO"
            block_reason = "Regime unknown bloqueado temporariamente."
            block_source = "regime_unknown_filter"

        trade_confidence = float(trade_decision.get("confidence", 0.0) or 0.0) * 10.0
        if analytical_signal != "NEUTRO" and trade_confidence < float(min_confidence or 0.0):
            emit_block_debug(
                "pipeline_engine.confidence_filter",
                candidate_signal=candidate_signal,
                trade_confidence=round(trade_confidence, 2),
                min_confidence=float(min_confidence or 0.0),
                entry_score=entry_quality_evaluation.get("entry_score"),
                scenario_score=scenario_evaluation.get("scenario_score"),
            )
            analytical_signal = "NEUTRO"
            block_reason = f"Confianca abaixo do minimo ({trade_confidence:.1f} < {float(min_confidence):.1f})."
            block_source = "confidence_filter"

        if analytical_signal != "NEUTRO":
            filtered_signal = self._apply_runtime_market_pattern_policy(
                analytical_signal=analytical_signal,
                allowed_market_patterns=allowed_execution_setups,
            )
            if filtered_signal == "NEUTRO":
                emit_block_debug(
                    "pipeline_engine.setup_allowlist_block",
                    candidate_signal=candidate_signal,
                    filtered_signal=filtered_signal,
                    market_pattern=entry_quality_evaluation.get("market_pattern") or entry_quality_evaluation.get("setup_type"),
                    allowed_execution_setups=list(allowed_execution_setups or []),
                )
                analytical_signal = "NEUTRO"
                block_reason = "Setup fora da allowlist operacional."
                block_source = "setup_allowlist"

        if analytical_signal != "NEUTRO":
            filtered_signal = self._apply_runtime_signal_direction_policy(
                analytical_signal=analytical_signal,
                allowed_signal_directions=allowed_signal_directions,
            )
            if filtered_signal == "NEUTRO":
                emit_block_debug(
                    "pipeline_engine.direction_allowlist_block",
                    candidate_signal=candidate_signal,
                    filtered_signal=filtered_signal,
                    allowed_signal_directions=list(allowed_signal_directions or []),
                    analytical_signal=analytical_signal,
                )
                analytical_signal = "NEUTRO"
                block_reason = "Direcao fora da allowlist operacional."
                block_source = "direction_allowlist"

        if analytical_signal != "NEUTRO":
            prior_signal = analytical_signal
            analytical_signal = self._apply_ai_guardrail(
                df=df,
                analytical_signal=analytical_signal,
                timeframe=timeframe,
                context_timeframe=context_timeframe,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )
            if analytical_signal == "NEUTRO":
                emit_block_debug(
                    "pipeline_engine.ai_guardrail_block",
                    candidate_signal=candidate_signal,
                    prior_signal=prior_signal,
                    timeframe=timeframe,
                    context_timeframe=context_timeframe,
                )

        if analytical_signal == "NEUTRO" and candidate_signal != "NEUTRO" and block_reason is None:
            emit_block_debug(
                "pipeline_engine.trade_decision_block",
                candidate_signal=candidate_signal,
                trade_decision_block_reason=trade_decision.get("block_reason"),
                rejection_reason=entry_quality_evaluation.get("rejection_reason"),
                objective_passed=entry_quality_evaluation.get("objective_passed"),
                failed_flags=entry_quality_evaluation.get("failed_flags"),
            )
            block_reason = trade_decision.get("block_reason") or entry_quality_evaluation.get("rejection_reason")
            block_source = "trade_decision"

        hard_block_evaluation = getattr(self, "_last_hard_block_evaluation", None) or {
            "hard_block": False,
            "block_reason": None,
            "block_source": None,
            "notes": [],
        }
        approved_signal = analytical_signal
        blocked_signal = candidate_signal if candidate_signal != approved_signal and candidate_signal != "NEUTRO" else None

        return {
            "candidate_signal": candidate_signal,
            "analytical_signal": analytical_signal,
            "approved_signal": approved_signal,
            "blocked_signal": blocked_signal,
            "block_reason": block_reason,
            "block_source": block_source,
            "analysis": analysis,
            "context_evaluation": context_evaluation,
            "regime_evaluation": regime_evaluation,
            "structure_evaluation": structure_evaluation,
            "confirmation_evaluation": confirmation_evaluation,
            "entry_quality_evaluation": entry_quality_evaluation,
            "scenario_evaluation": scenario_evaluation,
            "market_state_evaluation": market_state_evaluation,
            "trade_decision": trade_decision,
            "hard_block_evaluation": hard_block_evaluation,
        }

    def check_signal(self, df, min_confidence=60, require_volume=True, require_trend=False, avoid_ranging=False,
                    crypto_optimized=True, timeframe="5m", day_trading_mode=False, context_df=None,
                    context_timeframe: Optional[str] = None, stop_loss_pct: Optional[float] = None,
                    take_profit_pct: Optional[float] = None):
        pipeline = self.evaluate_signal_pipeline(
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
        return pipeline.get("approved_signal") or pipeline.get("analytical_signal") or "NEUTRO"

    def get_signal_with_confidence(self, df):
        pipeline = self.evaluate_signal_pipeline(df)
        decision = pipeline.get("trade_decision") or {}
        approved_signal = pipeline.get("approved_signal") or pipeline.get("analytical_signal") or "NEUTRO"
        return {
            "signal": approved_signal,
            "confidence": round(float(decision.get("confidence", 0.0) or 0.0) * 10.0, 2),
        }

    # Auxiliares De Sinal

    def _generate_advanced_signal(self, row):
        return self._generate_trend_signal(row, getattr(self, "rsi_min", 54), getattr(self, "rsi_max", 47))

    def _calculate_signal_confidence(self, row):
        signal_strength = abs(float(row.get("rsi", 50.0) or 50.0) - 50.0) / 50.0
        return round(min(signal_strength * 100.0, 100.0), 2)

    def _get_effective_min_confidence(self, min_confidence: float, timeframe: Optional[str]) -> float:
        del timeframe
        return float(min_confidence or 0.0)

    @staticmethod
    def _relax_low_confidence_signal(
        signal: str,
        confidence: float,
        effective_min_confidence: float,
        timeframe: Optional[str],
    ) -> Optional[str]:
        del timeframe
        return signal if confidence >= effective_min_confidence else None

    def _generate_trend_signal(self, row, rsi_min: float, rsi_max: float) -> str:
        rsi = float(row.get("rsi", 50.0) or 50.0)
        close = float(row.get("close", 0.0) or 0.0)
        ema_fast = float(row.get("ema_fast", close) or close)
        ema_slow = float(row.get("ema_slow", close) or close)
        ema_trend = float(row.get("ema_trend", close) or close)
        if close > ema_fast > ema_slow > ema_trend and rsi >= float(rsi_min):
            return "COMPRA"
        if close < ema_fast < ema_slow < ema_trend and rsi <= float(rsi_max):
            return "VENDA"
        return "NEUTRO"

    def calculate_advanced_score(self, row, signal=None):
        resolved_signal = signal or "NEUTRO"
        base = self._calculate_signal_confidence(row) / 10.0
        if resolved_signal in {"COMPRA", "VENDA"}:
            return round(min(base + 1.0, 10.0), 2)
        return round(min(base, 10.0), 2)
