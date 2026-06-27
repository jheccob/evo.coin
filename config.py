from __future__ import annotations

import json
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


def _get_int_list(name: str, default: List[int]) -> List[int]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    values = []
    for item in str(raw).split(","):
        token = str(item).strip()
        if not token:
            continue
        try:
            values.append(int(token))
        except (TypeError, ValueError):
            continue
    return values or list(default)


# Core Runtime / Strategy Defaults

SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
LIMIT = _get_int("LIMIT", 200)

TESTNET = _get_bool("TESTNET", BINANCE_TESTNET)
BACKTEST_USE_TESTNET = _get_bool("BACKTEST_USE_TESTNET", False)
BACKTEST_USE_LOCAL_CSV = _get_bool("BACKTEST_USE_LOCAL_CSV", False)
BACKTEST_REQUIRE_LOCAL_CSV = _get_bool("BACKTEST_REQUIRE_LOCAL_CSV", False)
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP = _get_bool("BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP", True)
BOT_ALLOW_REST_FALLBACK = _get_bool("BOT_ALLOW_REST_FALLBACK", True)
BOT_WEBSOCKET_TIMEOUT_SEC = _get_float("BOT_WEBSOCKET_TIMEOUT_SEC", 25.0)
BOT_BOOTSTRAP_CANDLES = _get_int("BOT_BOOTSTRAP_CANDLES", max(LIMIT, 500))
BOT_WAIT_NEXT_CLOSED_CANDLE_ON_REAL_STARTUP = _get_bool("BOT_WAIT_NEXT_CLOSED_CANDLE_ON_REAL_STARTUP", True)
BOT_REENTRY_COOLDOWN_CANDLES = _get_int("BOT_REENTRY_COOLDOWN_CANDLES", 1)
BINANCE_RECV_WINDOW_MS = _get_int("BINANCE_RECV_WINDOW_MS", 60000)
BOT_TRAILING_ONLY_WHEN_POSITION_ALIGNED = _get_bool("BOT_TRAILING_ONLY_WHEN_POSITION_ALIGNED", True)
HISTORY_DATA_DIR = os.getenv("HISTORY_DATA_DIR", os.path.join("data", "history"))
SYMBOL_APPROVALS_PATH = os.getenv(
    "SYMBOL_APPROVALS_PATH",
    os.path.join("reports", "validation", "symbol_approvals.json"),
)
SYMBOL_STRATEGY_OVERRIDES_PATH = os.getenv(
    "SYMBOL_STRATEGY_OVERRIDES_PATH",
    os.path.join("reports", "validation", "symbol_strategy_overrides.json"),
)
SAVED_STRATEGY_PROFILES_PATH = os.getenv(
    "SAVED_STRATEGY_PROFILES_PATH",
    os.path.join("reports", "validation", "saved_strategy_profiles.json"),
)
RUNTIME_REQUIRE_APPROVED_SYMBOL = _get_bool("RUNTIME_REQUIRE_APPROVED_SYMBOL", True)
RUNTIME_SYMBOL_APPROVAL_OVERRIDE = _get_bool("RUNTIME_SYMBOL_APPROVAL_OVERRIDE", False)
RUNTIME_ALLOW_WATCHLIST_IN_TESTNET = _get_bool("RUNTIME_ALLOW_WATCHLIST_IN_TESTNET", True)
ALT_STRICT_CONTEXT_FILTER = _get_bool("ALT_STRICT_CONTEXT_FILTER", True)
ALT_MIN_CONTEXT_GAP_PCT = _get_float("ALT_MIN_CONTEXT_GAP_PCT", 0.22)
ALT_MIN_GLOBAL_ATR_PCT = _get_float("ALT_MIN_GLOBAL_ATR_PCT", 0.0)
POLL_SECONDS = _get_int("POLL_SECONDS", 30)
LEVERAGE = _get_int("LEVERAGE", 10)
POSITION_SIZING_MODE = os.getenv("POSITION_SIZING_MODE", "allocation").strip().lower() or "allocation"
POSITION_MARGIN_ALLOCATION_PCT = _get_float("POSITION_MARGIN_ALLOCATION_PCT", 100.0)
SINGLE_USER_RUNTIME_USER_ID = _get_int("SINGLE_USER_RUNTIME_USER_ID", 0)
SINGLE_USER_RUNTIME_ACCOUNT_ID = os.getenv("SINGLE_USER_RUNTIME_ACCOUNT_ID", "env-primary")
SINGLE_USER_RUNTIME_ACCOUNT_ALIAS = os.getenv("SINGLE_USER_RUNTIME_ACCOUNT_ALIAS", "Primary Env Account")
SINGLE_USER_RUNTIME_EXCHANGE = os.getenv("SINGLE_USER_RUNTIME_EXCHANGE", "binanceusdm")
# Baseline operacional alinhado para um go-live conservador.
# Mantemos o runtime em TESTNET por padrão, mas a estrutura de risco
# já nasce compatível com um piloto real pequeno quando o live for liberado.
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 2.0)
MAX_OPEN_TRADES = _get_int("MAX_OPEN_TRADES", 1)

FAST_EMA = _get_int("FAST_EMA", 21)
SLOW_EMA = _get_int("SLOW_EMA", 50)
TREND_EMA = _get_int("TREND_EMA", 200)

RSI_PERIOD = _get_int("RSI_PERIOD", 14)
ATR_PERIOD = _get_int("ATR_PERIOD", 14)
ADX_PERIOD = _get_int("ADX_PERIOD", 14)
VOLUME_MA_PERIOD = _get_int("VOLUME_MA_PERIOD", 20)
ENABLE_VOLUME_MA_ENTRY_FILTER = _get_bool("ENABLE_VOLUME_MA_ENTRY_FILTER", False)
VOLUME_MA_ENTRY_MULTIPLIER = _get_float("VOLUME_MA_ENTRY_MULTIPLIER", 1.0)
MACD_FAST_PERIOD = _get_int("MACD_FAST_PERIOD", 12)
MACD_SLOW_PERIOD = _get_int("MACD_SLOW_PERIOD", 26)
MACD_SIGNAL_PERIOD = _get_int("MACD_SIGNAL_PERIOD", 9)
ENABLE_MACD_ENTRY_FILTER = _get_bool("ENABLE_MACD_ENTRY_FILTER", False)
MACD_ENTRY_FILTER_MODE = os.getenv("MACD_ENTRY_FILTER_MODE", "histogram").strip().lower() or "histogram"

LONG_SLOPE_LOOKBACK = _get_int("LONG_SLOPE_LOOKBACK", 8)
LONG_TREND_EMA_LOOKBACK = _get_int("LONG_TREND_EMA_LOOKBACK", 3)
SHORT_SLOPE_LOOKBACK = _get_int("SHORT_SLOPE_LOOKBACK", 5)
SHORT_TREND_EMA_LOOKBACK = _get_int("SHORT_TREND_EMA_LOOKBACK", 3)

PULLBACK_BUFFER_PCT = _get_float("PULLBACK_BUFFER_PCT", 0.25)
SHORT_RSI_MIN = _get_float("SHORT_RSI_MIN", 0.0)
ADX_THRESHOLD = _get_float("ADX_THRESHOLD", 25)
LONG_ADX_THRESHOLD = _get_float("LONG_ADX_THRESHOLD", 23)
# Short em 15m: mantemos o filtro seletivo, mas com uma abertura pequena
# para recuperar frequencia sem desmontar a qualidade anual.
SHORT_ADX_THRESHOLD = _get_float("SHORT_ADX_THRESHOLD", 35)
VOLUME_RATIO_REQUIRED = _get_float("VOLUME_RATIO_REQUIRED", 1.5)
LONG_VOLUME_RATIO_REQUIRED = _get_float("LONG_VOLUME_RATIO_REQUIRED", 1.5)
SHORT_VOLUME_RATIO_REQUIRED = _get_float("SHORT_VOLUME_RATIO_REQUIRED", 1.0)
ENABLE_SHORT_RESUME = _get_bool("ENABLE_SHORT_RESUME", True)

BUY_RSI_SIGNAL = _get_float("BUY_RSI_SIGNAL", 55.0)
SELL_RSI_SIGNAL = _get_float("SELL_RSI_SIGNAL", 35.0)

LONG_PULLBACK_MIN_TREND_STRENGTH_PCT = _get_float("LONG_PULLBACK_MIN_TREND_STRENGTH_PCT", 0.22)
SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT = _get_float("SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT", 0.28)

LONG_MIN_ATR_PCT = _get_float("LONG_MIN_ATR_PCT", 0.15)
SHORT_MIN_ATR_PCT = _get_float("SHORT_MIN_ATR_PCT", 0.30)

LONG_TREND_GAP_PCT = _get_float("LONG_TREND_GAP_PCT", 0.20)
SHORT_TREND_GAP_PCT = _get_float("SHORT_TREND_GAP_PCT", 0.34)

LONG_FAST_SLOW_GAP_PCT = _get_float("LONG_FAST_SLOW_GAP_PCT", 0.05)
SHORT_FAST_SLOW_GAP_PCT = _get_float("SHORT_FAST_SLOW_GAP_PCT", 0.08)
PULLBACK_LONG_COUNT_BREAKOUT_SCORE = _get_bool("PULLBACK_LONG_COUNT_BREAKOUT_SCORE", True)
MAX_PULLBACK_LONG_SCORE = _get_int("MAX_PULLBACK_LONG_SCORE", 8)
LONG_PULLBACK_HOT_CONTEXT_GAP_PCT = _get_float("LONG_PULLBACK_HOT_CONTEXT_GAP_PCT", 0.85)
LONG_PULLBACK_HOT_ATR_PCT = _get_float("LONG_PULLBACK_HOT_ATR_PCT", 0.40)
LONG_RESUME_HOT_CONTEXT_GAP_PCT = _get_float("LONG_RESUME_HOT_CONTEXT_GAP_PCT", 0.88)
LONG_PULLBACK_AS_RESUME_WHEN_DISABLED = _get_bool("LONG_PULLBACK_AS_RESUME_WHEN_DISABLED", False)
PULLBACK_LONG_MIN_ADX = _get_float("PULLBACK_LONG_MIN_ADX", 0.0)
PULLBACK_LONG_MAX_CONTEXT_GAP_PCT = _get_float("PULLBACK_LONG_MAX_CONTEXT_GAP_PCT", 1.2)
PULLBACK_LONG_MIN_RSI = _get_float("PULLBACK_LONG_MIN_RSI", 40.0)
PULLBACK_LONG_MAX_RSI = _get_float("PULLBACK_LONG_MAX_RSI", 68.0)
SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT = _get_float("SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT", 0.50)
SHORT_PULLBACK_MIN_ADX = _get_float("SHORT_PULLBACK_MIN_ADX", 50.0)
EXPERIMENTAL_LONG_SIDE_LOGIC = _get_bool("EXPERIMENTAL_LONG_SIDE_LOGIC", True)
EXPERIMENTAL_SHORT_SIDE_LOGIC = _get_bool("EXPERIMENTAL_SHORT_SIDE_LOGIC", False)

LONG_STOP_LOSS_PCT = _get_float("LONG_STOP_LOSS_PCT", 1.5) 
SHORT_STOP_LOSS_PCT = _get_float("SHORT_STOP_LOSS_PCT", 1.2)
LONG_TAKE_PROFIT_PCT = _get_float("LONG_TAKE_PROFIT_PCT", 2.9)
SHORT_TAKE_PROFIT_PCT = _get_float("SHORT_TAKE_PROFIT_PCT", 3.0)
LONG_TRAILING_STOP_PCT = _get_float("LONG_TRAILING_STOP_PCT", 0.8)
SHORT_TRAILING_STOP_PCT = _get_float("SHORT_TRAILING_STOP_PCT", 0.7)
TRAILING_TRIGGER_PCT = _get_float("TRAILING_TRIGGER_PCT", 1.5)
PARTIAL_TARGET_PCT = _get_float("PARTIAL_TARGET_PCT", 1.0)
ENFORCE_MIN_RISK_REWARD_RATIO = _get_bool("ENFORCE_MIN_RISK_REWARD_RATIO", False)
MIN_RISK_REWARD_RATIO = _get_float("MIN_RISK_REWARD_RATIO", 2.0)
TREND_RESUME_LONG_STOP_LOSS_PCT = _get_float("TREND_RESUME_LONG_STOP_LOSS_PCT", 0.9)
TREND_RESUME_LONG_PARTIAL_TARGET_PCT = _get_float("TREND_RESUME_LONG_PARTIAL_TARGET_PCT", PARTIAL_TARGET_PCT)
TREND_RESUME_LONG_TRAILING_TRIGGER_PCT = _get_float(
    "TREND_RESUME_LONG_TRAILING_TRIGGER_PCT",
    TRAILING_TRIGGER_PCT,
)
TREND_RESUME_LONG_TRAILING_STOP_PCT = _get_float(
    "TREND_RESUME_LONG_TRAILING_STOP_PCT",
    LONG_TRAILING_STOP_PCT,
)
TREND_RESUME_LONG_USE_FIXED_STOP = _get_bool("TREND_RESUME_LONG_USE_FIXED_STOP", True)
TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT = _get_float("TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT", 0.60)
TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT = _get_float("TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT", 0.0)
TREND_RESUME_LONG_MIN_ADX = _get_float("TREND_RESUME_LONG_MIN_ADX", 23.0)
TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT = _get_float("TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT", 0.25)
TREND_RESUME_LONG_MAX_RSI = _get_float("TREND_RESUME_LONG_MAX_RSI", 0.0)
TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE = _get_bool(
    "TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE",
    False,
)
TREND_RESUME_SHORT_STOP_LOSS_PCT = _get_float("TREND_RESUME_SHORT_STOP_LOSS_PCT", 2.0)
TREND_RESUME_SHORT_PARTIAL_TARGET_PCT = _get_float("TREND_RESUME_SHORT_PARTIAL_TARGET_PCT", 2.4)
TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT = _get_float(
    "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT",
    2.2,
)
TREND_RESUME_SHORT_TRAILING_STOP_PCT = _get_float(
    "TREND_RESUME_SHORT_TRAILING_STOP_PCT",
    0.8,
)
TREND_RESUME_SHORT_USE_FIXED_STOP = _get_bool("TREND_RESUME_SHORT_USE_FIXED_STOP", False)
TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION = _get_bool(
    "TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION",
    True,
)
TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT = _get_float("TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT", 0.0)
TREND_RESUME_SHORT_MIN_ADX = _get_float("TREND_RESUME_SHORT_MIN_ADX", 0.0)
TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION = _get_bool(
    "TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION",
    False,
)
TREND_RESUME_SHORT_BLOCKED_ENTRY_HOURS_UTC = _get_int_list(
    "TREND_RESUME_SHORT_BLOCKED_ENTRY_HOURS_UTC",
    [],
)
PULLBACK_LONG_STOP_LOSS_PCT = _get_float("PULLBACK_LONG_STOP_LOSS_PCT", 1.7)
PULLBACK_LONG_PARTIAL_TARGET_PCT = _get_float("PULLBACK_LONG_PARTIAL_TARGET_PCT", PARTIAL_TARGET_PCT)
PULLBACK_LONG_TRAILING_TRIGGER_PCT = _get_float(
    "PULLBACK_LONG_TRAILING_TRIGGER_PCT",
    1.4,
)
PULLBACK_LONG_TRAILING_STOP_PCT = _get_float(
    "PULLBACK_LONG_TRAILING_STOP_PCT",
    0.6,
)
PULLBACK_LONG_USE_FIXED_STOP = _get_bool("PULLBACK_LONG_USE_FIXED_STOP", False)
MARKET_READING_LONG_STOP_LOSS_PCT = _get_float("MARKET_READING_LONG_STOP_LOSS_PCT", 1.25)
MARKET_READING_LONG_PARTIAL_TARGET_PCT = _get_float("MARKET_READING_LONG_PARTIAL_TARGET_PCT", 1.0)
MARKET_READING_LONG_TRAILING_TRIGGER_PCT = _get_float("MARKET_READING_LONG_TRAILING_TRIGGER_PCT", PARTIAL_TARGET_PCT)
MARKET_READING_LONG_TRAILING_STOP_PCT = _get_float("MARKET_READING_LONG_TRAILING_STOP_PCT", 0.45)
MARKET_READING_LONG_USE_FIXED_STOP = _get_bool("MARKET_READING_LONG_USE_FIXED_STOP", False)
PULLBACK_SHORT_STOP_LOSS_PCT = _get_float("PULLBACK_SHORT_STOP_LOSS_PCT", SHORT_STOP_LOSS_PCT)
PULLBACK_SHORT_PARTIAL_TARGET_PCT = _get_float(
    "PULLBACK_SHORT_PARTIAL_TARGET_PCT",
    PARTIAL_TARGET_PCT,
)
PULLBACK_SHORT_TRAILING_TRIGGER_PCT = _get_float(
    "PULLBACK_SHORT_TRAILING_TRIGGER_PCT",
    TRAILING_TRIGGER_PCT,
)
PULLBACK_SHORT_TRAILING_STOP_PCT = _get_float(
    "PULLBACK_SHORT_TRAILING_STOP_PCT",
    SHORT_TRAILING_STOP_PCT,
)
PULLBACK_SHORT_USE_FIXED_STOP = _get_bool("PULLBACK_SHORT_USE_FIXED_STOP", False)
RELIEF_RALLY_SHORT_STOP_LOSS_PCT = _get_float("RELIEF_RALLY_SHORT_STOP_LOSS_PCT", 1.4)
RELIEF_RALLY_SHORT_PARTIAL_TARGET_PCT = _get_float("RELIEF_RALLY_SHORT_PARTIAL_TARGET_PCT", PARTIAL_TARGET_PCT)
RELIEF_RALLY_SHORT_TRAILING_TRIGGER_PCT = _get_float(
    "RELIEF_RALLY_SHORT_TRAILING_TRIGGER_PCT",
    1.8,
)
RELIEF_RALLY_SHORT_TRAILING_STOP_PCT = _get_float(
    "RELIEF_RALLY_SHORT_TRAILING_STOP_PCT",
    0.4,
)
RELIEF_RALLY_SHORT_USE_FIXED_STOP = _get_bool("RELIEF_RALLY_SHORT_USE_FIXED_STOP", False)
MARKET_READING_SHORT_STOP_LOSS_PCT = _get_float("MARKET_READING_SHORT_STOP_LOSS_PCT", 1.15)
MARKET_READING_SHORT_PARTIAL_TARGET_PCT = _get_float("MARKET_READING_SHORT_PARTIAL_TARGET_PCT", 1.0)
MARKET_READING_SHORT_TRAILING_TRIGGER_PCT = _get_float("MARKET_READING_SHORT_TRAILING_TRIGGER_PCT", PARTIAL_TARGET_PCT)
MARKET_READING_SHORT_TRAILING_STOP_PCT = _get_float("MARKET_READING_SHORT_TRAILING_STOP_PCT", 0.45)
MARKET_READING_SHORT_USE_FIXED_STOP = _get_bool("MARKET_READING_SHORT_USE_FIXED_STOP", False)
MIN_TARGET_DISTANCE_PCT = _get_float("MIN_TARGET_DISTANCE_PCT", 0.45)
ENABLE_LONG_PULLBACK = _get_bool("ENABLE_LONG_PULLBACK", True)
ENABLE_LONG_RESUME = _get_bool("ENABLE_LONG_RESUME", True)
ENABLE_SHORT_PULLBACK = _get_bool("ENABLE_SHORT_PULLBACK", True)
ENABLE_SHORT_RELIEF_RALLY = _get_bool("ENABLE_SHORT_RELIEF_RALLY", True)
SHORT_BREAKDOWN_BUFFER_PCT = _get_float("SHORT_BREAKDOWN_BUFFER_PCT", 0.12)
SHORT_REQUIRE_STRICT_REGIME = _get_bool("SHORT_REQUIRE_STRICT_REGIME", True)
ALLOW_TRIGGERLESS_ENTRIES = _get_bool("ALLOW_TRIGGERLESS_ENTRIES", False)
BYPASS_WEAK_REGIME_GATE = _get_bool("BYPASS_WEAK_REGIME_GATE", False)
ALLOW_WEAK_BULL_ATR_LONG_ENTRIES = _get_bool("ALLOW_WEAK_BULL_ATR_LONG_ENTRIES", True)
# Filtro legado por horario. Fica desligado por padrao porque o bot precisa
# operar em qualquer janela, sem depender de agenda fixa.
USE_ENTRY_HOUR_BLOCKS = _get_bool("USE_ENTRY_HOUR_BLOCKS", False)
BLOCKED_LONG_ENTRY_HOURS_UTC = _get_int_list("BLOCKED_LONG_ENTRY_HOURS_UTC", [])
BLOCKED_SHORT_ENTRY_HOURS_UTC = _get_int_list("BLOCKED_SHORT_ENTRY_HOURS_UTC", [])

MIN_TREND_STRENGTH_PCT = _get_float("MIN_TREND_STRENGTH_PCT", 0.15)
MIN_TREND_STRENGTH_PCT_SHORT = _get_float("MIN_TREND_STRENGTH_PCT_SHORT", 0.28)

# Filtros de Price Action e Volatilidade
# Desativado por padrao para nao bloquear oportunidades em regimes de ATR comprimido.
GLOBAL_MIN_ATR_PCT = _get_float("GLOBAL_MIN_ATR_PCT", 0.0)
CANDLE_WICK_REJECTION_RATIO = _get_float("CANDLE_WICK_REJECTION_RATIO", 0.4)
CANDLE_WICK_REJECTION_RATIO_SHORT_RELIEF = _get_float("CANDLE_WICK_REJECTION_RATIO_SHORT_RELIEF", 0.6)
RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT = _get_float("RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT", 0.40)
RELIEF_RALLY_SHORT_MIN_ADX = _get_float("RELIEF_RALLY_SHORT_MIN_ADX", 35.0)
SHORT_RSI_MIN_RELIEF_RALLY = _get_float("SHORT_RSI_MIN_RELIEF_RALLY", 55.0)
SHORT_RSI_MAX_RELIEF_RALLY = _get_float("SHORT_RSI_MAX_RELIEF_RALLY", 58.0)

LONG_MAX_DISTANCE_EMA_PCT = _get_float("LONG_MAX_DISTANCE_EMA_PCT", 3.5)
SHORT_MAX_DISTANCE_EMA_PCT = _get_float("SHORT_MAX_DISTANCE_EMA_PCT", 3.0)

ALLOW_LONG = _get_bool("ALLOW_LONG", True)
ALLOW_SHORT = _get_bool("ALLOW_SHORT", True)
BLOCK_UNKNOWN_REGIME = _get_bool("BLOCK_UNKNOWN_REGIME", True)
MIN_LONG_SCORE = _get_int("MIN_LONG_SCORE", 7)
MIN_SHORT_SCORE = _get_int("MIN_SHORT_SCORE", 7)
DISABLE_SHORT_SCORE_GATE = _get_bool("DISABLE_SHORT_SCORE_GATE", True)

USE_NEXT_CANDLE_OPEN_FOR_BACKTEST = _get_bool("USE_NEXT_CANDLE_OPEN_FOR_BACKTEST", True)
EXECUTION_PROFILE = str(os.getenv("EXECUTION_PROFILE", "managed")).strip().lower() or "managed"
FEE_PCT = _get_float("FEE_PCT", 0.08)
SLIPPAGE_PCT = _get_float("SLIPPAGE_PCT", 0.02)

# Production safety guards (runtime)
LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "")
MAX_REAL_RISK_PER_TRADE_PCT_START = _get_float("MAX_REAL_RISK_PER_TRADE_PCT_START", 2.0)
MAX_DAILY_REAL_LOSS_PCT = _get_float("MAX_DAILY_REAL_LOSS_PCT", 2.0)
MAX_CONSECUTIVE_REAL_LOSSES = _get_int("MAX_CONSECUTIVE_REAL_LOSSES", 3)
MAX_OPEN_REAL_TRADES = _get_int("MAX_OPEN_REAL_TRADES", 1)


def build_runtime_strategy_snapshot(context_timeframe: Optional[str] = None) -> Dict[str, object]:
    """
    Snapshot oficial do baseline ativo no runtime.
    Mantém um retrato único do setup para logs, auditoria e comparações.
    """
    from database.database import build_strategy_version

    resolved_context = context_timeframe or os.getenv("APP_PRIMARY_CONTEXT_TIMEFRAME", "1h")
    strategy_version = build_strategy_version(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        rsi_period=RSI_PERIOD,
        rsi_min=int(BUY_RSI_SIGNAL),
        rsi_max=int(SELL_RSI_SIGNAL),
        stop_loss_pct=float(LONG_STOP_LOSS_PCT),
        take_profit_pct=float(LONG_TAKE_PROFIT_PCT),
        require_volume=False,
        require_trend=False,
        avoid_ranging=False,
        context_timeframe=resolved_context,
    )

    return {
        "strategy_version": strategy_version,
        "symbol": SYMBOL,
          "timeframe": TIMEFRAME,
          "position_sizing_mode": str(POSITION_SIZING_MODE),
          "position_margin_allocation_pct": float(POSITION_MARGIN_ALLOCATION_PCT),
          "leverage": int(LEVERAGE),
          "context_timeframe": resolved_context,
        "rsi_period": int(RSI_PERIOD),
        "buy_rsi_signal": float(BUY_RSI_SIGNAL),
        "sell_rsi_signal": float(SELL_RSI_SIGNAL),
        "allow_long": bool(ALLOW_LONG),
        "allow_short": bool(ALLOW_SHORT),
        "block_unknown_regime": bool(BLOCK_UNKNOWN_REGIME),
        "enable_long_pullback": bool(ENABLE_LONG_PULLBACK),
        "enable_long_resume": bool(ENABLE_LONG_RESUME),
        "enable_short_pullback": bool(ENABLE_SHORT_PULLBACK),
        "enable_short_relief_rally": bool(ENABLE_SHORT_RELIEF_RALLY),
        "enable_short_resume": bool(ENABLE_SHORT_RESUME),
        "allow_triggerless_entries": bool(ALLOW_TRIGGERLESS_ENTRIES),
        "bypass_weak_regime_gate": bool(BYPASS_WEAK_REGIME_GATE),
        "disable_short_score_gate": bool(DISABLE_SHORT_SCORE_GATE),
        "short_rsi_min": float(SHORT_RSI_MIN),
        "long_slope_lookback": int(LONG_SLOPE_LOOKBACK),
        "long_trend_ema_lookback": int(LONG_TREND_EMA_LOOKBACK),
        "short_slope_lookback": int(SHORT_SLOPE_LOOKBACK),
        "short_trend_ema_lookback": int(SHORT_TREND_EMA_LOOKBACK),
        "long_pullback_min_trend_strength_pct": float(LONG_PULLBACK_MIN_TREND_STRENGTH_PCT),
        "short_pullback_min_trend_strength_pct": float(SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT),
        "min_trend_strength_pct_long": float(MIN_TREND_STRENGTH_PCT),
        "min_trend_strength_pct_short": float(MIN_TREND_STRENGTH_PCT_SHORT),
        "long_adx_threshold": float(LONG_ADX_THRESHOLD),
        "short_adx_threshold": float(SHORT_ADX_THRESHOLD),
        "volume_ma_period": int(VOLUME_MA_PERIOD),
        "enable_volume_ma_entry_filter": bool(ENABLE_VOLUME_MA_ENTRY_FILTER),
        "volume_ma_entry_multiplier": float(VOLUME_MA_ENTRY_MULTIPLIER),
        "long_volume_ratio_required": float(LONG_VOLUME_RATIO_REQUIRED),
        "short_volume_ratio_required": float(SHORT_VOLUME_RATIO_REQUIRED),
        "enable_macd_entry_filter": bool(ENABLE_MACD_ENTRY_FILTER),
        "macd_entry_filter_mode": str(MACD_ENTRY_FILTER_MODE),
        "macd_fast_period": int(MACD_FAST_PERIOD),
        "macd_slow_period": int(MACD_SLOW_PERIOD),
        "macd_signal_period": int(MACD_SIGNAL_PERIOD),
        "short_trend_gap_pct": float(SHORT_TREND_GAP_PCT),
        "long_fast_slow_gap_pct": float(LONG_FAST_SLOW_GAP_PCT),
        "short_fast_slow_gap_pct": float(SHORT_FAST_SLOW_GAP_PCT),
        "pullback_long_count_breakout_score": bool(PULLBACK_LONG_COUNT_BREAKOUT_SCORE),
        "max_pullback_long_score": int(MAX_PULLBACK_LONG_SCORE),
        "long_pullback_hot_context_gap_pct": float(LONG_PULLBACK_HOT_CONTEXT_GAP_PCT),
        "long_pullback_hot_atr_pct": float(LONG_PULLBACK_HOT_ATR_PCT),
        "long_pullback_as_resume_when_disabled": bool(LONG_PULLBACK_AS_RESUME_WHEN_DISABLED),
        "pullback_long_min_adx": float(PULLBACK_LONG_MIN_ADX),
        "pullback_long_max_context_gap_pct": float(PULLBACK_LONG_MAX_CONTEXT_GAP_PCT),
        "pullback_long_min_rsi": float(PULLBACK_LONG_MIN_RSI),
        "pullback_long_max_rsi": float(PULLBACK_LONG_MAX_RSI),
        "long_resume_hot_context_gap_pct": float(LONG_RESUME_HOT_CONTEXT_GAP_PCT),
        "long_stop_loss_pct": float(LONG_STOP_LOSS_PCT),
        "short_stop_loss_pct": float(SHORT_STOP_LOSS_PCT),
        "long_take_profit_pct": float(LONG_TAKE_PROFIT_PCT),
        "short_take_profit_pct": float(SHORT_TAKE_PROFIT_PCT),
        "trend_resume_long_stop_loss_pct": float(TREND_RESUME_LONG_STOP_LOSS_PCT),
        "trend_resume_long_partial_target_pct": float(TREND_RESUME_LONG_PARTIAL_TARGET_PCT),
        "trend_resume_long_trailing_trigger_pct": float(TREND_RESUME_LONG_TRAILING_TRIGGER_PCT),
        "trend_resume_long_trailing_stop_pct": float(TREND_RESUME_LONG_TRAILING_STOP_PCT),
        "trend_resume_long_use_fixed_stop": bool(TREND_RESUME_LONG_USE_FIXED_STOP),
        "trend_resume_long_min_context_gap_pct": float(TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT),
        "trend_resume_long_max_context_gap_pct": float(TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT),
        "trend_resume_long_min_adx": float(TREND_RESUME_LONG_MIN_ADX),
        "trend_resume_long_min_trend_strength_pct": float(TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT),
        "trend_resume_long_max_rsi": float(TREND_RESUME_LONG_MAX_RSI),
        "trend_resume_long_require_close_above_prev_close": bool(
            TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE
        ),
        "trend_resume_short_stop_loss_pct": float(TREND_RESUME_SHORT_STOP_LOSS_PCT),
        "trend_resume_short_partial_target_pct": float(TREND_RESUME_SHORT_PARTIAL_TARGET_PCT),
        "trend_resume_short_trailing_trigger_pct": float(TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT),
        "trend_resume_short_trailing_stop_pct": float(TREND_RESUME_SHORT_TRAILING_STOP_PCT),
        "trend_resume_short_use_fixed_stop": bool(TREND_RESUME_SHORT_USE_FIXED_STOP),
        "trend_resume_short_min_context_gap_pct": float(TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT),
        "trend_resume_short_min_adx": float(TREND_RESUME_SHORT_MIN_ADX),
        "trend_resume_short_require_breakdown_confirmation": bool(
            TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION
        ),
        "trend_resume_short_blocked_entry_hours_utc": list(TREND_RESUME_SHORT_BLOCKED_ENTRY_HOURS_UTC),
        "trend_resume_short_require_close_confirmation_for_protection": bool(
            TREND_RESUME_SHORT_REQUIRE_CLOSE_CONFIRMATION_FOR_PROTECTION
        ),
        "pullback_long_stop_loss_pct": float(PULLBACK_LONG_STOP_LOSS_PCT),
        "pullback_long_partial_target_pct": float(PULLBACK_LONG_PARTIAL_TARGET_PCT),
        "pullback_long_trailing_trigger_pct": float(PULLBACK_LONG_TRAILING_TRIGGER_PCT),
        "pullback_long_trailing_stop_pct": float(PULLBACK_LONG_TRAILING_STOP_PCT),
        "pullback_long_use_fixed_stop": bool(PULLBACK_LONG_USE_FIXED_STOP),
        "pullback_short_stop_loss_pct": float(PULLBACK_SHORT_STOP_LOSS_PCT),
        "pullback_short_partial_target_pct": float(PULLBACK_SHORT_PARTIAL_TARGET_PCT),
        "pullback_short_trailing_trigger_pct": float(PULLBACK_SHORT_TRAILING_TRIGGER_PCT),
        "pullback_short_trailing_stop_pct": float(PULLBACK_SHORT_TRAILING_STOP_PCT),
        "pullback_short_use_fixed_stop": bool(PULLBACK_SHORT_USE_FIXED_STOP),
        "relief_rally_short_stop_loss_pct": float(RELIEF_RALLY_SHORT_STOP_LOSS_PCT),
        "relief_rally_short_partial_target_pct": float(RELIEF_RALLY_SHORT_PARTIAL_TARGET_PCT),
        "relief_rally_short_trailing_trigger_pct": float(RELIEF_RALLY_SHORT_TRAILING_TRIGGER_PCT),
        "relief_rally_short_trailing_stop_pct": float(RELIEF_RALLY_SHORT_TRAILING_STOP_PCT),
        "relief_rally_short_use_fixed_stop": bool(RELIEF_RALLY_SHORT_USE_FIXED_STOP),
        "relief_rally_short_min_context_gap_pct": float(RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT),
        "relief_rally_short_min_adx": float(RELIEF_RALLY_SHORT_MIN_ADX),
        "short_breakdown_buffer_pct": float(SHORT_BREAKDOWN_BUFFER_PCT),
        "short_pullback_min_context_gap_pct": float(SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT),
        "short_pullback_min_adx": float(SHORT_PULLBACK_MIN_ADX),
        "short_pullback_min_trend_strength_pct": float(SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT),
        "experimental_long_side_logic": bool(EXPERIMENTAL_LONG_SIDE_LOGIC),
        "experimental_short_side_logic": bool(EXPERIMENTAL_SHORT_SIDE_LOGIC),
        "short_require_strict_regime": bool(SHORT_REQUIRE_STRICT_REGIME),
        "allow_weak_bull_atr_long_entries": bool(ALLOW_WEAK_BULL_ATR_LONG_ENTRIES),
        "use_entry_hour_blocks": bool(USE_ENTRY_HOUR_BLOCKS),
        "blocked_long_entry_hours_utc": list(BLOCKED_LONG_ENTRY_HOURS_UTC),
        "blocked_short_entry_hours_utc": list(BLOCKED_SHORT_ENTRY_HOURS_UTC),
        "short_rsi_min_relief_rally": float(SHORT_RSI_MIN_RELIEF_RALLY),
        "short_rsi_max_relief_rally": float(SHORT_RSI_MAX_RELIEF_RALLY),
        "trailing_trigger_pct": float(TRAILING_TRIGGER_PCT),
        "partial_target_pct": float(PARTIAL_TARGET_PCT),
        "enforce_min_risk_reward_ratio": bool(ENFORCE_MIN_RISK_REWARD_RATIO),
        "min_risk_reward_ratio": float(MIN_RISK_REWARD_RATIO),
        "execution_profile": str(EXECUTION_PROFILE),
        "fee_pct": float(FEE_PCT),
        "testnet": bool(TESTNET),
          "live_execution_enabled": bool(ProductionConfig.ENABLE_LIVE_EXECUTION),
          "ai_intrabar_position_monitor": bool(ProductionConfig.AI_INTRABAR_POSITION_MONITOR),
          "ai_position_monitor_include_current_candle": bool(
              ProductionConfig.AI_POSITION_MONITOR_INCLUDE_CURRENT_CANDLE
          ),
          "ai_assist_mode": str(ProductionConfig.AI_ASSIST_MODE),
          "ai_market_reading_min_confidence": float(ProductionConfig.AI_MARKET_READING_MIN_CONFIDENCE),
          "ai_market_reading_approval_threshold": float(ProductionConfig.AI_MARKET_READING_APPROVAL_THRESHOLD),
          "ai_market_reading_directional_min_prob": float(ProductionConfig.AI_MARKET_READING_DIRECTIONAL_MIN_PROB),
          "ai_market_reading_directional_edge": float(ProductionConfig.AI_MARKET_READING_DIRECTIONAL_EDGE),
          "ai_market_reading_hold_edge": float(ProductionConfig.AI_MARKET_READING_HOLD_EDGE),
          "ai_market_reading_min_action_margin": float(ProductionConfig.AI_MARKET_READING_MIN_ACTION_MARGIN),
          "ai_market_reading_min_trend_score": float(ProductionConfig.AI_MARKET_READING_MIN_TREND_SCORE),
          "ai_market_reading_max_range_score": float(ProductionConfig.AI_MARKET_READING_MAX_RANGE_SCORE),
          "ai_market_reading_min_adx": float(ProductionConfig.AI_MARKET_READING_MIN_ADX),
          "ai_market_reading_near_level_pct": float(ProductionConfig.AI_MARKET_READING_NEAR_LEVEL_PCT),
          "ai_market_reading_learning_guard_min_trades": int(
              ProductionConfig.AI_MARKET_READING_LEARNING_GUARD_MIN_TRADES
          ),
          "ai_market_reading_learning_guard_min_win_rate_pct": float(
              ProductionConfig.AI_MARKET_READING_LEARNING_GUARD_MIN_WIN_RATE_PCT
          ),
          "ai_market_reading_learning_guard_max_avg_net_pct": float(
              ProductionConfig.AI_MARKET_READING_LEARNING_GUARD_MAX_AVG_NET_PCT
          ),
          "ai_structure_exit_min_profit_pct": float(ProductionConfig.AI_STRUCTURE_EXIT_MIN_PROFIT_PCT),
          "ai_structure_exit_near_level_pct": float(ProductionConfig.AI_STRUCTURE_EXIT_NEAR_LEVEL_PCT),
          "ai_structure_range_threshold": float(ProductionConfig.AI_STRUCTURE_RANGE_THRESHOLD),
          "ai_structure_trend_weak_threshold": float(ProductionConfig.AI_STRUCTURE_TREND_WEAK_THRESHOLD),
            "ai_structure_exit_require_protection": bool(ProductionConfig.AI_STRUCTURE_EXIT_REQUIRE_PROTECTION),
            "ai_structure_exit_strong_confidence_bonus": float(ProductionConfig.AI_STRUCTURE_EXIT_STRONG_CONFIDENCE_BONUS),
            "ai_setup_guard_enabled": bool(ProductionConfig.AI_SETUP_GUARD_ENABLED),
            "ai_setup_guard_min_trades": int(ProductionConfig.AI_SETUP_GUARD_MIN_TRADES),
            "ai_setup_guard_lookback": int(ProductionConfig.AI_SETUP_GUARD_LOOKBACK),
            "ai_setup_guard_max_consecutive_losses": int(ProductionConfig.AI_SETUP_GUARD_MAX_CONSECUTIVE_LOSSES),
            "ai_setup_guard_cooldown_signals": int(ProductionConfig.AI_SETUP_GUARD_COOLDOWN_SIGNALS),
            "ai_setup_guard_min_recent_pf": float(ProductionConfig.AI_SETUP_GUARD_MIN_RECENT_PF),
            "ai_setup_guard_max_recent_avg_net_pct": float(ProductionConfig.AI_SETUP_GUARD_MAX_RECENT_AVG_NET_PCT),
            "ai_entry_structure_guard_enabled": bool(ProductionConfig.AI_ENTRY_STRUCTURE_GUARD_ENABLED),
          "ai_entry_pullback_long_min_adx": float(ProductionConfig.AI_ENTRY_PULLBACK_LONG_MIN_ADX),
          "ai_entry_pullback_long_min_trend_score": float(ProductionConfig.AI_ENTRY_PULLBACK_LONG_MIN_TREND_SCORE),
          "ai_entry_pullback_long_max_range_score": float(ProductionConfig.AI_ENTRY_PULLBACK_LONG_MAX_RANGE_SCORE),
          "ai_entry_trend_resume_short_support_near_pct": float(
              ProductionConfig.AI_ENTRY_TREND_RESUME_SHORT_SUPPORT_NEAR_PCT
          ),
          "ai_entry_trend_resume_short_max_channel_position": float(
              ProductionConfig.AI_ENTRY_TREND_RESUME_SHORT_MAX_CHANNEL_POSITION
          ),
          "ai_entry_trend_resume_short_min_range_score": float(
              ProductionConfig.AI_ENTRY_TREND_RESUME_SHORT_MIN_RANGE_SCORE
          ),
          "db_backend": "postgres" if str(DATABASE_URL or "").strip().lower().startswith(("postgres://", "postgresql://")) else "sqlite",
        "history_data_dir": str(HISTORY_DATA_DIR),
        "bot_require_local_csv_bootstrap": bool(BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP),
        "bot_allow_rest_fallback": bool(BOT_ALLOW_REST_FALLBACK),
        "bot_bootstrap_candles": int(BOT_BOOTSTRAP_CANDLES),
        "bot_websocket_timeout_sec": float(BOT_WEBSOCKET_TIMEOUT_SEC),
    }


def normalize_symbol(symbol: str) -> str:
    token = str(symbol or "").strip().upper()
    if not token:
        return ""
    return token.replace(":USDT", "/USDT") if ":USDT" in token else token


def _resolve_timeframe_minutes(timeframe: str) -> int:
    token = str(timeframe or "").strip().lower()
    if not token:
        return 15
    if token.endswith("m"):
        return max(int(float(token[:-1] or 15)), 1)
    if token.endswith("h"):
        return max(int(float(token[:-1] or 1) * 60), 1)
    if token.endswith("d"):
        return max(int(float(token[:-1] or 1) * 1440), 1)
    return 15


def get_backtest_governance_profile(
    *,
    symbol: str,
    timeframe: str,
    period_days: int,
) -> Dict[str, float]:
    base_min_trades_90d = float(getattr(ProductionConfig, "MIN_BACKTEST_TRADES_FOR_PROMOTION", 50) or 50)
    base_max_drawdown = float(getattr(ProductionConfig, "MAX_PROMOTION_DRAWDOWN", 25.0) or 25.0)
    timeframe_minutes = _resolve_timeframe_minutes(timeframe)
    scale = max(float(timeframe_minutes) / 15.0, 1.0)
    min_trades = (base_min_trades_90d / scale) * (max(int(period_days), 1) / 90.0)

    symbol_family = str(get_symbol_family_key(symbol) or "global")
    max_drawdown = base_max_drawdown
    if symbol_family == "alt_trend_strict" and timeframe_minutes >= 60:
        max_drawdown = max(base_max_drawdown, 35.0)

    return {
        "min_trades": float(min_trades),
        "max_drawdown_pct": float(max_drawdown),
        "min_profit_factor": float(getattr(ProductionConfig, "MIN_PROMOTION_PROFIT_FACTOR", 1.10) or 1.10),
        "min_expectancy_pct": float(getattr(ProductionConfig, "MIN_PROMOTION_EXPECTANCY_PCT", 0.01) or 0.01),
        "timeframe_minutes": float(timeframe_minutes),
        "trade_scale": float(scale),
    }


def get_symbol_family_key(symbol: str) -> str:
    token = normalize_symbol(symbol)
    if token.startswith("BTC/"):
        return "btc_core"
    if token.startswith("ETH/"):
        return "eth_overlay"
    if token.startswith(("SOL/", "BNB/", "XRP/", "ADA/", "DOGE/", "LINK/", "XLM/")):
        return "alt_trend_strict"
    return "global"


SYMBOL_STRATEGY_OVERRIDE_KEYS = [
    "GLOBAL_MIN_ATR_PCT",
    "SHORT_MIN_ATR_PCT",
    "MIN_TREND_STRENGTH_PCT",
    "MIN_TREND_STRENGTH_PCT_SHORT",
    "LONG_ADX_THRESHOLD",
    "SHORT_ADX_THRESHOLD",
    "VOLUME_MA_PERIOD",
    "ENABLE_VOLUME_MA_ENTRY_FILTER",
    "VOLUME_MA_ENTRY_MULTIPLIER",
    "LONG_VOLUME_RATIO_REQUIRED",
    "SHORT_VOLUME_RATIO_REQUIRED",
    "ENABLE_MACD_ENTRY_FILTER",
    "MACD_ENTRY_FILTER_MODE",
    "MACD_FAST_PERIOD",
    "MACD_SLOW_PERIOD",
    "MACD_SIGNAL_PERIOD",
    "MIN_LONG_SCORE",
    "MIN_SHORT_SCORE",
    "BUY_RSI_SIGNAL",
    "SELL_RSI_SIGNAL",
    "LONG_FAST_SLOW_GAP_PCT",
    "SHORT_FAST_SLOW_GAP_PCT",
    "ENABLE_LONG_PULLBACK",
    "ENABLE_LONG_RESUME",
    "ENABLE_SHORT_PULLBACK",
    "ENABLE_SHORT_RELIEF_RALLY",
    "ENABLE_SHORT_RESUME",
    "ALLOW_TRIGGERLESS_ENTRIES",
    "BYPASS_WEAK_REGIME_GATE",
    "ALLOW_WEAK_BULL_ATR_LONG_ENTRIES",
    "DISABLE_SHORT_SCORE_GATE",
    "EXPERIMENTAL_LONG_SIDE_LOGIC",
    "EXPERIMENTAL_SHORT_SIDE_LOGIC",
    "LONG_RESUME_HOT_CONTEXT_GAP_PCT",
    "LONG_PULLBACK_AS_RESUME_WHEN_DISABLED",
    "PULLBACK_LONG_MIN_ADX",
    "PULLBACK_LONG_MAX_CONTEXT_GAP_PCT",
    "PULLBACK_LONG_MIN_RSI",
    "PULLBACK_LONG_MAX_RSI",
    "SHORT_TREND_GAP_PCT",
    "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT",
    "SHORT_PULLBACK_MIN_ADX",
    "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT",
    "TREND_RESUME_LONG_MAX_CONTEXT_GAP_PCT",
    "TREND_RESUME_LONG_MIN_ADX",
    "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT",
    "TREND_RESUME_LONG_MAX_RSI",
    "TREND_RESUME_LONG_REQUIRE_CLOSE_ABOVE_PREV_CLOSE",
    "TREND_RESUME_LONG_STOP_LOSS_PCT",
    "TREND_RESUME_LONG_PARTIAL_TARGET_PCT",
    "TREND_RESUME_LONG_TRAILING_TRIGGER_PCT",
    "TREND_RESUME_LONG_TRAILING_STOP_PCT",
    "TREND_RESUME_LONG_USE_FIXED_STOP",
    "TREND_RESUME_SHORT_STOP_LOSS_PCT",
    "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT",
    "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT",
    "TREND_RESUME_SHORT_TRAILING_STOP_PCT",
    "TREND_RESUME_SHORT_USE_FIXED_STOP",
    "TREND_RESUME_SHORT_MIN_CONTEXT_GAP_PCT",
    "TREND_RESUME_SHORT_MIN_ADX",
    "TREND_RESUME_SHORT_REQUIRE_BREAKDOWN_CONFIRMATION",
    "TREND_RESUME_SHORT_BLOCKED_ENTRY_HOURS_UTC",
]


def load_symbol_approvals(path: Optional[str] = None) -> Dict[str, object]:
    resolved_path = str(path or SYMBOL_APPROVALS_PATH or "").strip()
    if not resolved_path or not os.path.exists(resolved_path):
        return {}
    try:
        with open(resolved_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    symbols = payload.get("symbols")
    return symbols if isinstance(symbols, dict) else {}


def load_symbol_strategy_overrides(path: Optional[str] = None) -> Dict[str, object]:
    resolved_path = str(path or SYMBOL_STRATEGY_OVERRIDES_PATH or "").strip()
    if not resolved_path or not os.path.exists(resolved_path):
        return {}
    try:
        with open(resolved_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    symbols = payload.get("symbols")
    return symbols if isinstance(symbols, dict) else {}


def get_symbol_strategy_override_record(symbol: str, overrides_path: Optional[str] = None) -> Dict[str, object]:
    overrides = load_symbol_strategy_overrides(path=overrides_path)
    record = overrides.get(normalize_symbol(symbol), {})
    return dict(record) if isinstance(record, dict) else {}


def load_saved_strategy_profiles(path: Optional[str] = None) -> Dict[str, object]:
    resolved_path = str(path or SAVED_STRATEGY_PROFILES_PATH or "").strip()
    if not resolved_path or not os.path.exists(resolved_path):
        return {}
    try:
        with open(resolved_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    profiles = payload.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def get_saved_strategy_profile(profile_name: str, path: Optional[str] = None) -> Dict[str, object]:
    profiles = load_saved_strategy_profiles(path=path)
    record = profiles.get(str(profile_name or "").strip(), {})
    return dict(record) if isinstance(record, dict) else {}


def save_runtime_strategy_profile(
    profile_name: str,
    *,
    snapshot: Optional[Dict[str, object]] = None,
    metadata: Optional[Dict[str, object]] = None,
    path: Optional[str] = None,
) -> Dict[str, object]:
    from datetime import datetime
    from datetime import timezone

    resolved_name = str(profile_name or "").strip()
    if not resolved_name:
        raise ValueError("profile_name obrigatorio para salvar um motor.")

    resolved_path = str(path or SAVED_STRATEGY_PROFILES_PATH or "").strip()
    if not resolved_path:
        raise ValueError("Caminho de perfis salvos nao configurado.")

    profiles = load_saved_strategy_profiles(path=resolved_path)
    entry = {
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot": dict(snapshot or build_runtime_strategy_snapshot()),
        "metadata": dict(metadata or {}),
    }
    profiles[resolved_name] = entry

    parent = os.path.dirname(resolved_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(resolved_path, "w", encoding="utf-8") as handle:
        json.dump({"profiles": profiles}, handle, ensure_ascii=False, indent=2, sort_keys=True)

    return {
        "name": resolved_name,
        "path": resolved_path,
        "entry": entry,
    }


def get_symbol_strategy_overrides(symbol: str, overrides_path: Optional[str] = None) -> Dict[str, object]:
    record = get_symbol_strategy_override_record(symbol, overrides_path=overrides_path)
    params = record.get("overrides")
    if isinstance(params, dict):
        return {str(key): value for key, value in params.items()}
    return {str(key): value for key, value in record.items() if key in SYMBOL_STRATEGY_OVERRIDE_KEYS}


def _coerce_strategy_override_value(key: str, value):
    current = globals().get(key)
    if isinstance(current, bool):
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "sim"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        if isinstance(value, list):
            return value
        return list(current)
    return value


def apply_symbol_strategy_overrides(symbol: str, overrides_path: Optional[str] = None) -> Dict[str, object]:
    symbol_token = normalize_symbol(symbol)
    globals()["SYMBOL"] = symbol_token
    record = get_symbol_strategy_override_record(symbol_token, overrides_path=overrides_path)
    raw_overrides = get_symbol_strategy_overrides(symbol_token, overrides_path=overrides_path)
    applied: Dict[str, object] = {}
    ignored: Dict[str, object] = {}
    resolved_timeframe = str(record.get("recommended_timeframe") or record.get("timeframe") or "").strip().lower()
    if resolved_timeframe:
        globals()["TIMEFRAME"] = resolved_timeframe
        applied["TIMEFRAME"] = resolved_timeframe
    for key, value in raw_overrides.items():
        if key not in SYMBOL_STRATEGY_OVERRIDE_KEYS or key not in globals():
            ignored[key] = value
            continue
        try:
            coerced = _coerce_strategy_override_value(key, value)
        except (TypeError, ValueError):
            ignored[key] = value
            continue
        globals()[key] = coerced
        applied[key] = coerced
    return {
        "symbol": symbol_token,
        "applied": applied,
        "ignored": ignored,
        "source": str(overrides_path or SYMBOL_STRATEGY_OVERRIDES_PATH),
    }


def get_symbol_validation_record(symbol: str, approvals_path: Optional[str] = None) -> Dict[str, object]:
    approvals = load_symbol_approvals(path=approvals_path)
    record = approvals.get(normalize_symbol(symbol), {})
    return dict(record) if isinstance(record, dict) else {}


def is_symbol_runtime_approved(symbol: str, approvals_path: Optional[str] = None) -> bool:
    if bool(RUNTIME_SYMBOL_APPROVAL_OVERRIDE):
        return True
    record = get_symbol_validation_record(symbol, approvals_path=approvals_path)
    status = str(record.get("status") or "").strip().lower()
    return status == "approved"


class AppConfig:
    DB_PATH = os.getenv("DB_PATH", "data/trading_bot.db")
    DATABASE_URL = str(os.getenv("DATABASE_URL", "")).strip()
    DB_BACKEND = "postgres" if DATABASE_URL.lower().startswith(("postgres://", "postgresql://")) else "sqlite"
    DB_DISPLAY = "postgres (DATABASE_URL)" if DB_BACKEND == "postgres" else DB_PATH

    DEFAULT_SYMBOL = os.getenv("APP_DEFAULT_SYMBOL", SYMBOL)
    DEFAULT_TIMEFRAME = os.getenv("APP_DEFAULT_TIMEFRAME", TIMEFRAME)
    DEFAULT_RSI_PERIOD = _get_int("APP_DEFAULT_RSI_PERIOD", RSI_PERIOD)
    DEFAULT_RSI_MIN = _get_int("APP_DEFAULT_RSI_MIN", int(BUY_RSI_SIGNAL))
    DEFAULT_RSI_MAX = _get_int("APP_DEFAULT_RSI_MAX", int(SELL_RSI_SIGNAL))

    PRIMARY_CONTEXT_TIMEFRAME = os.getenv("APP_PRIMARY_CONTEXT_TIMEFRAME", "1h")
    DEFAULT_BACKTEST_WINDOW_DAYS = _get_int("APP_DEFAULT_BACKTEST_WINDOW_DAYS", 90)
    DEFAULT_BACKTEST_PRESET = os.getenv("APP_DEFAULT_BACKTEST_PRESET", "Leitura Ativa (15m)")
    DEFAULT_BACKTEST_PRESET_SUMMARY = (
        "Preset global validado no terminal para leitura EMA/RSI com risco balanceado."
    )

    BRAZIL_SUPPORTED_EXCHANGES = _get_csv_list(
        "BRAZIL_SUPPORTED_EXCHANGES",
        ["binanceusdm", "bybit"],
    )

    SINGLE_SETUP_MODE = _get_bool("SINGLE_SETUP_MODE", False)
    ENABLE_PARAMETER_OPTIMIZATION = _get_bool("ENABLE_PARAMETER_OPTIMIZATION", True)
    ENABLE_MARKET_SCAN = _get_bool("ENABLE_MARKET_SCAN", True)
    MAX_CANDLES = _get_int("MAX_CANDLES", 1200)

    _SUPPORTED_PAIRS = _get_csv_list(
        "SUPPORTED_PAIRS",
        ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT", "XLM/USDT"],
    )
    _SUPPORTED_TIMEFRAMES = _get_csv_list(
        "SUPPORTED_TIMEFRAMES",
        ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
    )

    _CRYPTO_TIMEFRAME_SETTINGS = {
        "1m": {"rsi_oversold": 50, "rsi_overbought": 50, "min_confidence": 72, "min_volume_ratio": 1.4},
        "5m": {"rsi_oversold": int(BUY_RSI_SIGNAL), "rsi_overbought": int(SELL_RSI_SIGNAL), "min_confidence": 70, "min_volume_ratio": 1.2},
        "15m": {"rsi_oversold": int(BUY_RSI_SIGNAL), "rsi_overbought": int(SELL_RSI_SIGNAL), "min_confidence": 68, "min_volume_ratio": 1.15},
        "30m": {"rsi_oversold": 53, "rsi_overbought": 47, "min_confidence": 66, "min_volume_ratio": 1.1},
        "1h": {"rsi_oversold": 52, "rsi_overbought": 48, "min_confidence": 64, "min_volume_ratio": 1.05},
        "4h": {"rsi_oversold": 51, "rsi_overbought": 49, "min_confidence": 62, "min_volume_ratio": 1.0},
        "1d": {"rsi_oversold": 50, "rsi_overbought": 50, "min_confidence": 60, "min_volume_ratio": 1.0},
    }

    _DAY_TRADING_SETTINGS = {
        "1m": {"rsi_oversold": 50, "rsi_overbought": 50, "min_confidence": 74, "min_volume_ratio": 1.5},
        "5m": {"rsi_oversold": int(BUY_RSI_SIGNAL), "rsi_overbought": int(SELL_RSI_SIGNAL), "min_confidence": 72, "min_volume_ratio": 1.3},
        "15m": {"rsi_oversold": int(BUY_RSI_SIGNAL), "rsi_overbought": int(SELL_RSI_SIGNAL), "min_confidence": 70, "min_volume_ratio": 1.2},
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
            "bt_rsi_period": RSI_PERIOD,
            "bt_rsi_min": int(BUY_RSI_SIGNAL),
            "bt_rsi_max": int(SELL_RSI_SIGNAL),
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
            "bt_rsi_period": RSI_PERIOD,
            "bt_rsi_min": int(BUY_RSI_SIGNAL),
            "bt_rsi_max": int(SELL_RSI_SIGNAL),
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
            "bt_rsi_period": RSI_PERIOD,
            "bt_rsi_min": int(BUY_RSI_SIGNAL),
            "bt_rsi_max": int(SELL_RSI_SIGNAL),
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
            "bt_rsi_period": RSI_PERIOD,
            "bt_rsi_min": int(BUY_RSI_SIGNAL),
            "bt_rsi_max": int(SELL_RSI_SIGNAL),
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
        defaults = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT"]
        symbols = [sym for sym in defaults if sym in supported]
        return symbols or supported[:8]

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
    def normalize_exchange_name(exchange_name: str = "binanceusdm") -> str:
        normalized_name = str(exchange_name or "binanceusdm").strip().lower()
        aliases = {
            "binance": "binanceusdm",
            "binance-futures": "binanceusdm",
            "futures": "binanceusdm",
            "bybitusdm": "bybit",
            "bybit-usdt": "bybit",
            "bybit-futures": "bybit",
        }
        return aliases.get(normalized_name, normalized_name or "binanceusdm")

    @staticmethod
    def get_exchange_label(exchange_name: str = "binanceusdm") -> str:
        normalized_name = ExchangeConfig.normalize_exchange_name(exchange_name)
        labels = {
            "binanceusdm": "Binance Futures",
            "bybit": "Bybit USDT Perp",
        }
        return labels.get(normalized_name, normalized_name.upper())

    @staticmethod
    def get_exchange_instance_with_credentials(
        exchange_name: str = "binance",
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ):
        try:
            import ccxt  # type: ignore
        except Exception:
            return _NullExchange()

        normalized_name = ExchangeConfig.normalize_exchange_name(exchange_name)
        api_key = str(api_key or "").strip()
        api_secret = str(api_secret or "").strip()

        if normalized_name == "bybit" and hasattr(ccxt, "bybit"):
            exchange = ccxt.bybit(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                    "options": {
                        "defaultType": "swap",
                        "adjustForTimeDifference": True,
                    },
                }
            )
        elif normalized_name == "binanceusdm" and hasattr(ccxt, "binanceusdm"):
            exchange = ccxt.binanceusdm(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                    "options": {
                        "defaultType": "future",
                        "adjustForTimeDifference": True,
                        "recvWindow": int(BINANCE_RECV_WINDOW_MS),
                    },
                }
            )
        else:
            exchange_class = getattr(ccxt, normalized_name, None) or getattr(ccxt, "binance")
            exchange = exchange_class(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                    "options": {
                        "defaultType": "future",
                        "adjustForTimeDifference": True,
                        "recvWindow": int(BINANCE_RECV_WINDOW_MS),
                    },
                }
            )

        if testnet:
            try:
                exchange.set_sandbox_mode(True)
            except Exception:
                pass
        if hasattr(exchange, "load_time_difference"):
            try:
                # Prime the exchange clock offset before the first signed request.
                # This helps reduce Binance -1021 recvWindow failures on hosts with clock drift.
                exchange.load_time_difference()
            except Exception:
                pass
        return exchange

    @staticmethod
    def get_exchange_instance(exchange_name: str = "binance", testnet: bool = True):
        normalized_name = ExchangeConfig.normalize_exchange_name(exchange_name)
        if normalized_name == "bybit":
            api_key_env = "BYBIT_TESTNET_API_KEY" if testnet else "BYBIT_API_KEY"
            api_secret_env = "BYBIT_TESTNET_SECRET_KEY" if testnet else "BYBIT_SECRET_KEY"
        else:
            api_key_env = "BINANCE_TESTNET_API_KEY" if testnet else "BINANCE_API_KEY"
            api_secret_env = "BINANCE_TESTNET_SECRET_KEY" if testnet else "BINANCE_SECRET_KEY"
        return ExchangeConfig.get_exchange_instance_with_credentials(
            exchange_name=normalized_name,
            api_key=os.getenv(api_key_env, ""),
            api_secret=os.getenv(api_secret_env, ""),
            testnet=testnet,
        )

    @staticmethod
    def test_connection(exchange_name: str = "binance", testnet: bool = True ):
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

    AI_MODEL_PATH = os.getenv("AI_MODEL_PATH", os.path.join("data", "models", "runtime_model.tflite"))
    AI_MODEL_METADATA_PATH = os.getenv(
        "AI_MODEL_METADATA_PATH",
        os.path.join("data", "models", "runtime_model_metadata.json"),
    )
    ENABLE_AI_ASSISTANT = _get_bool("ENABLE_AI_ASSISTANT", True)
    AI_ASSIST_MODE = os.getenv("AI_ASSIST_MODE", "hybrid")
    AI_MIN_WIN_PROBABILITY = _get_float("AI_MIN_WIN_PROBABILITY", 0.60)
    AI_COMPARE_BASELINE_DEFAULT = _get_bool("AI_COMPARE_BASELINE_DEFAULT", True)
    AI_MIN_SIGNAL_CONFIDENCE = _get_float("AI_MIN_SIGNAL_CONFIDENCE", 0.40)
    AI_EXIT_MIN_SIGNAL_CONFIDENCE = _get_float("AI_EXIT_MIN_SIGNAL_CONFIDENCE", 0.45)
    AI_MARKET_READING_MIN_CONFIDENCE = _get_float("AI_MARKET_READING_MIN_CONFIDENCE", 0.30)
    AI_MARKET_READING_APPROVAL_THRESHOLD = _get_float("AI_MARKET_READING_APPROVAL_THRESHOLD", 0.28)
    AI_MARKET_READING_DIRECTIONAL_MIN_PROB = _get_float("AI_MARKET_READING_DIRECTIONAL_MIN_PROB", 0.28)
    AI_MARKET_READING_DIRECTIONAL_EDGE = _get_float("AI_MARKET_READING_DIRECTIONAL_EDGE", 0.03)
    AI_MARKET_READING_HOLD_EDGE = _get_float("AI_MARKET_READING_HOLD_EDGE", 0.06)
    AI_MARKET_READING_MIN_ACTION_MARGIN = _get_float("AI_MARKET_READING_MIN_ACTION_MARGIN", 0.08)
    AI_MARKET_READING_MIN_TREND_SCORE = _get_float("AI_MARKET_READING_MIN_TREND_SCORE", 0.40)
    AI_MARKET_READING_MAX_RANGE_SCORE = _get_float("AI_MARKET_READING_MAX_RANGE_SCORE", 0.74)
    AI_MARKET_READING_MIN_ADX = _get_float("AI_MARKET_READING_MIN_ADX", 24.0)
    AI_MARKET_READING_NEAR_LEVEL_PCT = _get_float("AI_MARKET_READING_NEAR_LEVEL_PCT", 0.28)
    AI_MARKET_READING_LEARNING_GUARD_MIN_TRADES = _get_int("AI_MARKET_READING_LEARNING_GUARD_MIN_TRADES", 10)
    AI_MARKET_READING_LEARNING_GUARD_MIN_WIN_RATE_PCT = _get_float(
        "AI_MARKET_READING_LEARNING_GUARD_MIN_WIN_RATE_PCT",
        48.0,
    )
    AI_MARKET_READING_LEARNING_GUARD_MAX_AVG_NET_PCT = _get_float(
        "AI_MARKET_READING_LEARNING_GUARD_MAX_AVG_NET_PCT",
        -0.05,
    )
    AI_MARKET_READING_LEARNING_GUARD_CONFIDENCE_BONUS = _get_float(
        "AI_MARKET_READING_LEARNING_GUARD_CONFIDENCE_BONUS",
        0.04,
    )
    AI_MARKET_READING_LEARNING_GUARD_APPROVAL_BONUS = _get_float(
        "AI_MARKET_READING_LEARNING_GUARD_APPROVAL_BONUS",
        0.05,
    )
    AI_MARKET_READING_LEARNING_GUARD_MARGIN_BONUS = _get_float(
        "AI_MARKET_READING_LEARNING_GUARD_MARGIN_BONUS",
        0.05,
    )
    AI_MARKET_READING_LONG_MAX_CHANNEL_POSITION = _get_float(
        "AI_MARKET_READING_LONG_MAX_CHANNEL_POSITION",
        0.88,
    )
    AI_MARKET_READING_SHORT_MIN_CHANNEL_POSITION = _get_float(
        "AI_MARKET_READING_SHORT_MIN_CHANNEL_POSITION",
        0.12,
    )
    AI_MARKET_READING_PRESSURE_THRESHOLD = _get_float("AI_MARKET_READING_PRESSURE_THRESHOLD", 0.82)
    AI_ALLOW_EARLY_EXIT = _get_bool("AI_ALLOW_EARLY_EXIT", True)
    AI_INTRABAR_POSITION_MONITOR = _get_bool("AI_INTRABAR_POSITION_MONITOR", True)
    AI_POSITION_MONITOR_INCLUDE_CURRENT_CANDLE = _get_bool(
        "AI_POSITION_MONITOR_INCLUDE_CURRENT_CANDLE",
        True,
    )
    AI_STRUCTURE_EXIT_MIN_PROFIT_PCT = _get_float("AI_STRUCTURE_EXIT_MIN_PROFIT_PCT", 0.90)
    AI_STRUCTURE_EXIT_NEAR_LEVEL_PCT = _get_float("AI_STRUCTURE_EXIT_NEAR_LEVEL_PCT", 0.22)
    AI_STRUCTURE_RANGE_THRESHOLD = _get_float("AI_STRUCTURE_RANGE_THRESHOLD", 0.72)
    AI_STRUCTURE_TREND_WEAK_THRESHOLD = _get_float("AI_STRUCTURE_TREND_WEAK_THRESHOLD", 0.24)
    AI_STRUCTURE_EXIT_REQUIRE_PROTECTION = _get_bool("AI_STRUCTURE_EXIT_REQUIRE_PROTECTION", True)
    AI_STRUCTURE_EXIT_STRONG_CONFIDENCE_BONUS = _get_float("AI_STRUCTURE_EXIT_STRONG_CONFIDENCE_BONUS", 0.08)
    AI_MEMORY_PATH = os.getenv("AI_MEMORY_PATH", os.path.join("data", "models", "runtime_learning_memory.json"))
    AI_ONLINE_LEARNING_ENABLED = _get_bool("AI_ONLINE_LEARNING_ENABLED", True)
    AI_MEMORY_MIN_TRADES = _get_int("AI_MEMORY_MIN_TRADES", 6)
    AI_MEMORY_MAX_BIAS = _get_float("AI_MEMORY_MAX_BIAS", 0.12)
    AI_HYBRID_APPROVAL_THRESHOLD = _get_float("AI_HYBRID_APPROVAL_THRESHOLD", 0.24)
    AI_SETUP_GUARD_ENABLED = _get_bool("AI_SETUP_GUARD_ENABLED", False)
    AI_SETUP_GUARD_MIN_TRADES = _get_int("AI_SETUP_GUARD_MIN_TRADES", 8)
    AI_SETUP_GUARD_LOOKBACK = _get_int("AI_SETUP_GUARD_LOOKBACK", 10)
    AI_SETUP_GUARD_MAX_CONSECUTIVE_LOSSES = _get_int("AI_SETUP_GUARD_MAX_CONSECUTIVE_LOSSES", 3)
    AI_SETUP_GUARD_COOLDOWN_SIGNALS = _get_int("AI_SETUP_GUARD_COOLDOWN_SIGNALS", 3)
    AI_SETUP_GUARD_MIN_RECENT_PF = _get_float("AI_SETUP_GUARD_MIN_RECENT_PF", 0.95)
    AI_SETUP_GUARD_MAX_RECENT_AVG_NET_PCT = _get_float("AI_SETUP_GUARD_MAX_RECENT_AVG_NET_PCT", -0.05)
    AI_ENTRY_STRUCTURE_GUARD_ENABLED = _get_bool("AI_ENTRY_STRUCTURE_GUARD_ENABLED", False)
    AI_ENTRY_PULLBACK_LONG_MIN_ADX = _get_float("AI_ENTRY_PULLBACK_LONG_MIN_ADX", 39.0)
    AI_ENTRY_PULLBACK_LONG_MIN_TREND_SCORE = _get_float("AI_ENTRY_PULLBACK_LONG_MIN_TREND_SCORE", 0.42)
    AI_ENTRY_PULLBACK_LONG_MAX_RANGE_SCORE = _get_float("AI_ENTRY_PULLBACK_LONG_MAX_RANGE_SCORE", 0.60)
    AI_ENTRY_TREND_RESUME_SHORT_SUPPORT_NEAR_PCT = _get_float(
        "AI_ENTRY_TREND_RESUME_SHORT_SUPPORT_NEAR_PCT",
        0.45,
    )
    AI_ENTRY_TREND_RESUME_SHORT_MAX_CHANNEL_POSITION = _get_float(
        "AI_ENTRY_TREND_RESUME_SHORT_MAX_CHANNEL_POSITION",
        0.20,
    )
    AI_ENTRY_TREND_RESUME_SHORT_MIN_RANGE_SCORE = _get_float(
        "AI_ENTRY_TREND_RESUME_SHORT_MIN_RANGE_SCORE",
        0.55,
    )
    AI_WEB_CONTEXT_ENABLED = _get_bool("AI_WEB_CONTEXT_ENABLED", True)
    AI_WEB_CONTEXT_CACHE_TTL_SEC = _get_int("AI_WEB_CONTEXT_CACHE_TTL_SEC", 900)
    AI_WEB_CONTEXT_TIMEOUT_SEC = _get_float("AI_WEB_CONTEXT_TIMEOUT_SEC", 8.0)
    AI_FEAR_GREED_ENABLED = _get_bool("AI_FEAR_GREED_ENABLED", True)
    AI_FEAR_GREED_API_URL = os.getenv("AI_FEAR_GREED_API_URL", "https://api.alternative.me/fng/?limit=1&format=json")
    AI_NEWS_ENABLED = _get_bool("AI_NEWS_ENABLED", True)
    AI_NEWS_LOOKBACK_HOURS = _get_int("AI_NEWS_LOOKBACK_HOURS", 18)
    AI_NEWS_MAX_ITEMS = _get_int("AI_NEWS_MAX_ITEMS", 12)
    AI_NEWS_FEED_URLS = _get_csv_list(
        "AI_NEWS_FEED_URLS",
        ["https://www.coindesk.com/arc/outboundfeeds/rss/"],
    )
    BACKTEST_FAST_MODE_DEFAULT = _get_bool("BACKTEST_FAST_MODE_DEFAULT", True)

    ENABLE_DASHBOARD_BACKGROUND_BOT = _get_bool("ENABLE_DASHBOARD_BACKGROUND_BOT", False)
    DASHBOARD_USER_SESSION_TIMEOUT_HOURS = _get_int("DASHBOARD_USER_SESSION_TIMEOUT_HOURS", 24)
    DASHBOARD_MIN_PASSWORD_LENGTH = _get_int("DASHBOARD_MIN_PASSWORD_LENGTH", 10)
    ALLOW_SELF_SERVICE_SIGNUP = _get_bool("ALLOW_SELF_SERVICE_SIGNUP", False)
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
    POSITION_SIZING_MODE = os.getenv("POSITION_SIZING_MODE", POSITION_SIZING_MODE).strip().lower() or "hybrid"
    POSITION_MARGIN_ALLOCATION_PCT = _get_float(
        "POSITION_MARGIN_ALLOCATION_PCT",
        POSITION_MARGIN_ALLOCATION_PCT,
    )
    ENFORCE_MIN_RISK_REWARD_RATIO = _get_bool(
        "ENFORCE_MIN_RISK_REWARD_RATIO",
        ENFORCE_MIN_RISK_REWARD_RATIO,
    )
    MIN_RISK_REWARD_RATIO = _get_float("MIN_RISK_REWARD_RATIO", MIN_RISK_REWARD_RATIO)
    MAX_OPEN_PAPER_TRADES = _get_int("MAX_OPEN_PAPER_TRADES", 1)
    MAX_OPEN_PAPER_TRADES_PER_SYMBOL = _get_int("MAX_OPEN_PAPER_TRADES_PER_SYMBOL", 1)
    MAX_PORTFOLIO_OPEN_RISK_PCT = _get_float("MAX_PORTFOLIO_OPEN_RISK_PCT", 5.0)

    ENABLE_LIVE_EXECUTION = _get_bool("ENABLE_LIVE_EXECUTION", False)
    LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "")
    MAX_REAL_RISK_PER_TRADE_PCT_START = _get_float("MAX_REAL_RISK_PER_TRADE_PCT_START", 2.0)
    MAX_DAILY_REAL_LOSS_PCT = _get_float("MAX_DAILY_REAL_LOSS_PCT", 2.0)
    MAX_CONSECUTIVE_REAL_LOSSES = _get_int("MAX_CONSECUTIVE_REAL_LOSSES", 3)
    MAX_OPEN_REAL_TRADES = _get_int("MAX_OPEN_REAL_TRADES", 1)
    REQUIRE_APPROVED_GOVERNANCE_FOR_LIVE = _get_bool("REQUIRE_APPROVED_GOVERNANCE_FOR_LIVE", True)
    MIN_LIVE_QUALITY_SCORE = _get_float("MIN_LIVE_QUALITY_SCORE", 65.0)

    REQUIRE_ACTIVE_PROFILE_FOR_RUNTIME = _get_bool("REQUIRE_ACTIVE_PROFILE_FOR_RUNTIME", False)
    ENABLE_MULTIUSER_RUNTIME = _get_bool("ENABLE_MULTIUSER_RUNTIME", False)
    ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION = _get_bool("ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION", False)
    REQUIRE_MULTIUSER_VALID_TOKEN = _get_bool("REQUIRE_MULTIUSER_VALID_TOKEN", True)
    REQUIRE_MULTIUSER_VALID_PERMISSIONS = _get_bool("REQUIRE_MULTIUSER_VALID_PERMISSIONS", True)
    REQUIRE_MULTIUSER_RECONCILIATION_OK = _get_bool("REQUIRE_MULTIUSER_RECONCILIATION_OK", True)
    REQUIRE_DASHBOARD_DEVICE_LICENSE = _get_bool("REQUIRE_DASHBOARD_DEVICE_LICENSE", True)
    DASHBOARD_LICENSE_AUTO_BIND_FIRST_ACCESS = _get_bool("DASHBOARD_LICENSE_AUTO_BIND_FIRST_ACCESS", True)
    DASHBOARD_LICENSE_BIND_IP = _get_bool("DASHBOARD_LICENSE_BIND_IP", True)
    DASHBOARD_LICENSE_BIND_DEVICE = _get_bool("DASHBOARD_LICENSE_BIND_DEVICE", True)

    MIN_BACKTEST_TRADES_FOR_PROMOTION = _get_int("MIN_BACKTEST_TRADES_FOR_PROMOTION", 50)
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
    "BOT_WAIT_NEXT_CLOSED_CANDLE_ON_REAL_STARTUP",
    "BOT_REENTRY_COOLDOWN_CANDLES",
    "BINANCE_RECV_WINDOW_MS",
    "BOT_TRAILING_ONLY_WHEN_POSITION_ALIGNED",
    "SYMBOL_APPROVALS_PATH",
    "SYMBOL_STRATEGY_OVERRIDES_PATH",
    "RUNTIME_REQUIRE_APPROVED_SYMBOL",
    "RUNTIME_SYMBOL_APPROVAL_OVERRIDE",
    "RUNTIME_ALLOW_WATCHLIST_IN_TESTNET",
    "ALT_STRICT_CONTEXT_FILTER",
    "ALT_MIN_CONTEXT_GAP_PCT",
    "ALT_MIN_GLOBAL_ATR_PCT",
    "POLL_SECONDS",
    "LEVERAGE",
    "POSITION_SIZING_MODE",
    "POSITION_MARGIN_ALLOCATION_PCT",
    "ENFORCE_MIN_RISK_REWARD_RATIO",
    "MIN_RISK_REWARD_RATIO",
    "SINGLE_USER_RUNTIME_USER_ID",
    "SINGLE_USER_RUNTIME_ACCOUNT_ID",
    "SINGLE_USER_RUNTIME_ACCOUNT_ALIAS",
    "SINGLE_USER_RUNTIME_EXCHANGE",
    "RISK_PER_TRADE_PCT",
    "MAX_OPEN_TRADES",
    "FAST_EMA",
    "SLOW_EMA",
    "TREND_EMA",
    "RSI_PERIOD",
    "ATR_PERIOD",
    "LONG_SLOPE_LOOKBACK",
    "LONG_TREND_EMA_LOOKBACK",
    "SHORT_SLOPE_LOOKBACK",
    "SHORT_TREND_EMA_LOOKBACK",
    "PULLBACK_BUFFER_PCT",
    "SHORT_RSI_MIN",
    "LONG_ADX_THRESHOLD",
    "SHORT_ADX_THRESHOLD",
    "VOLUME_MA_PERIOD",
    "ENABLE_VOLUME_MA_ENTRY_FILTER",
    "VOLUME_MA_ENTRY_MULTIPLIER",
    "LONG_VOLUME_RATIO_REQUIRED",
    "SHORT_VOLUME_RATIO_REQUIRED",
    "ENABLE_MACD_ENTRY_FILTER",
    "MACD_ENTRY_FILTER_MODE",
    "MACD_FAST_PERIOD",
    "MACD_SLOW_PERIOD",
    "MACD_SIGNAL_PERIOD",
    "ENABLE_SHORT_PULLBACK",
    "ENABLE_LONG_RESUME",
    "ENABLE_SHORT_RELIEF_RALLY",
    "ENABLE_SHORT_RESUME",
    "ALLOW_TRIGGERLESS_ENTRIES",
    "BYPASS_WEAK_REGIME_GATE",
    "ALLOW_WEAK_BULL_ATR_LONG_ENTRIES",
    "DISABLE_SHORT_SCORE_GATE",
    "BUY_RSI_SIGNAL",
    "SELL_RSI_SIGNAL",
    "LONG_PULLBACK_MIN_TREND_STRENGTH_PCT",
    "LONG_MIN_ATR_PCT",
    "SHORT_MIN_ATR_PCT",
    "LONG_TREND_GAP_PCT",
    "SHORT_TREND_GAP_PCT",
    "LONG_FAST_SLOW_GAP_PCT",
    "SHORT_FAST_SLOW_GAP_PCT",
    "LONG_PULLBACK_HOT_CONTEXT_GAP_PCT",
    "LONG_PULLBACK_HOT_ATR_PCT",
    "LONG_RESUME_HOT_CONTEXT_GAP_PCT",
    "EXPERIMENTAL_LONG_SIDE_LOGIC",
    "LONG_STOP_LOSS_PCT",
    "SHORT_STOP_LOSS_PCT",
    "LONG_TAKE_PROFIT_PCT",
    "SHORT_TAKE_PROFIT_PCT",
    "LONG_TRAILING_STOP_PCT",
    "SHORT_TRAILING_STOP_PCT",
    "TREND_RESUME_LONG_STOP_LOSS_PCT",
    "TREND_RESUME_LONG_PARTIAL_TARGET_PCT",
    "TREND_RESUME_LONG_TRAILING_TRIGGER_PCT",
    "TREND_RESUME_LONG_TRAILING_STOP_PCT",
    "TREND_RESUME_LONG_USE_FIXED_STOP",
    "TREND_RESUME_LONG_MIN_CONTEXT_GAP_PCT",
    "TREND_RESUME_LONG_MIN_ADX",
    "TREND_RESUME_LONG_MIN_TREND_STRENGTH_PCT",
    "TREND_RESUME_SHORT_STOP_LOSS_PCT",
    "TREND_RESUME_SHORT_PARTIAL_TARGET_PCT",
    "TREND_RESUME_SHORT_TRAILING_TRIGGER_PCT",
    "TREND_RESUME_SHORT_TRAILING_STOP_PCT",
    "TREND_RESUME_SHORT_USE_FIXED_STOP",
    "TREND_RESUME_SHORT_BLOCKED_ENTRY_HOURS_UTC",
    "PULLBACK_LONG_STOP_LOSS_PCT",
    "PULLBACK_LONG_PARTIAL_TARGET_PCT",
    "PULLBACK_LONG_TRAILING_TRIGGER_PCT",
    "PULLBACK_LONG_TRAILING_STOP_PCT",
    "PULLBACK_LONG_USE_FIXED_STOP",
    "PULLBACK_SHORT_STOP_LOSS_PCT",
    "PULLBACK_SHORT_PARTIAL_TARGET_PCT",
    "PULLBACK_SHORT_TRAILING_TRIGGER_PCT",
    "PULLBACK_SHORT_TRAILING_STOP_PCT",
    "PULLBACK_SHORT_USE_FIXED_STOP",
    "RELIEF_RALLY_SHORT_STOP_LOSS_PCT",
    "RELIEF_RALLY_SHORT_PARTIAL_TARGET_PCT",
    "RELIEF_RALLY_SHORT_TRAILING_TRIGGER_PCT",
    "RELIEF_RALLY_SHORT_TRAILING_STOP_PCT",
    "RELIEF_RALLY_SHORT_USE_FIXED_STOP",
    "RELIEF_RALLY_SHORT_MIN_CONTEXT_GAP_PCT",
    "RELIEF_RALLY_SHORT_MIN_ADX",
    "SHORT_BREAKDOWN_BUFFER_PCT",
    "SHORT_PULLBACK_MIN_TREND_STRENGTH_PCT",
    "SHORT_PULLBACK_MIN_CONTEXT_GAP_PCT",
    "SHORT_PULLBACK_MIN_ADX",
    "EXPERIMENTAL_SHORT_SIDE_LOGIC",
    "SHORT_REQUIRE_STRICT_REGIME",
    "USE_ENTRY_HOUR_BLOCKS",
    "BLOCKED_LONG_ENTRY_HOURS_UTC",
    "BLOCKED_SHORT_ENTRY_HOURS_UTC",
    "TRAILING_TRIGGER_PCT",
    "PARTIAL_TARGET_PCT",
    "MIN_TARGET_DISTANCE_PCT",
    "MIN_TREND_STRENGTH_PCT",
    "MIN_TREND_STRENGTH_PCT_SHORT",
    "ALLOW_LONG",
    "CANDLE_WICK_REJECTION_RATIO_SHORT_RELIEF",
    "SHORT_RSI_MIN_RELIEF_RALLY",
    "SHORT_RSI_MAX_RELIEF_RALLY",
    "ALLOW_SHORT",
    "BLOCK_UNKNOWN_REGIME",
    "USE_NEXT_CANDLE_OPEN_FOR_BACKTEST",
    "EXECUTION_PROFILE",
    "FEE_PCT",
    "LIVE_TRADING_CONFIRMATION",
    "MAX_REAL_RISK_PER_TRADE_PCT_START",
    "MAX_DAILY_REAL_LOSS_PCT",
    "MAX_CONSECUTIVE_REAL_LOSSES",
    "MAX_OPEN_REAL_TRADES",
    "normalize_symbol",
    "get_backtest_governance_profile",
    "get_symbol_family_key",
    "load_symbol_approvals",
    "load_symbol_strategy_overrides",
    "load_saved_strategy_profiles",
    "get_saved_strategy_profile",
    "save_runtime_strategy_profile",
    "get_symbol_strategy_override_record",
    "get_symbol_strategy_overrides",
    "apply_symbol_strategy_overrides",
    "get_symbol_validation_record",
    "is_symbol_runtime_approved",
    # app classes
    "AppConfig",
    "ExchangeConfig",
    "ProductionConfig",
]
