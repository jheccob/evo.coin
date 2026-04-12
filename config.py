from __future__ import annotations

import os
from typing import Dict, List, Optional

BINANCE_TESTNET = str(os.getenv("BINANCE_TESTNET", "true")).strip().lower() in {"1", "true", "yes", "on", "y", "sim"}
DATABASE_URL = os.getenv("DATABASE_URL")


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y", "sim"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return float(default)


def _get_csv_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    values = [item.strip() for item in str(raw).split(",")]
    filtered = [item for item in values if item]
    return filtered or list(default)


# Core Runtime / Strategy Defaults

SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
LIMIT = _get_int("LIMIT", 200)

TESTNET = _get_bool("TESTNET", BINANCE_TESTNET)
BACKTEST_USE_TESTNET = _get_bool("BACKTEST_USE_TESTNET", False)
BACKTEST_USE_LOCAL_CSV = _get_bool("BACKTEST_USE_LOCAL_CSV", True)
BACKTEST_REQUIRE_LOCAL_CSV = _get_bool("BACKTEST_REQUIRE_LOCAL_CSV", True)
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP = _get_bool("BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP", False)
BOT_ALLOW_REST_FALLBACK = _get_bool("BOT_ALLOW_REST_FALLBACK", False)
BOT_WEBSOCKET_TIMEOUT_SEC = _get_float("BOT_WEBSOCKET_TIMEOUT_SEC", 25.0)
BOT_BOOTSTRAP_CANDLES = _get_int("BOT_BOOTSTRAP_CANDLES", max(LIMIT, 300))
POLL_SECONDS = _get_int("POLL_SECONDS", 30)
LEVERAGE = _get_int("LEVERAGE", 5)
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 1.0)
MAX_OPEN_TRADES = _get_int("MAX_OPEN_TRADES", 1)

FAST_EMA = _get_int("FAST_EMA", 9)
SLOW_EMA = _get_int("SLOW_EMA", 21)
TREND_EMA = _get_int("TREND_EMA", 50)

RSI_PERIOD = _get_int("RSI_PERIOD", 14)
ATR_PERIOD = _get_int("ATR_PERIOD", 14)

LONG_SLOPE_LOOKBACK = _get_int("LONG_SLOPE_LOOKBACK", 8)
SHORT_SLOPE_LOOKBACK = _get_int("SHORT_SLOPE_LOOKBACK", 5)

SHORT_RSI_MIN = _get_float("SHORT_RSI_MIN", 0.0)
ENABLE_SHORT_PULLBACK = _get_bool("ENABLE_SHORT_PULLBACK", True)
ENABLE_SHORT_RESUME = _get_bool("ENABLE_SHORT_RESUME", True)

BUY_RSI_SIGNAL = _get_float("BUY_RSI_SIGNAL", 62.0)
SELL_RSI_SIGNAL = _get_float("SELL_RSI_SIGNAL", 37.0)

LONG_PULLBACK_MIN_TREND_STRENGTH_PCT = _get_float("LONG_PULLBACK_MIN_TREND_STRENGTH_PCT", 0.18)

LONG_MIN_ATR_PCT = _get_float("LONG_MIN_ATR_PCT", 0.25)
SHORT_MIN_ATR_PCT = _get_float("SHORT_MIN_ATR_PCT", 0.25)

LONG_TREND_GAP_PCT = _get_float("LONG_TREND_GAP_PCT", 0.25)
SHORT_TREND_GAP_PCT = _get_float("SHORT_TREND_GAP_PCT", 0.25)

LONG_FAST_SLOW_GAP_PCT = _get_float("LONG_FAST_SLOW_GAP_PCT", 0.08)
SHORT_FAST_SLOW_GAP_PCT = _get_float("SHORT_FAST_SLOW_GAP_PCT", 0.10)

LONG_STOP_LOSS_PCT = _get_float("LONG_STOP_LOSS_PCT", 0.8)
SHORT_STOP_LOSS_PCT = _get_float("SHORT_STOP_LOSS_PCT", 0.9)

LONG_TAKE_PROFIT_PCT = _get_float("LONG_TAKE_PROFIT_PCT", 1.8)
SHORT_TAKE_PROFIT_PCT = _get_float("SHORT_TAKE_PROFIT_PCT", 1.8)

LONG_TRAILING_STOP_PCT = _get_float("LONG_TRAILING_STOP_PCT", 1.0)
SHORT_TRAILING_STOP_PCT = _get_float("SHORT_TRAILING_STOP_PCT", 1.0)

TRAILING_TRIGGER_PCT = _get_float("TRAILING_TRIGGER_PCT", 1.0)
PARTIAL_TARGET_PCT = _get_float("PARTIAL_TARGET_PCT", 1.0)
MIN_TARGET_DISTANCE_PCT = _get_float("MIN_TARGET_DISTANCE_PCT", 0.45)

MIN_TREND_STRENGTH_PCT = _get_float("MIN_TREND_STRENGTH_PCT", 0.12)
MIN_TREND_STRENGTH_PCT_SHORT = _get_float("MIN_TREND_STRENGTH_PCT_SHORT", 0.16)

ALLOW_LONG = _get_bool("ALLOW_LONG", True)
ALLOW_SHORT = _get_bool("ALLOW_SHORT", True)
BLOCK_UNKNOWN_REGIME = _get_bool("BLOCK_UNKNOWN_REGIME", True)

USE_NEXT_CANDLE_OPEN_FOR_BACKTEST = _get_bool("USE_NEXT_CANDLE_OPEN_FOR_BACKTEST", True)
FEE_PCT = _get_float("FEE_PCT", 0.08)

# Production safety guards (runtime)
LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "")
MAX_REAL_RISK_PER_TRADE_PCT_START = _get_float("MAX_REAL_RISK_PER_TRADE_PCT_START", 0.25)
MAX_DAILY_REAL_LOSS_PCT = _get_float("MAX_DAILY_REAL_LOSS_PCT", 2.5)
MAX_CONSECUTIVE_REAL_LOSSES = _get_int("MAX_CONSECUTIVE_REAL_LOSSES", 4)
MAX_OPEN_REAL_TRADES = _get_int("MAX_OPEN_REAL_TRADES", 1)


class AppConfig:
    DB_PATH = os.getenv("DB_PATH", "data/trading_bot.db")
    DATABASE_URL = str(os.getenv("DATABASE_URL", "")).strip()
    DB_BACKEND = "postgres" if DATABASE_URL.lower().startswith(("postgres://", "postgresql://")) else "sqlite"
    DB_DISPLAY = "postgres (DATABASE_URL)" if DB_BACKEND == "postgres" else DB_PATH

    DEFAULT_SYMBOL = os.getenv("APP_DEFAULT_SYMBOL", SYMBOL)
    DEFAULT_TIMEFRAME = os.getenv("APP_DEFAULT_TIMEFRAME", TIMEFRAME)
    DEFAULT_RSI_PERIOD = _get_int("APP_DEFAULT_RSI_PERIOD", RSI_PERIOD)
    # Mantido dentro do range da UI (45-60 / 40-55).
    DEFAULT_RSI_MIN = _get_int("APP_DEFAULT_RSI_MIN", 54)
    DEFAULT_RSI_MAX = _get_int("APP_DEFAULT_RSI_MAX", 47)

    PRIMARY_CONTEXT_TIMEFRAME = os.getenv("APP_PRIMARY_CONTEXT_TIMEFRAME", "1h")
    DEFAULT_BACKTEST_WINDOW_DAYS = _get_int("APP_DEFAULT_BACKTEST_WINDOW_DAYS", 90)
    DEFAULT_BACKTEST_PRESET = os.getenv("APP_DEFAULT_BACKTEST_PRESET", "Leitura Ativa (15m)")
    DEFAULT_BACKTEST_PRESET_SUMMARY = (
        "Preset global validado no terminal para leitura EMA/RSI com risco balanceado."
    )

    BRAZIL_SUPPORTED_EXCHANGES = _get_csv_list(
        "BRAZIL_SUPPORTED_EXCHANGES",
        ["binance", "binanceusdm"],
    )

    SINGLE_SETUP_MODE = _get_bool("SINGLE_SETUP_MODE", False)
    ENABLE_PARAMETER_OPTIMIZATION = _get_bool("ENABLE_PARAMETER_OPTIMIZATION", True)
    ENABLE_MARKET_SCAN = _get_bool("ENABLE_MARKET_SCAN", True)
    MAX_CANDLES = _get_int("MAX_CANDLES", 1200)

    _SUPPORTED_PAIRS = _get_csv_list(
        "SUPPORTED_PAIRS",
        ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"],
    )
    _SUPPORTED_TIMEFRAMES = _get_csv_list(
        "SUPPORTED_TIMEFRAMES",
        ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
    )

    _CRYPTO_TIMEFRAME_SETTINGS = {
        "1m": {"rsi_oversold": 50, "rsi_overbought": 50, "min_confidence": 72, "min_volume_ratio": 1.4},
        "5m": {"rsi_oversold": 54, "rsi_overbought": 47, "min_confidence": 70, "min_volume_ratio": 1.2},
        "15m": {"rsi_oversold": 54, "rsi_overbought": 47, "min_confidence": 68, "min_volume_ratio": 1.15},
        "30m": {"rsi_oversold": 53, "rsi_overbought": 47, "min_confidence": 66, "min_volume_ratio": 1.1},
        "1h": {"rsi_oversold": 52, "rsi_overbought": 48, "min_confidence": 64, "min_volume_ratio": 1.05},
        "4h": {"rsi_oversold": 51, "rsi_overbought": 49, "min_confidence": 62, "min_volume_ratio": 1.0},
        "1d": {"rsi_oversold": 50, "rsi_overbought": 50, "min_confidence": 60, "min_volume_ratio": 1.0},
    }

    _DAY_TRADING_SETTINGS = {
        "1m": {"rsi_oversold": 50, "rsi_overbought": 50, "min_confidence": 74, "min_volume_ratio": 1.5},
        "5m": {"rsi_oversold": 54, "rsi_overbought": 47, "min_confidence": 72, "min_volume_ratio": 1.3},
        "15m": {"rsi_oversold": 54, "rsi_overbought": 47, "min_confidence": 70, "min_volume_ratio": 1.2},
    }

    _DIRECTION_FILTER_LABELS = {
        "COMPRA": "Apenas Compra (Long)",
        "VENDA": "Apenas Venda (Short)",
    }

    _MARKET_READING_FAMILY_CONFIGS = {
        "all_states": {
            "label": "Todos os Estados",
            "description": "Aceita leitura compradora e vendedora quando o setup estiver válido.",
            "allowed_directions": ["COMPRA", "VENDA"],
        },
        "long_bias": {
            "label": "Viés Comprador",
            "description": "Prioriza cenários de tendência compradora.",
            "allowed_directions": ["COMPRA"],
        },
        "short_bias": {
            "label": "Viés Vendedor",
            "description": "Prioriza cenários de tendência vendedora.",
            "allowed_directions": ["VENDA"],
        },
        "trend_only": {
            "label": "Somente Tendência",
            "description": "Mantém ambos os lados, mas para execução focada em continuidade.",
            "allowed_directions": ["COMPRA", "VENDA"],
        },
    }

    _RISK_PROFILE_CONFIGS = {
        "manual": {
            "label": "Manual",
            "description": "Você controla SL/TP manualmente.",
        },
        "balanced": {
            "label": "Balanceado",
            "description": "Risco moderado, alinhado ao setup validado.",
            "stop_loss_pct": 0.8,
            "take_profit_pct": 1.8,
        },
        "conservative": {
            "label": "Conservador",
            "description": "Stop mais curto e alvo mais contido para reduzir variância.",
            "stop_loss_pct": 0.7,
            "take_profit_pct": 1.4,
        },
        "aggressive": {
            "label": "Agressivo",
            "description": "Aceita maior oscilação buscando trades mais longos.",
            "stop_loss_pct": 1.0,
            "take_profit_pct": 2.2,
        },
    }

    _BACKTEST_SETUP_PRESETS = {
        "Leitura Ativa (15m)": {
            "bt_market_family": "all_states",
            "bt_direction_focus": ["COMPRA", "VENDA"],
            "bt_risk_profile": "balanced",
            "bt_enable_volume_filter": False,
            "bt_enable_trend_filter": False,
            "bt_enable_avoid_ranging": False,
            "bt_stop_loss_pct": 0.8,
            "bt_take_profit_pct": 1.8,
            "bt_context_mode": "same_timeframe",
        },
        "Leitura Conservadora (1h)": {
            "bt_market_family": "trend_only",
            "bt_direction_focus": ["COMPRA", "VENDA"],
            "bt_risk_profile": "conservative",
            "bt_enable_volume_filter": True,
            "bt_enable_trend_filter": True,
            "bt_enable_avoid_ranging": True,
            "bt_stop_loss_pct": 0.7,
            "bt_take_profit_pct": 1.4,
            "bt_context_mode": "1h",
        },
        "Leitura Compradora": {
            "bt_market_family": "long_bias",
            "bt_direction_focus": ["COMPRA"],
            "bt_risk_profile": "balanced",
            "bt_enable_volume_filter": False,
            "bt_enable_trend_filter": False,
            "bt_enable_avoid_ranging": False,
            "bt_stop_loss_pct": 0.8,
            "bt_take_profit_pct": 1.8,
            "bt_context_mode": "same_timeframe",
        },
        "Leitura Vendedora": {
            "bt_market_family": "short_bias",
            "bt_direction_focus": ["VENDA"],
            "bt_risk_profile": "balanced",
            "bt_enable_volume_filter": False,
            "bt_enable_trend_filter": False,
            "bt_enable_avoid_ranging": False,
            "bt_stop_loss_pct": 0.8,
            "bt_take_profit_pct": 1.8,
            "bt_context_mode": "same_timeframe",
        },
    }

    _BACKTEST_PRESET_NOTES = {
        "Leitura Ativa (15m)": "Preset principal usado para validar o setup operacional no terminal.",
        "Leitura Conservadora (1h)": "Menos trades, mais filtro de qualidade.",
        "Leitura Compradora": "Modo de diagnóstico para isolar performance de long.",
        "Leitura Vendedora": "Modo de diagnóstico para isolar performance de short.",
    }

    @classmethod
    def get_supported_pairs(cls) -> List[str]:
        return list(cls._SUPPORTED_PAIRS)

    @classmethod
    def get_supported_timeframes(cls) -> List[str]:
        return list(cls._SUPPORTED_TIMEFRAMES)

    @classmethod
    def get_crypto_timeframe_settings(cls, timeframe: str) -> Dict[str, float]:
        key = str(timeframe or "").strip().lower()
        return dict(cls._CRYPTO_TIMEFRAME_SETTINGS.get(key, cls._CRYPTO_TIMEFRAME_SETTINGS["15m"]))

    @classmethod
    def get_day_trading_settings(cls, timeframe: str) -> Dict[str, float]:
        key = str(timeframe or "").strip().lower()
        return dict(cls._DAY_TRADING_SETTINGS.get(key, cls._DAY_TRADING_SETTINGS["5m"]))

    @classmethod
    def get_symbol_profile_family_label(cls, symbol: str) -> str:
        profile = cls.get_backtest_family_profile(symbol)
        return str(profile.get("label") or "Global")

    @classmethod
    def get_backtest_direction_filter_labels(cls) -> Dict[str, str]:
        return dict(cls._DIRECTION_FILTER_LABELS)

    @classmethod
    def get_market_reading_family_configs(cls) -> Dict[str, Dict[str, object]]:
        return {key: dict(value) for key, value in cls._MARKET_READING_FAMILY_CONFIGS.items()}

    @classmethod
    def get_risk_profile_configs(cls) -> Dict[str, Dict[str, object]]:
        return {key: dict(value) for key, value in cls._RISK_PROFILE_CONFIGS.items()}

    @classmethod
    def get_backtest_setup_presets(cls) -> Dict[str, Dict[str, object]]:
        return {key: dict(value) for key, value in cls._BACKTEST_SETUP_PRESETS.items()}

    @classmethod
    def get_backtest_preset_notes(cls) -> Dict[str, str]:
        return dict(cls._BACKTEST_PRESET_NOTES)

    @classmethod
    def get_backtest_preset_updates(cls, preset_name: str) -> Dict[str, object]:
        return dict(cls._BACKTEST_SETUP_PRESETS.get(preset_name, cls._BACKTEST_SETUP_PRESETS[cls.DEFAULT_BACKTEST_PRESET]))

    @classmethod
    def get_backtest_family_profile(cls, symbol: str) -> Dict[str, object]:
        token = str(symbol or "").upper().replace(":USDT", "")
        if token.startswith("BTC/"):
            return {
                "family_key": "btc_core",
                "label": "BTC Core",
                "description": "Benchmark principal da estratégia.",
                "overrides": {},
            }
        if token.startswith("ETH/"):
            return {
                "family_key": "eth_overlay",
                "label": "ETH Overlay",
                "description": "Ativa filtro de tendência por comportamento mais errático.",
                "overrides": {
                    "bt_enable_trend_filter": True,
                    "bt_market_family": "trend_only",
                },
            }
        return {
            "family_key": "global",
            "label": "Global",
            "description": "Sem ajuste adicional.",
            "overrides": {},
        }

    @classmethod
    def get_global_validation_symbols(cls) -> List[str]:
        supported = cls.get_supported_pairs()
        defaults = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        symbols = [sym for sym in defaults if sym in supported]
        return symbols or supported[:3]

    @classmethod
    def get_global_validation_horizons(cls) -> List[int]:
        return [90, 180, 365]

    @classmethod
    def get_runtime_allowed_signal_directions(
        cls,
        timeframe: Optional[str] = None,
        market_state: Optional[str] = None,
        allowed_market_states: Optional[List[str]] = None,
        allowed_setup_types: Optional[List[str]] = None,
    ) -> List[str]:
        del timeframe

        def _flag_from_token(token: str) -> Dict[str, bool]:
            value = str(token or "").strip().lower()
            is_long = any(item in value for item in ("long", "buy", "bull", "compra"))
            is_short = any(item in value for item in ("short", "sell", "bear", "venda"))
            return {"long": is_long, "short": is_short}

        allow_long = False
        allow_short = False

        for setup in allowed_setup_types or []:
            flags = _flag_from_token(setup)
            allow_long = allow_long or flags["long"]
            allow_short = allow_short or flags["short"]

        if not (allow_long or allow_short):
            for state in allowed_market_states or []:
                flags = _flag_from_token(state)
                allow_long = allow_long or flags["long"]
                allow_short = allow_short or flags["short"]

        if not (allow_long or allow_short):
            flags = _flag_from_token(market_state or "")
            allow_long = flags["long"]
            allow_short = flags["short"]

        if not (allow_long or allow_short):
            return list(cls._DIRECTION_FILTER_LABELS.keys())

        directions: List[str] = []
        if allow_long:
            directions.append("COMPRA")
        if allow_short:
            directions.append("VENDA")
        return directions


class _NullExchange:
    name = "null"

    def load_markets(self):
        return {"BTC/USDT": {}, "ETH/USDT": {}}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": None}


class ExchangeConfig:
    @staticmethod
    def get_exchange_instance(exchange_name: str = "binance", testnet: bool = False):
        try:
            import ccxt  # type: ignore
        except Exception:
            return _NullExchange()

        normalized_name = str(exchange_name or "binance").strip().lower()
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_SECRET_KEY", "")

        if normalized_name in {"binanceusdm", "binance-futures", "futures"} and hasattr(ccxt, "binanceusdm"):
            exchange = ccxt.binanceusdm(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                }
            )
        else:
            exchange = ccxt.binance(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "future"},
                }
            )

        if testnet:
            try:
                exchange.set_sandbox_mode(True)
            except Exception:
                pass
        return exchange

    @staticmethod
    def test_connection(exchange_name: str = "binance", testnet: bool = False):
        try:
            exchange = ExchangeConfig.get_exchange_instance(exchange_name=exchange_name, testnet=testnet)
            markets = exchange.load_markets()
            market_count = len(markets or {})
            return True, f"Conexão OK ({exchange_name}) | mercados carregados: {market_count}"
        except Exception as exc:
            return False, f"Falha ao conectar em {exchange_name}: {exc}"


class ProductionConfig:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")

    AI_MODEL_PATH = os.getenv("AI_MODEL_PATH", "data/models/runtime_model.bin")
    AI_MODEL_METADATA_PATH = os.getenv("AI_MODEL_METADATA_PATH", "data/models/runtime_model_metadata.json")
    ENABLE_AI_ASSISTANT = _get_bool("ENABLE_AI_ASSISTANT", False)
    AI_ASSIST_MODE = os.getenv("AI_ASSIST_MODE", "disabled")
    AI_MIN_WIN_PROBABILITY = _get_float("AI_MIN_WIN_PROBABILITY", 0.60)
    AI_COMPARE_BASELINE_DEFAULT = _get_bool("AI_COMPARE_BASELINE_DEFAULT", True)
    BACKTEST_FAST_MODE_DEFAULT = _get_bool("BACKTEST_FAST_MODE_DEFAULT", True)

    ENABLE_DASHBOARD_BACKGROUND_BOT = _get_bool("ENABLE_DASHBOARD_BACKGROUND_BOT", False)
    DASHBOARD_USER_SESSION_TIMEOUT_HOURS = _get_int("DASHBOARD_USER_SESSION_TIMEOUT_HOURS", 24)
    DASHBOARD_MIN_PASSWORD_LENGTH = _get_int("DASHBOARD_MIN_PASSWORD_LENGTH", 10)
    ALLOW_SELF_SERVICE_SIGNUP = _get_bool("ALLOW_SELF_SERVICE_SIGNUP", True)
    REQUIRE_ACTIVE_SUBSCRIPTION_FOR_BOT = _get_bool("REQUIRE_ACTIVE_SUBSCRIPTION_FOR_BOT", True)
    SUBSCRIPTION_EXPIRY_ALERT_DAYS = _get_int("SUBSCRIPTION_EXPIRY_ALERT_DAYS", 3)

    DEFAULT_LIVE_STOP_LOSS_PCT = _get_float("DEFAULT_LIVE_STOP_LOSS_PCT", 0.8)
    DEFAULT_LIVE_TAKE_PROFIT_PCT = _get_float("DEFAULT_LIVE_TAKE_PROFIT_PCT", 1.8)

    PAPER_ACCOUNT_BALANCE = _get_float("PAPER_ACCOUNT_BALANCE", 10000.0)
    PAPER_FEE_RATE = _get_float("PAPER_FEE_RATE", 0.0004)
    PAPER_SLIPPAGE = _get_float("PAPER_SLIPPAGE", 0.0002)
    SL_TP_ONLY_EXIT_MODE = _get_bool("SL_TP_ONLY_EXIT_MODE", False)
    INDICATOR_INTRABAR_PROTECTIVE_EXITS = _get_bool("INDICATOR_INTRABAR_PROTECTIVE_EXITS", True)

    ENABLE_EDGE_GUARDRAIL = _get_bool("ENABLE_EDGE_GUARDRAIL", True)
    MIN_PAPER_TRADES_FOR_EDGE_GUARDRAIL = _get_int("MIN_PAPER_TRADES_FOR_EDGE_GUARDRAIL", 20)
    MIN_PAPER_TRADES_FOR_EDGE_VALIDATION = _get_int("MIN_PAPER_TRADES_FOR_EDGE_VALIDATION", 30)

    ENABLE_RISK_CIRCUIT_BREAKER = _get_bool("ENABLE_RISK_CIRCUIT_BREAKER", True)
    MAX_DAILY_PAPER_LOSS_PCT = _get_float("MAX_DAILY_PAPER_LOSS_PCT", 3.0)
    MAX_CONSECUTIVE_PAPER_LOSSES = _get_int("MAX_CONSECUTIVE_PAPER_LOSSES", 5)
    RISK_DRAWDOWN_WARNING_PCT = _get_float("RISK_DRAWDOWN_WARNING_PCT", 8.0)
    RISK_DRAWDOWN_BLOCK_PCT = _get_float("RISK_DRAWDOWN_BLOCK_PCT", 12.0)
    RISK_STREAK_REDUCTION_THRESHOLD = _get_int("RISK_STREAK_REDUCTION_THRESHOLD", 3)
    RISK_REDUCED_MODE_MULTIPLIER = _get_float("RISK_REDUCED_MODE_MULTIPLIER", 0.5)
    RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", RISK_PER_TRADE_PCT)
    MAX_OPEN_PAPER_TRADES = _get_int("MAX_OPEN_PAPER_TRADES", 1)
    MAX_OPEN_PAPER_TRADES_PER_SYMBOL = _get_int("MAX_OPEN_PAPER_TRADES_PER_SYMBOL", 1)
    MAX_PORTFOLIO_OPEN_RISK_PCT = _get_float("MAX_PORTFOLIO_OPEN_RISK_PCT", 5.0)

    ENABLE_LIVE_EXECUTION = _get_bool("ENABLE_LIVE_EXECUTION", False)
    LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "")
    MAX_REAL_RISK_PER_TRADE_PCT_START = _get_float("MAX_REAL_RISK_PER_TRADE_PCT_START", 0.25)
    MAX_DAILY_REAL_LOSS_PCT = _get_float("MAX_DAILY_REAL_LOSS_PCT", 2.5)
    MAX_CONSECUTIVE_REAL_LOSSES = _get_int("MAX_CONSECUTIVE_REAL_LOSSES", 4)
    MAX_OPEN_REAL_TRADES = _get_int("MAX_OPEN_REAL_TRADES", 1)
    REQUIRE_APPROVED_GOVERNANCE_FOR_LIVE = _get_bool("REQUIRE_APPROVED_GOVERNANCE_FOR_LIVE", True)
    MIN_LIVE_QUALITY_SCORE = _get_float("MIN_LIVE_QUALITY_SCORE", 65.0)

    REQUIRE_ACTIVE_PROFILE_FOR_RUNTIME = _get_bool("REQUIRE_ACTIVE_PROFILE_FOR_RUNTIME", False)
    ENABLE_MULTIUSER_RUNTIME = _get_bool("ENABLE_MULTIUSER_RUNTIME", False)
    ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION = _get_bool("ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION", False)
    REQUIRE_MULTIUSER_VALID_TOKEN = _get_bool("REQUIRE_MULTIUSER_VALID_TOKEN", True)
    REQUIRE_MULTIUSER_VALID_PERMISSIONS = _get_bool("REQUIRE_MULTIUSER_VALID_PERMISSIONS", True)
    REQUIRE_MULTIUSER_RECONCILIATION_OK = _get_bool("REQUIRE_MULTIUSER_RECONCILIATION_OK", True)

    MIN_BACKTEST_TRADES_FOR_PROMOTION = _get_int("MIN_BACKTEST_TRADES_FOR_PROMOTION", 80)
    MIN_PROMOTION_SETUP_TRADES = _get_int("MIN_PROMOTION_SETUP_TRADES", 20)
    MIN_PROMOTION_PERIOD_DAYS = _get_int("MIN_PROMOTION_PERIOD_DAYS", 90)
    MIN_PROMOTION_PROFIT_FACTOR = _get_float("MIN_PROMOTION_PROFIT_FACTOR", 1.10)
    MIN_PROMOTION_EXPECTANCY_PCT = _get_float("MIN_PROMOTION_EXPECTANCY_PCT", 0.01)
    MIN_PROMOTION_OOS_TRADES = _get_int("MIN_PROMOTION_OOS_TRADES", 20)
    MIN_PROMOTION_OOS_PROFIT_FACTOR = _get_float("MIN_PROMOTION_OOS_PROFIT_FACTOR", 1.05)
    MIN_PROMOTION_OOS_EXPECTANCY_PCT = _get_float("MIN_PROMOTION_OOS_EXPECTANCY_PCT", 0.0)
    MAX_PROMOTION_DRAWDOWN = _get_float("MAX_PROMOTION_DRAWDOWN", 25.0)
    MIN_WALK_FORWARD_PASS_RATE_PCT = _get_float("MIN_WALK_FORWARD_PASS_RATE_PCT", 50.0)
    MIN_WALK_FORWARD_OOS_PROFIT_FACTOR = _get_float("MIN_WALK_FORWARD_OOS_PROFIT_FACTOR", 1.0)
    MAX_STATISTICAL_PROFIT_FACTOR = _get_float("MAX_STATISTICAL_PROFIT_FACTOR", 10.0)

    GOVERNANCE_LOOKBACK_DAYS = _get_int("GOVERNANCE_LOOKBACK_DAYS", 90)
    GOVERNANCE_LOOKBACK_TRADES = _get_int("GOVERNANCE_LOOKBACK_TRADES", 120)
    GOVERNANCE_MIN_REGIME_TRADES = _get_int("GOVERNANCE_MIN_REGIME_TRADES", 20)
    GOVERNANCE_MIN_ALIGNMENT_TRADES = _get_int("GOVERNANCE_MIN_ALIGNMENT_TRADES", 12)
    GOVERNANCE_MIN_EXPECTANCY_PCT = _get_float("GOVERNANCE_MIN_EXPECTANCY_PCT", 0.0)
    GOVERNANCE_APPROVED_PF = _get_float("GOVERNANCE_APPROVED_PF", 1.10)
    GOVERNANCE_REDUCED_PF = _get_float("GOVERNANCE_REDUCED_PF", 0.95)
    GOVERNANCE_REDUCED_SIZE_MULTIPLIER = _get_float("GOVERNANCE_REDUCED_SIZE_MULTIPLIER", 0.5)

    GOVERNANCE_ALIGNMENT_WARNING_PF_MULTIPLIER = _get_float("GOVERNANCE_ALIGNMENT_WARNING_PF_MULTIPLIER", 0.9)
    GOVERNANCE_ALIGNMENT_BROKEN_PF_MULTIPLIER = _get_float("GOVERNANCE_ALIGNMENT_BROKEN_PF_MULTIPLIER", 0.75)
    GOVERNANCE_ALIGNMENT_WARNING_EXPECTANCY_MULTIPLIER = _get_float("GOVERNANCE_ALIGNMENT_WARNING_EXPECTANCY_MULTIPLIER", 0.8)
    GOVERNANCE_ALIGNMENT_BROKEN_EXPECTANCY_MULTIPLIER = _get_float("GOVERNANCE_ALIGNMENT_BROKEN_EXPECTANCY_MULTIPLIER", 0.6)
    GOVERNANCE_ALIGNMENT_WARNING_WINRATE_GAP = _get_float("GOVERNANCE_ALIGNMENT_WARNING_WINRATE_GAP", 5.0)
    GOVERNANCE_ALIGNMENT_BROKEN_WINRATE_GAP = _get_float("GOVERNANCE_ALIGNMENT_BROKEN_WINRATE_GAP", 10.0)
    GOVERNANCE_MAX_PROFIT_GIVEBACK_WARNING_PCT = _get_float("GOVERNANCE_MAX_PROFIT_GIVEBACK_WARNING_PCT", 65.0)
    GOVERNANCE_MAX_PROFIT_GIVEBACK_BLOCK_PCT = _get_float("GOVERNANCE_MAX_PROFIT_GIVEBACK_BLOCK_PCT", 85.0)

    CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")

    BINANCE_USER_STREAM_KEEPALIVE_SECONDS = _get_int("BINANCE_USER_STREAM_KEEPALIVE_SECONDS", 30 * 60)
    BINANCE_USER_STREAM_RECONNECT_SECONDS = _get_int("BINANCE_USER_STREAM_RECONNECT_SECONDS", 12 * 60 * 60)
    BINANCE_USER_STREAM_MAINNET_WS_URL = os.getenv(
        "BINANCE_USER_STREAM_MAINNET_WS_URL",
        "wss://fstream.binance.com/ws",
    )
    BINANCE_USER_STREAM_TESTNET_WS_URL = os.getenv(
        "BINANCE_USER_STREAM_TESTNET_WS_URL",
        "wss://stream.binancefuture.com/ws",
    )

    REDIS_URL = os.getenv("REDIS_URL", "")

    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "")
    STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "")
    PREMIUM_PRICE_WEEKLY = _get_float("PREMIUM_PRICE_WEEKLY", 0.0)
    PREMIUM_PRICE_MONTHLY = _get_float("PREMIUM_PRICE_MONTHLY", 0.0)
    PREMIUM_PRICE_YEARLY = _get_float("PREMIUM_PRICE_YEARLY", 0.0)


__all__ = [
    # module-level strategy constants
    "SYMBOL",
    "TIMEFRAME",
    "LIMIT",
    "TESTNET",
    "BACKTEST_USE_TESTNET",
    "BACKTEST_USE_LOCAL_CSV",
    "BACKTEST_REQUIRE_LOCAL_CSV",
    "BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP",
    "BOT_ALLOW_REST_FALLBACK",
    "BOT_WEBSOCKET_TIMEOUT_SEC",
    "BOT_BOOTSTRAP_CANDLES",
    "POLL_SECONDS",
    "LEVERAGE",
    "RISK_PER_TRADE_PCT",
    "MAX_OPEN_TRADES",
    "FAST_EMA",
    "SLOW_EMA",
    "TREND_EMA",
    "RSI_PERIOD",
    "ATR_PERIOD",
    "LONG_SLOPE_LOOKBACK",
    "SHORT_SLOPE_LOOKBACK",
    "SHORT_RSI_MIN",
    "ENABLE_SHORT_PULLBACK",
    "ENABLE_SHORT_RESUME",
    "BUY_RSI_SIGNAL",
    "SELL_RSI_SIGNAL",
    "LONG_PULLBACK_MIN_TREND_STRENGTH_PCT",
    "LONG_MIN_ATR_PCT",
    "SHORT_MIN_ATR_PCT",
    "LONG_TREND_GAP_PCT",
    "SHORT_TREND_GAP_PCT",
    "LONG_FAST_SLOW_GAP_PCT",
    "SHORT_FAST_SLOW_GAP_PCT",
    "LONG_STOP_LOSS_PCT",
    "SHORT_STOP_LOSS_PCT",
    "LONG_TAKE_PROFIT_PCT",
    "SHORT_TAKE_PROFIT_PCT",
    "LONG_TRAILING_STOP_PCT",
    "SHORT_TRAILING_STOP_PCT",
    "TRAILING_TRIGGER_PCT",
    "PARTIAL_TARGET_PCT",
    "MIN_TARGET_DISTANCE_PCT",
    "MIN_TREND_STRENGTH_PCT",
    "MIN_TREND_STRENGTH_PCT_SHORT",
    "ALLOW_LONG",
    "ALLOW_SHORT",
    "BLOCK_UNKNOWN_REGIME",
    "USE_NEXT_CANDLE_OPEN_FOR_BACKTEST",
    "FEE_PCT",
    "LIVE_TRADING_CONFIRMATION",
    "MAX_REAL_RISK_PER_TRADE_PCT_START",
    "MAX_DAILY_REAL_LOSS_PCT",
    "MAX_CONSECUTIVE_REAL_LOSSES",
    "MAX_OPEN_REAL_TRADES",
    # app classes
    "AppConfig",
    "ExchangeConfig",
    "ProductionConfig",
]
