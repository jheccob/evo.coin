"""
Sistema de banco de dados usando SQLite para persistir dados do trading bot
"""
import hmac
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import List, Dict, Optional, Any
from utils.timezone_utils import get_brazil_datetime_naive, format_brazil_time
from config import AppConfig, ProductionConfig
from market_state_engine import (
    market_states_to_setup_allowlist,
    setup_types_to_market_state_allowlist,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


_SQLITE_LITERAL_DATETIME_PATTERN = re.compile(
    r"datetime\(\s*'now'\s*,\s*'(?P<interval>[^']+)'\s*\)",
    flags=re.IGNORECASE,
)
_SQLITE_NOW_DATETIME_PATTERN = re.compile(
    r"datetime\(\s*'now'\s*\)",
    flags=re.IGNORECASE,
)


def _looks_like_postgres_url(database_url: str) -> bool:
    normalized = str(database_url or "").strip().lower()
    return normalized.startswith(("postgres://", "postgresql://"))


def _safe_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)


def _safe_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return float(default)


def _replace_literal_datetime_now(match: re.Match) -> str:
    raw_interval = str(match.group("interval") or "").strip()
    if not raw_interval:
        return "NOW()"
    sign = "+"
    interval_body = raw_interval
    if raw_interval.startswith("-"):
        sign = "-"
        interval_body = raw_interval[1:].strip()
    elif raw_interval.startswith("+"):
        sign = "+"
        interval_body = raw_interval[1:].strip()
    if not interval_body:
        return "NOW()"
    return f"(NOW() {sign} INTERVAL '{interval_body}')"


def _sqlite_to_postgres_sql(sql: str) -> str:
    translated = str(sql or "")
    translated = translated.replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
    )
    translated = translated.replace("AUTOINCREMENT", "")
    translated = translated.replace("BOOLEAN", "INTEGER")
    translated = re.sub(r"\bDEFAULT\s+FALSE\b", "DEFAULT 0", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bDEFAULT\s+TRUE\b", "DEFAULT 1", translated, flags=re.IGNORECASE)
    translated = translated.replace("datetime('now', ?)", "(NOW() + %s::interval)")
    translated = _SQLITE_LITERAL_DATETIME_PATTERN.sub(_replace_literal_datetime_now, translated)
    translated = _SQLITE_NOW_DATETIME_PATTERN.sub("NOW()", translated)

    if re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", translated, flags=re.IGNORECASE):
        translated = re.sub(
            r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            translated,
            flags=re.IGNORECASE,
        )
        stripped = translated.rstrip()
        if "ON CONFLICT" not in stripped.upper():
            translated = stripped.rstrip(";") + " ON CONFLICT DO NOTHING"

    translated = translated.replace("?", "%s")
    translated = re.sub(r"(?<!:)%s\s+IS\s+NULL", "%s::text IS NULL", translated, flags=re.IGNORECASE)
    return translated


class _PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def execute(self, sql, params=None):
        translated_sql = _sqlite_to_postgres_sql(sql)
        self._cursor.execute(translated_sql, params if params is not None else ())
        self.lastrowid = None
        if translated_sql.lstrip().upper().startswith("INSERT"):
            try:
                self._cursor.execute("SELECT LASTVAL() AS lastval")
                lastval_row = self._cursor.fetchone()
                if isinstance(lastval_row, dict):
                    self.lastrowid = lastval_row.get("lastval")
                elif lastval_row:
                    self.lastrowid = lastval_row[0]
            except Exception:
                self.lastrowid = None
        return self

    def executemany(self, sql, seq_of_params):
        translated_sql = _sqlite_to_postgres_sql(sql)
        self._cursor.executemany(translated_sql, seq_of_params)
        self.lastrowid = None
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()

    def __getattr__(self, item):
        return getattr(self._cursor, item)


class _PostgresConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PostgresCursor(self._conn.cursor(row_factory=dict_row))

    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def build_strategy_version(
    symbol: str,
    timeframe: str,
    rsi_period: Optional[int] = None,
    rsi_min: Optional[int] = None,
    rsi_max: Optional[int] = None,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    require_volume: bool = False,
    require_trend: bool = False,
    avoid_ranging: bool = False,
    context_timeframe: Optional[str] = None,
) -> str:
    safe_symbol = (symbol or "UNKNOWN").replace("/", "").upper()
    safe_timeframe = timeframe or "na"
    rsi_period = int(rsi_period or 0)
    rsi_min = int(rsi_min or 0)
    rsi_max = int(rsi_max or 0)
    stop_loss_pct = float(stop_loss_pct or 0.0)
    take_profit_pct = float(take_profit_pct or 0.0)

    version = (
        f"{safe_symbol}-{safe_timeframe}-"
        f"rsi{rsi_period}-{rsi_min}-{rsi_max}-"
        f"sl{stop_loss_pct:.2f}-tp{take_profit_pct:.2f}-"
        f"v{int(bool(require_volume))}-t{int(bool(require_trend))}-"
        f"r{int(bool(avoid_ranging))}"
    )
    if context_timeframe and context_timeframe != timeframe:
        version += f"-ctx{context_timeframe}"
    return version

class TradingDatabase:
    def __init__(self, db_path: str = AppConfig.DB_PATH):
        self.db_path = db_path
        self.database_url = str(os.getenv("DATABASE_URL", "")).strip()
        self.backend = "postgres" if _looks_like_postgres_url(self.database_url) else "sqlite"
        self.postgres_connect_timeout_sec = max(2, _safe_env_int("POSTGRES_CONNECT_TIMEOUT_SEC", 10))
        self.postgres_connect_retries = max(1, _safe_env_int("POSTGRES_CONNECT_RETRIES", 10))
        self.postgres_retry_delay_sec = max(0.5, _safe_env_float("POSTGRES_RETRY_DELAY_SEC", 3.0))
        self.postgres_retry_backoff = max(1.0, _safe_env_float("POSTGRES_RETRY_BACKOFF", 1.5))
        self.postgres_retry_max_delay_sec = max(
            self.postgres_retry_delay_sec,
            _safe_env_float("POSTGRES_RETRY_MAX_DELAY_SEC", 15.0),
        )
        if self.backend == "postgres" and psycopg is None:
            print(
                "[database] DATABASE_URL detectada, mas psycopg nao esta disponivel. "
                "Fallback para sqlite.",
                flush=True,
            )
            self.backend = "sqlite"
        if self.backend == "sqlite":
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.init_database()

    @staticmethod
    def _is_retryable_postgres_error(exc: Exception) -> bool:
        message = str(exc or "").strip().lower()
        retryable_tokens = (
            "temporary failure in name resolution",
            "name or service not known",
            "could not translate host name",
            "connection refused",
            "connection timed out",
            "timeout expired",
            "server closed the connection unexpectedly",
            "network is unreachable",
            "could not connect",
            "connection is bad",
            "connection reset",
            "connection failed",
        )
        return any(token in message for token in retryable_tokens)

    def _connect_postgres_with_retry(self):
        last_exc: Optional[Exception] = None
        delay = float(self.postgres_retry_delay_sec)

        for attempt in range(1, int(self.postgres_connect_retries) + 1):
            try:
                conn = psycopg.connect(
                    self.database_url,
                    connect_timeout=int(self.postgres_connect_timeout_sec),
                )
                return _PostgresConnection(conn)
            except Exception as exc:
                last_exc = exc
                is_retryable = self._is_retryable_postgres_error(exc)
                if attempt >= int(self.postgres_connect_retries) or not is_retryable:
                    break

                print(
                    "[database] falha ao conectar no postgres "
                    f"(tentativa {attempt}/{self.postgres_connect_retries}): {exc}. "
                    f"Nova tentativa em {delay:.1f}s.",
                    flush=True,
                )
                time.sleep(delay)
                delay = min(delay * float(self.postgres_retry_backoff), float(self.postgres_retry_max_delay_sec))

        assert last_exc is not None
        raise last_exc
    
    def get_connection(self):
        """Criar conexao com banco de dados"""
        if self.backend == "postgres":
            return self._connect_postgres_with_retry()
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row  # Para retornar dicionarios
        self._configure_sqlite_connection(conn)
        return conn

    @staticmethod
    def _configure_sqlite_connection(conn: sqlite3.Connection):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    
    def init_database(self):
        """Inicializar estrutura do banco de dados"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Tabela para sinais de trading
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                context_timeframe TEXT,
                strategy_version TEXT,
                regime TEXT,
                signal_type TEXT NOT NULL,  -- 'buy', 'sell', 'hold'
                price REAL NOT NULL,
                rsi REAL,
                macd_signal TEXT,
                macd_value REAL,
                signal_strength REAL,
                volume REAL,
                candle_timestamp TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at_br TEXT,  -- Horário brasileiro formatado
                sent_telegram BOOLEAN DEFAULT FALSE,
                sent_telegram_at TEXT,
                telegram_error TEXT
            )
        ''')
        
        # Tabela para configurações
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabela para histórico de análises
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analysis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                analysis_data TEXT,  -- JSON com dados da análise
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at_br TEXT
            )
        ''')
        
        # Tabela para estatísticas de performance
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS performance_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,  -- 'daily', 'weekly', 'monthly'
                date TEXT NOT NULL,
                total_signals INTEGER DEFAULT 0,
                buy_signals INTEGER DEFAULT 0,
                sell_signals INTEGER DEFAULT 0,
                accuracy REAL DEFAULT 0.0,
                profit_loss REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS telegram_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                is_admin INTEGER NOT NULL DEFAULT 0,
                joined_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                analysis_count_today INTEGER NOT NULL DEFAULT 0,
                last_reset TEXT,
                last_analysis TEXT
            )
            '''
        )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                context_timeframe TEXT,
                strategy_version TEXT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                initial_balance REAL NOT NULL,
                final_balance REAL NOT NULL,
                net_profit REAL NOT NULL,
                total_return_pct REAL NOT NULL,
                total_trades INTEGER NOT NULL DEFAULT 0,
                winning_trades INTEGER NOT NULL DEFAULT 0,
                losing_trades INTEGER NOT NULL DEFAULT 0,
                win_rate REAL NOT NULL DEFAULT 0.0,
                max_drawdown REAL NOT NULL DEFAULT 0.0,
                sharpe_ratio REAL NOT NULL DEFAULT 0.0,
                profit_factor REAL NOT NULL DEFAULT 0.0,
                avg_profit REAL NOT NULL DEFAULT 0.0,
                avg_loss REAL NOT NULL DEFAULT 0.0,
                expectancy_pct REAL NOT NULL DEFAULT 0.0,
                rsi_period INTEGER,
                rsi_min INTEGER,
                rsi_max INTEGER,
                stop_loss_pct REAL DEFAULT 0.0,
                take_profit_pct REAL DEFAULT 0.0,
                fee_rate REAL DEFAULT 0.0,
                slippage REAL DEFAULT 0.0,
                position_size_pct REAL DEFAULT 1.0,
                require_volume BOOLEAN DEFAULT FALSE,
                require_trend BOOLEAN DEFAULT FALSE,
                avoid_ranging BOOLEAN DEFAULT FALSE,
                validation_split_pct REAL DEFAULT 0.0,
                in_sample_end TEXT,
                out_of_sample_start TEXT,
                in_sample_return_pct REAL DEFAULT 0.0,
                in_sample_profit_factor REAL DEFAULT 0.0,
                in_sample_win_rate REAL DEFAULT 0.0,
                in_sample_total_trades INTEGER DEFAULT 0,
                out_of_sample_return_pct REAL DEFAULT 0.0,
                out_of_sample_profit_factor REAL DEFAULT 0.0,
                out_of_sample_win_rate REAL DEFAULT 0.0,
                out_of_sample_total_trades INTEGER DEFAULT 0,
                out_of_sample_expectancy_pct REAL DEFAULT 0.0,
                out_of_sample_passed BOOLEAN DEFAULT FALSE,
                walk_forward_windows INTEGER DEFAULT 0,
                walk_forward_passed BOOLEAN DEFAULT FALSE,
                walk_forward_pass_rate_pct REAL DEFAULT 0.0,
                walk_forward_avg_oos_return_pct REAL DEFAULT 0.0,
                walk_forward_avg_oos_profit_factor REAL DEFAULT 0.0,
                walk_forward_avg_oos_expectancy_pct REAL DEFAULT 0.0,
                objective_status TEXT,
                objective_score REAL DEFAULT 0.0,
                approved_market_state TEXT,
                approved_market_states TEXT,
                approved_market_state_trades INTEGER DEFAULT 0,
                approved_market_state_profit_factor REAL DEFAULT 0.0,
                approved_setup_type TEXT,
                approved_setup_types TEXT,
                approved_setup_trades INTEGER DEFAULT 0,
                approved_setup_profit_factor REAL DEFAULT 0.0,
                evaluation_period_days REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at_br TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                context_timeframe TEXT,
                setup_name TEXT,
                strategy_version TEXT,
                regime TEXT,
                market_state TEXT,
                execution_mode TEXT,
                signal_score REAL DEFAULT 0.0,
                atr REAL DEFAULT 0.0,
                entry_timestamp TEXT,
                entry_reason TEXT,
                entry_quality TEXT,
                rejection_reason TEXT,
                exit_timestamp TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                initial_stop_price REAL,
                initial_take_price REAL,
                final_stop_price REAL,
                final_take_price REAL,
                break_even_active BOOLEAN DEFAULT FALSE,
                trailing_active BOOLEAN DEFAULT FALSE,
                protection_level TEXT,
                regime_exit_flag BOOLEAN DEFAULT FALSE,
                structure_exit_flag BOOLEAN DEFAULT FALSE,
                post_pump_protection BOOLEAN DEFAULT FALSE,
                mfe_pct REAL DEFAULT 0.0,
                mae_pct REAL DEFAULT 0.0,
                max_unrealized_rr REAL DEFAULT 0.0,
                exit_reason TEXT,
                profit_loss_pct REAL NOT NULL,
                profit_loss REAL NOT NULL,
                signal TEXT NOT NULL,
                side TEXT,
                reason TEXT,
                sample_type TEXT DEFAULT 'backtest',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                context_timeframe TEXT,
                setup_name TEXT,
                strategy_version TEXT,
                regime TEXT,
                signal_score REAL DEFAULT 0.0,
                atr REAL DEFAULT 0.0,
                sample_type TEXT DEFAULT 'paper',
                signal TEXT NOT NULL,
                side TEXT NOT NULL,
                source TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                entry_reason TEXT,
                entry_quality TEXT,
                rejection_reason TEXT,
                entry_price REAL NOT NULL,
                stop_loss_pct REAL NOT NULL DEFAULT 0.0,
                take_profit_pct REAL NOT NULL DEFAULT 0.0,
                fee_rate REAL DEFAULT 0.0,
                slippage REAL DEFAULT 0.0,
                stop_loss_price REAL,
                take_profit_price REAL,
                initial_stop_price REAL,
                initial_take_price REAL,
                final_stop_price REAL,
                final_take_price REAL,
                break_even_active BOOLEAN DEFAULT FALSE,
                trailing_active BOOLEAN DEFAULT FALSE,
                protection_level TEXT,
                regime_exit_flag BOOLEAN DEFAULT FALSE,
                structure_exit_flag BOOLEAN DEFAULT FALSE,
                post_pump_protection BOOLEAN DEFAULT FALSE,
                mfe_pct REAL DEFAULT 0.0,
                mae_pct REAL DEFAULT 0.0,
                max_unrealized_rr REAL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                outcome TEXT NOT NULL DEFAULT 'OPEN',
                close_reason TEXT,
                exit_reason TEXT,
                exit_timestamp TEXT,
                exit_price REAL,
                result_pct REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at_br TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                context_timeframe TEXT,
                strategy_version TEXT NOT NULL,
                market_state TEXT,
                allowed_market_states TEXT,
                setup_type TEXT,
                allowed_setup_types TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                rsi_period INTEGER,
                rsi_min INTEGER,
                rsi_max INTEGER,
                stop_loss_pct REAL DEFAULT 0.0,
                take_profit_pct REAL DEFAULT 0.0,
                require_volume BOOLEAN DEFAULT FALSE,
                require_trend BOOLEAN DEFAULT FALSE,
                avoid_ranging BOOLEAN DEFAULT FALSE,
                source_run_id INTEGER,
                notes TEXT,
                promoted_at_br TEXT,
                deactivated_at_br TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at_br TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at_br TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT,
                evaluation_type TEXT NOT NULL DEFAULT 'combined',
                total_backtest_runs INTEGER DEFAULT 0,
                total_backtest_trades INTEGER DEFAULT 0,
                avg_return_pct REAL DEFAULT 0.0,
                avg_profit_factor REAL DEFAULT 0.0,
                avg_expectancy_pct REAL DEFAULT 0.0,
                avg_out_of_sample_return_pct REAL DEFAULT 0.0,
                avg_out_of_sample_profit_factor REAL DEFAULT 0.0,
                avg_out_of_sample_expectancy_pct REAL DEFAULT 0.0,
                passed_oos_runs INTEGER DEFAULT 0,
                avg_walk_forward_pass_rate_pct REAL DEFAULT 0.0,
                avg_walk_forward_oos_return_pct REAL DEFAULT 0.0,
                avg_walk_forward_oos_profit_factor REAL DEFAULT 0.0,
                passed_walk_forward_runs INTEGER DEFAULT 0,
                avg_max_drawdown REAL DEFAULT 0.0,
                total_net_profit REAL DEFAULT 0.0,
                paper_closed_trades INTEGER DEFAULT 0,
                paper_win_rate REAL DEFAULT 0.0,
                paper_avg_result_pct REAL DEFAULT 0.0,
                paper_total_result_pct REAL DEFAULT 0.0,
                paper_profit_factor REAL DEFAULT 0.0,
                baseline_source TEXT,
                edge_status TEXT,
                governance_status TEXT,
                quality_score REAL DEFAULT 0.0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at_br TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT,
                setup_type TEXT,
                regime TEXT,
                regime_score REAL DEFAULT 0.0,
                trend_state TEXT,
                volatility_state TEXT,
                context_bias TEXT,
                directional_bias TEXT,
                structure_state TEXT,
                event_type TEXT,
                regime_phase TEXT,
                context_score REAL DEFAULT 0.0,
                confirmation_state TEXT,
                entry_quality TEXT,
                entry_score REAL DEFAULT 0.0,
                risk_mode TEXT,
                reading_execution_mode TEXT,
                context_source TEXT,
                position_size REAL DEFAULT 0.0,
                position_notional REAL DEFAULT 0.0,
                risk_amount REAL DEFAULT 0.0,
                stop_initial REAL,
                take_initial REAL,
                stop_final REAL,
                take_final REAL,
                exit_reason TEXT,
                entry_timestamp TEXT,
                exit_timestamp TEXT,
                holding_time_minutes REAL DEFAULT 0.0,
                holding_candles INTEGER DEFAULT 0,
                pnl_pct REAL DEFAULT 0.0,
                pnl_abs REAL DEFAULT 0.0,
                mfe_pct REAL DEFAULT 0.0,
                mae_pct REAL DEFAULT 0.0,
                rr_realized REAL DEFAULT 0.0,
                break_even_activated BOOLEAN DEFAULT FALSE,
                trailing_activated BOOLEAN DEFAULT FALSE,
                regime_shift_during_trade BOOLEAN DEFAULT FALSE,
                profit_given_back_pct REAL DEFAULT 0.0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signal_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT,
                timestamp TEXT NOT NULL,
                candidate_signal TEXT,
                approved_signal TEXT,
                blocked_signal TEXT,
                block_reason TEXT,
                regime TEXT,
                regime_score REAL DEFAULT 0.0,
                trend_state TEXT,
                volatility_state TEXT,
                context_bias TEXT,
                directional_bias TEXT,
                structure_state TEXT,
                event_type TEXT,
                regime_phase TEXT,
                context_score REAL DEFAULT 0.0,
                confirmation_state TEXT,
                entry_quality TEXT,
                entry_score REAL DEFAULT 0.0,
                scenario_score REAL DEFAULT 0.0,
                setup_type TEXT,
                market_state TEXT,
                execution_mode TEXT,
                reading_execution_mode TEXT,
                context_source TEXT,
                risk_mode TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS setup_regime_baselines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                regime TEXT NOT NULL,
                baseline_source TEXT NOT NULL DEFAULT 'backtest',
                baseline_profit_factor REAL DEFAULT 0.0,
                baseline_expectancy_pct REAL DEFAULT 0.0,
                baseline_win_rate REAL DEFAULT 0.0,
                baseline_drawdown REAL DEFAULT 0.0,
                baseline_trade_count INTEGER DEFAULT 0,
                total_return_pct REAL DEFAULT 0.0,
                oos_profit_factor REAL DEFAULT 0.0,
                oos_expectancy_pct REAL DEFAULT 0.0,
                walk_forward_pass_rate_pct REAL DEFAULT 0.0,
                performance_status TEXT,
                window_days INTEGER DEFAULT 0,
                notes TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, timeframe, strategy_version, regime)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alignment_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                regime TEXT,
                window_days INTEGER DEFAULT 0,
                window_trades INTEGER DEFAULT 0,
                baseline_source TEXT,
                baseline_profit_factor REAL DEFAULT 0.0,
                baseline_expectancy_pct REAL DEFAULT 0.0,
                baseline_win_rate REAL DEFAULT 0.0,
                baseline_trade_count INTEGER DEFAULT 0,
                paper_profit_factor REAL DEFAULT 0.0,
                paper_expectancy_pct REAL DEFAULT 0.0,
                paper_win_rate REAL DEFAULT 0.0,
                paper_trade_count INTEGER DEFAULT 0,
                live_profit_factor REAL DEFAULT 0.0,
                live_expectancy_pct REAL DEFAULT 0.0,
                live_win_rate REAL DEFAULT 0.0,
                live_trade_count INTEGER DEFAULT 0,
                paper_pf_alignment_pct REAL DEFAULT 0.0,
                paper_expectancy_alignment_pct REAL DEFAULT 0.0,
                paper_win_rate_delta_pct REAL DEFAULT 0.0,
                live_pf_alignment_pct REAL DEFAULT 0.0,
                live_expectancy_alignment_pct REAL DEFAULT 0.0,
                live_win_rate_delta_pct REAL DEFAULT 0.0,
                alignment_status TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS governance_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                regime TEXT,
                governance_status TEXT NOT NULL,
                governance_mode TEXT NOT NULL,
                current_regime_status TEXT,
                alignment_status TEXT,
                promotion_status TEXT,
                degradation_status TEXT,
                action TEXT,
                action_reason TEXT,
                allowed_regimes TEXT,
                reduced_regimes TEXT,
                blocked_regimes TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS setup_governance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                regime TEXT,
                previous_status TEXT,
                previous_mode TEXT,
                governance_status TEXT NOT NULL,
                governance_mode TEXT NOT NULL,
                alignment_status TEXT,
                promotion_status TEXT,
                degradation_status TEXT,
                action TEXT,
                action_reason TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                account_alias TEXT,
                exchange TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                live_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                paper_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                capital_base REAL DEFAULT 0.0,
                risk_mode TEXT DEFAULT 'normal',
                allowed_symbols TEXT,
                allowed_timeframes TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, account_id)
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_risk_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                max_risk_per_trade REAL DEFAULT 0.0,
                max_daily_loss REAL DEFAULT 0.0,
                max_drawdown REAL DEFAULT 0.0,
                max_portfolio_open_risk_pct REAL DEFAULT 0.0,
                allowed_position_count INTEGER DEFAULT 0,
                preferred_symbols TEXT,
                leverage_cap REAL DEFAULT 0.0,
                risk_mode TEXT DEFAULT 'normal',
                live_enabled BOOLEAN DEFAULT FALSE,
                paper_enabled BOOLEAN DEFAULT TRUE,
                is_valid BOOLEAN DEFAULT TRUE,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, account_id)
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_exchange_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                exchange TEXT NOT NULL,
                credential_alias TEXT,
                api_key_ref TEXT,
                token_ref TEXT,
                encrypted_api_key TEXT NOT NULL,
                encrypted_api_secret TEXT NOT NULL,
                permissions_read BOOLEAN DEFAULT TRUE,
                permissions_trade BOOLEAN DEFAULT TRUE,
                permissions_withdraw BOOLEAN DEFAULT FALSE,
                permission_status TEXT DEFAULT 'unknown',
                token_status TEXT DEFAULT 'unknown',
                reconciliation_status TEXT DEFAULT 'unknown',
                last_validated_at TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, account_id, exchange)
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_live_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                exchange TEXT,
                symbol TEXT,
                timeframe TEXT,
                strategy_version TEXT,
                client_order_id TEXT,
                exchange_order_id TEXT,
                side TEXT,
                order_type TEXT,
                quantity REAL DEFAULT 0.0,
                price REAL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_live_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                exchange TEXT,
                symbol TEXT,
                timeframe TEXT,
                strategy_version TEXT,
                side TEXT,
                quantity REAL DEFAULT 0.0,
                entry_price REAL DEFAULT 0.0,
                mark_price REAL DEFAULT 0.0,
                unrealized_pnl REAL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'open',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                exchange TEXT,
                symbol TEXT,
                timeframe TEXT,
                strategy_version TEXT,
                event_type TEXT NOT NULL,
                event_status TEXT NOT NULL,
                message TEXT,
                details_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_governance_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                exchange TEXT,
                symbol TEXT,
                timeframe TEXT,
                strategy_version TEXT,
                governance_status TEXT DEFAULT 'unknown',
                governance_mode TEXT DEFAULT 'blocked',
                blocked BOOLEAN DEFAULT FALSE,
                block_reason TEXT,
                notes TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, account_id, exchange, symbol, timeframe, strategy_version)
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS dashboard_user_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                login_name TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                require_password_change BOOLEAN NOT NULL DEFAULT FALSE,
                notes TEXT,
                last_login_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS dashboard_user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                login_name TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked BOOLEAN NOT NULL DEFAULT FALSE,
                last_seen_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS dashboard_user_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                plan_code TEXT NOT NULL DEFAULT 'free',
                status TEXT NOT NULL DEFAULT 'inactive',
                started_at TEXT,
                expires_at TEXT,
                auto_renew BOOLEAN NOT NULL DEFAULT FALSE,
                payment_provider TEXT,
                external_subscription_id TEXT,
                credits_balance REAL NOT NULL DEFAULT 0.0,
                last_payment_at TEXT,
                next_billing_at TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS dashboard_signup_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login_name TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                contact_text TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_notes TEXT,
                approved_user_id INTEGER,
                notes TEXT
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS bot_runtime_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                runtime_key TEXT NOT NULL UNIQUE,
                runtime_name TEXT,
                environment TEXT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy_version TEXT,
                status TEXT NOT NULL DEFAULT 'starting',
                last_heartbeat_at TEXT,
                last_candle_timestamp TEXT,
                last_signal TEXT,
                last_signal_reason TEXT,
                last_signal_price REAL,
                position_side TEXT,
                position_entry_price REAL,
                blocked BOOLEAN NOT NULL DEFAULT FALSE,
                block_reason TEXT,
                last_error TEXT,
                state_payload TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        self._ensure_column(cursor, 'trading_signals', 'context_timeframe', 'TEXT')
        self._ensure_column(cursor, 'trading_signals', 'strategy_version', 'TEXT')
        self._ensure_column(cursor, 'trading_signals', 'regime', 'TEXT')
        self._ensure_column(cursor, 'trading_signals', 'candle_timestamp', 'TEXT')
        self._ensure_column(cursor, 'trading_signals', 'sent_telegram', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'trading_signals', 'sent_telegram_at', 'TEXT')
        self._ensure_column(cursor, 'trading_signals', 'telegram_error', 'TEXT')
        cursor.execute(
            '''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trading_signals_unique_candle_signal
            ON trading_signals(symbol, timeframe, signal_type, candle_timestamp)
            WHERE candle_timestamp IS NOT NULL
            '''
        )
        self._ensure_column(cursor, 'backtest_runs', 'context_timeframe', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'strategy_version', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'context_timeframe', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'strategy_version', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'context_timeframe', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'strategy_version', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'execution_mode', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'setup_name', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'regime', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'signal_score', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'atr', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'entry_reason', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'entry_quality', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'rejection_reason', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'exit_reason', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'market_state', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'execution_mode', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'initial_stop_price', 'REAL')
        self._ensure_column(cursor, 'backtest_trades', 'initial_take_price', 'REAL')
        self._ensure_column(cursor, 'backtest_trades', 'final_stop_price', 'REAL')
        self._ensure_column(cursor, 'backtest_trades', 'final_take_price', 'REAL')
        self._ensure_column(cursor, 'backtest_trades', 'break_even_active', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_trades', 'trailing_active', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_trades', 'protection_level', 'TEXT')
        self._ensure_column(cursor, 'backtest_trades', 'regime_exit_flag', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_trades', 'structure_exit_flag', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_trades', 'post_pump_protection', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_trades', 'mfe_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'mae_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'max_unrealized_rr', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'sample_type', "TEXT DEFAULT 'backtest'")
        self._ensure_column(cursor, 'backtest_trades', 'risk_mode', "TEXT DEFAULT 'normal'")
        self._ensure_column(cursor, 'backtest_trades', 'risk_amount', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'position_notional', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'quantity', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_trades', 'size_reduced', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_trades', 'risk_reason', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'setup_name', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'regime', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'signal_score', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'atr', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'entry_reason', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'entry_quality', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'rejection_reason', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'exit_reason', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'initial_stop_price', 'REAL')
        self._ensure_column(cursor, 'paper_trades', 'initial_take_price', 'REAL')
        self._ensure_column(cursor, 'paper_trades', 'final_stop_price', 'REAL')
        self._ensure_column(cursor, 'paper_trades', 'final_take_price', 'REAL')
        self._ensure_column(cursor, 'paper_trades', 'break_even_active', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'trailing_active', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'protection_level', 'TEXT')
        self._ensure_column(cursor, 'paper_trades', 'regime_exit_flag', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'structure_exit_flag', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'post_pump_protection', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'mfe_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'mae_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'max_unrealized_rr', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'sample_type', "TEXT DEFAULT 'paper'")
        self._ensure_column(cursor, 'paper_trades', 'fee_rate', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'slippage', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'avoid_ranging', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'planned_risk_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'planned_risk_amount', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'planned_position_notional', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'planned_quantity', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'account_reference_balance', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'paper_trades', 'risk_mode', "TEXT DEFAULT 'normal'")
        self._ensure_column(cursor, 'paper_trades', 'size_reduced', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'paper_trades', 'risk_reason', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'validation_split_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'in_sample_end', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_start', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'in_sample_return_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'in_sample_profit_factor', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'in_sample_win_rate', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'in_sample_total_trades', 'INTEGER DEFAULT 0')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_return_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_profit_factor', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_win_rate', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_total_trades', 'INTEGER DEFAULT 0')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_expectancy_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'out_of_sample_passed', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_runs', 'walk_forward_windows', 'INTEGER DEFAULT 0')
        self._ensure_column(cursor, 'backtest_runs', 'walk_forward_passed', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'backtest_runs', 'walk_forward_pass_rate_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'walk_forward_avg_oos_return_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'walk_forward_avg_oos_profit_factor', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'walk_forward_avg_oos_expectancy_pct', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'objective_status', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'objective_score', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'approved_market_state', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'approved_market_states', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'approved_market_state_trades', 'INTEGER DEFAULT 0')
        self._ensure_column(cursor, 'backtest_runs', 'approved_market_state_profit_factor', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'approved_setup_type', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'approved_setup_types', 'TEXT')
        self._ensure_column(cursor, 'backtest_runs', 'approved_setup_trades', 'INTEGER DEFAULT 0')
        self._ensure_column(cursor, 'backtest_runs', 'approved_setup_profit_factor', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'backtest_runs', 'evaluation_period_days', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'signal_audit', 'market_state', 'TEXT')
        self._ensure_column(cursor, 'signal_audit', 'execution_mode', 'TEXT')
        self._ensure_column(cursor, 'signal_audit', 'reading_execution_mode', 'TEXT')
        self._ensure_column(cursor, 'signal_audit', 'event_type', 'TEXT')
        self._ensure_column(cursor, 'signal_audit', 'regime_phase', 'TEXT')
        self._ensure_column(cursor, 'signal_audit', 'context_score', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'signal_audit', 'directional_bias', 'TEXT')
        self._ensure_column(cursor, 'signal_audit', 'context_source', 'TEXT')
        self._ensure_column(cursor, 'trade_analytics', 'reading_execution_mode', 'TEXT')
        self._ensure_column(cursor, 'trade_analytics', 'event_type', 'TEXT')
        self._ensure_column(cursor, 'trade_analytics', 'regime_phase', 'TEXT')
        self._ensure_column(cursor, 'trade_analytics', 'context_score', 'REAL DEFAULT 0.0')
        self._ensure_column(cursor, 'trade_analytics', 'directional_bias', 'TEXT')
        self._ensure_column(cursor, 'trade_analytics', 'context_source', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'market_state', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'allowed_market_states', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'setup_type', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'allowed_setup_types', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'context_timeframe', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'source_run_id', 'INTEGER')
        self._ensure_column(cursor, 'strategy_profiles', 'avoid_ranging', 'BOOLEAN DEFAULT FALSE')
        self._ensure_column(cursor, 'strategy_profiles', 'notes', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'promoted_at_br', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'deactivated_at_br', 'TEXT')
        self._ensure_column(cursor, 'strategy_profiles', 'updated_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        self._ensure_column(cursor, 'strategy_profiles', 'updated_at_br', 'TEXT')

        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_user_accounts_active
            ON user_accounts(status, live_enabled, paper_enabled)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_user_execution_events_lookup
            ON user_execution_events(user_id, account_id, created_at)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_user_live_orders_lookup
            ON user_live_orders(user_id, account_id, status, created_at)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_user_live_positions_lookup
            ON user_live_positions(user_id, account_id, status, created_at)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_dashboard_user_access_login
            ON dashboard_user_access(login_name)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_dashboard_user_sessions_lookup
            ON dashboard_user_sessions(user_id, expires_at, revoked)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_dashboard_user_subscriptions_status_expiry
            ON dashboard_user_subscriptions(status, expires_at)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_dashboard_signup_requests_status
            ON dashboard_signup_requests(status, requested_at)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_dashboard_signup_requests_login
            ON dashboard_signup_requests(login_name)
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_bot_runtime_state_lookup
            ON bot_runtime_state(symbol, timeframe, updated_at)
            '''
        )

        conn.commit()
        conn.close()

    def _ensure_column(self, cursor, table_name: str, column_name: str, column_definition: str):
        """Adicionar coluna em instalacoes antigas sem destruir o banco existente."""
        if self.backend == "postgres":
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
                LIMIT 1
                """,
                (table_name, column_name),
            )
            if cursor.fetchone() is None:
                postgres_definition = str(column_definition).replace("BOOLEAN", "INTEGER")
                postgres_definition = re.sub(
                    r"\bDEFAULT\s+FALSE\b",
                    "DEFAULT 0",
                    postgres_definition,
                    flags=re.IGNORECASE,
                )
                postgres_definition = re.sub(
                    r"\bDEFAULT\s+TRUE\b",
                    "DEFAULT 1",
                    postgres_definition,
                    flags=re.IGNORECASE,
                )
                try:
                    cursor.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {postgres_definition}"
                    )
                except Exception as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            return

        cursor.execute(f"PRAGMA table_info({table_name})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def _to_json_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=True)

    def _from_json_text(self, value: Any) -> Any:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except Exception:
            return value

    def _to_list(self, raw_value: Any) -> List[str]:
        if raw_value in (None, ""):
            return []
        if isinstance(raw_value, list):
            return [str(item) for item in raw_value if item not in (None, "")]
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed if item not in (None, "")]
            except Exception:
                pass
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return [str(raw_value)]

    def upsert_bot_runtime_state(self, runtime_data: Dict[str, Any]) -> int:
        runtime_key = str(runtime_data.get("runtime_key") or "").strip()
        symbol = str(runtime_data.get("symbol") or "").strip()
        timeframe = str(runtime_data.get("timeframe") or "").strip()
        if not runtime_key:
            raise ValueError("runtime_key e obrigatorio para persistir estado do bot.")
        if not symbol or not timeframe:
            raise ValueError("symbol e timeframe sao obrigatorios para persistir estado do bot.")

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                '''
                INSERT INTO bot_runtime_state (
                    runtime_key, runtime_name, environment, symbol, timeframe, strategy_version,
                    status, last_heartbeat_at, last_candle_timestamp, last_signal, last_signal_reason,
                    last_signal_price, position_side, position_entry_price, blocked, block_reason,
                    last_error, state_payload, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(runtime_key) DO UPDATE SET
                    runtime_name = excluded.runtime_name,
                    environment = excluded.environment,
                    symbol = excluded.symbol,
                    timeframe = excluded.timeframe,
                    strategy_version = excluded.strategy_version,
                    status = excluded.status,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_candle_timestamp = excluded.last_candle_timestamp,
                    last_signal = excluded.last_signal,
                    last_signal_reason = excluded.last_signal_reason,
                    last_signal_price = excluded.last_signal_price,
                    position_side = excluded.position_side,
                    position_entry_price = excluded.position_entry_price,
                    blocked = excluded.blocked,
                    block_reason = excluded.block_reason,
                    last_error = excluded.last_error,
                    state_payload = excluded.state_payload,
                    updated_at = datetime('now')
                ''',
                (
                    runtime_key,
                    runtime_data.get("runtime_name"),
                    runtime_data.get("environment"),
                    symbol,
                    timeframe,
                    runtime_data.get("strategy_version"),
                    runtime_data.get("status", "starting"),
                    runtime_data.get("last_heartbeat_at"),
                    runtime_data.get("last_candle_timestamp"),
                    runtime_data.get("last_signal"),
                    runtime_data.get("last_signal_reason"),
                    runtime_data.get("last_signal_price"),
                    runtime_data.get("position_side"),
                    runtime_data.get("position_entry_price"),
                    int(bool(runtime_data.get("blocked", False))),
                    runtime_data.get("block_reason"),
                    runtime_data.get("last_error"),
                    self._to_json_text(runtime_data.get("state_payload")),
                ),
            )
            conn.commit()
            cursor.execute(
                '''
                SELECT id
                FROM bot_runtime_state
                WHERE runtime_key = ?
                LIMIT 1
                ''',
                (runtime_key,),
            )
            row = cursor.fetchone()
            if isinstance(row, dict):
                return int(row.get("id") or 0)
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def get_bot_runtime_state(self, runtime_key: Optional[str] = None, limit: int = 20) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                '''
                SELECT *
                FROM bot_runtime_state
                WHERE (? IS NULL OR runtime_key = ?)
                ORDER BY updated_at DESC
                LIMIT ?
                ''',
                (runtime_key, runtime_key, int(limit)),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row["blocked"] = bool(row.get("blocked", False))
                row["state_payload"] = self._from_json_text(row.get("state_payload"))
            return rows
        finally:
            conn.close()

    @staticmethod
    def _normalize_dashboard_login_name(login_name: Any) -> str:
        return str(login_name or "").strip().lower()

    @staticmethod
    def _validate_dashboard_password(password: str):
        minimum_length = max(int(ProductionConfig.DASHBOARD_MIN_PASSWORD_LENGTH), 10)
        if len(str(password or "")) < minimum_length:
            raise ValueError(f"A senha da dashboard deve ter pelo menos {minimum_length} caracteres.")

    @classmethod
    def _hash_dashboard_password(cls, password: str, salt_hex: Optional[str] = None) -> Dict[str, str]:
        cls._validate_dashboard_password(password)
        salt_bytes = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt_bytes,
            200000,
        )
        return {
            "salt_hex": salt_bytes.hex(),
            "hash_hex": digest.hex(),
        }

    @classmethod
    def _verify_dashboard_password(cls, password: str, salt_hex: str, stored_hash_hex: str) -> bool:
        if not password or not salt_hex or not stored_hash_hex:
            return False
        derived = cls._hash_dashboard_password(password, salt_hex=salt_hex)
        return hmac.compare_digest(derived["hash_hex"], str(stored_hash_hex))

    @staticmethod
    def _normalize_subscription_plan_code(plan_code: Any) -> str:
        normalized = str(plan_code or "free").strip().lower()
        if normalized in {"weekly", "mensal", "semanal"}:
            return "weekly"
        if normalized in {"monthly", "mes", "mensalidade"}:
            return "monthly"
        if normalized in {"yearly", "annual", "anual"}:
            return "yearly"
        if normalized in {"trial"}:
            return "trial"
        return "free"

    @staticmethod
    def _plan_duration_days(plan_code: str) -> int:
        normalized = str(plan_code or "").strip().lower()
        if normalized == "weekly":
            return 7
        if normalized == "monthly":
            return 30
        if normalized == "yearly":
            return 365
        if normalized == "trial":
            return 7
        return 0

    @staticmethod
    def _to_utc_datetime(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @classmethod
    def _build_subscription_snapshot(
        cls,
        *,
        plan_code: Any,
        status: Any,
        started_at: Any,
        expires_at: Any,
        alert_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_plan = cls._normalize_subscription_plan_code(plan_code)
        normalized_status = str(status or "inactive").strip().lower()
        started_dt = cls._to_utc_datetime(started_at)
        expires_dt = cls._to_utc_datetime(expires_at)
        now_utc = datetime.now(UTC)

        is_active = (
            normalized_status == "active"
            and expires_dt is not None
            and expires_dt > now_utc
        )
        if normalized_status == "active" and not is_active:
            normalized_status = "expired"

        days_remaining = 0
        if expires_dt is not None and expires_dt > now_utc:
            total_seconds = (expires_dt - now_utc).total_seconds()
            days_remaining = int((total_seconds + 86399) // 86400)

        threshold = max(int(alert_days or ProductionConfig.SUBSCRIPTION_EXPIRY_ALERT_DAYS), 1)
        expiring_soon = bool(is_active and days_remaining <= threshold)

        return {
            "plan_code": normalized_plan,
            "status": normalized_status,
            "started_at": started_dt.isoformat() if started_dt else None,
            "expires_at": expires_dt.isoformat() if expires_dt else None,
            "is_active": bool(is_active),
            "days_remaining": int(days_remaining),
            "expiring_soon": bool(expiring_soon),
            "alert_threshold_days": int(threshold),
        }

    def upsert_dashboard_user_access(self, access_data: Dict[str, Any]) -> int:
        user_id = int(access_data["user_id"])
        login_name = self._normalize_dashboard_login_name(access_data.get("login_name"))
        if not login_name:
            raise ValueError("Informe um login_name valido para o acesso da dashboard.")

        password = access_data.get("password")
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM dashboard_user_access
                WHERE user_id = ? OR login_name = ?
                ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                ''',
                (user_id, login_name, user_id),
            )
            existing = cursor.fetchone()

            if existing:
                existing = dict(existing)
                if int(existing["user_id"]) != user_id and self._normalize_dashboard_login_name(existing["login_name"]) == login_name:
                    raise ValueError("Este login_name ja esta em uso por outro usuario.")
                password_salt = str(existing["password_salt"])
                password_hash = str(existing["password_hash"])
            else:
                if not password:
                    raise ValueError("Defina uma senha para criar o acesso da dashboard.")
                password_payload = self._hash_dashboard_password(str(password))
                password_salt = password_payload["salt_hex"]
                password_hash = password_payload["hash_hex"]

            if password:
                password_payload = self._hash_dashboard_password(str(password))
                password_salt = password_payload["salt_hex"]
                password_hash = password_payload["hash_hex"]

            cursor.execute(
                '''
                INSERT INTO dashboard_user_access (
                    user_id, login_name, password_salt, password_hash,
                    is_active, require_password_change, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    login_name = excluded.login_name,
                    password_salt = excluded.password_salt,
                    password_hash = excluded.password_hash,
                    is_active = excluded.is_active,
                    require_password_change = excluded.require_password_change,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (
                    user_id,
                    login_name,
                    password_salt,
                    password_hash,
                    int(bool(access_data.get("is_active", True))),
                    int(bool(access_data.get("require_password_change", False))),
                    access_data.get("notes"),
                ),
            )
            cursor.execute(
                '''
                SELECT id
                FROM dashboard_user_access
                WHERE user_id = ?
                LIMIT 1
                ''',
                (user_id,),
            )
            row = cursor.fetchone()
            cursor.execute(
                '''
                INSERT INTO dashboard_user_subscriptions (
                    user_id, plan_code, status, started_at, expires_at, auto_renew, credits_balance, updated_at
                ) VALUES (?, 'free', 'inactive', NULL, NULL, 0, 0.0, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO NOTHING
                ''',
                (user_id,),
            )
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def _build_dashboard_auth_response(self, payload: Dict[str, Any], *, expires_at_override: Optional[Any] = None) -> Dict[str, Any]:
        subscription_snapshot = self._build_subscription_snapshot(
            plan_code=payload.get("subscription_plan_code"),
            status=payload.get("subscription_status"),
            started_at=payload.get("subscription_started_at"),
            expires_at=payload.get("subscription_expires_at"),
        )
        response = {
            "id": int(payload["id"]),
            "user_id": int(payload["user_id"]),
            "login_name": payload.get("login_name"),
            "is_active": bool(payload.get("is_active")),
            "require_password_change": bool(payload.get("require_password_change")),
            "last_login_at": payload.get("last_login_at"),
            "username": payload.get("telegram_username"),
            "first_name": payload.get("telegram_first_name"),
            "plan": payload.get("telegram_plan"),
            "subscription": subscription_snapshot,
        }
        if expires_at_override is not None:
            response["expires_at"] = self._to_utc_datetime(expires_at_override).isoformat()
        return response

    def authenticate_dashboard_user(self, login_name: str, password: str) -> Optional[Dict[str, Any]]:
        normalized_login = self._normalize_dashboard_login_name(login_name)
        if not normalized_login or not password:
            return None

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    access.*,
                    tu.username AS telegram_username,
                    tu.first_name AS telegram_first_name,
                    tu.plan AS telegram_plan,
                    sub.plan_code AS subscription_plan_code,
                    sub.status AS subscription_status,
                    sub.started_at AS subscription_started_at,
                    sub.expires_at AS subscription_expires_at
                FROM dashboard_user_access access
                LEFT JOIN telegram_users tu
                  ON tu.telegram_id = access.user_id
                LEFT JOIN dashboard_user_subscriptions sub
                  ON sub.user_id = access.user_id
                WHERE lower(access.login_name) = ?
                   OR CAST(access.user_id AS TEXT) = ?
                LIMIT 1
                ''',
                (normalized_login, normalized_login),
            )
            row = cursor.fetchone()
            if not row:
                return None
            payload = dict(row)
            if not bool(payload.get("is_active")):
                return None
            if not self._verify_dashboard_password(
                password=str(password),
                salt_hex=str(payload.get("password_salt") or ""),
                stored_hash_hex=str(payload.get("password_hash") or ""),
            ):
                return None
            last_login_at = datetime.now(UTC).isoformat()
            cursor.execute(
                '''
                UPDATE dashboard_user_access
                SET last_login_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (last_login_at, int(payload["id"])),
            )
            payload["last_login_at"] = last_login_at
            conn.commit()
            return self._build_dashboard_auth_response(payload)
        finally:
            conn.close()

    def create_dashboard_user_session(
        self,
        *,
        user_id: int,
        login_name: str,
        expires_at: Any,
    ) -> str:
        token = secrets.token_urlsafe(32)
        expires_dt = self._to_utc_datetime(expires_at)
        if not expires_dt:
            raise ValueError("expires_at invalido para sessao persistente da dashboard.")

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO dashboard_user_sessions (
                    session_token, user_id, login_name, expires_at, revoked, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
                ''',
                (
                    token,
                    int(user_id),
                    str(login_name),
                    expires_dt.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
            return token
        finally:
            conn.close()

    def authenticate_dashboard_session(self, session_token: str) -> Optional[Dict[str, Any]]:
        normalized_token = str(session_token or "").strip()
        if not normalized_token:
            return None

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    session_row.session_token,
                    session_row.expires_at AS session_expires_at,
                    access.*,
                    tu.username AS telegram_username,
                    tu.first_name AS telegram_first_name,
                    tu.plan AS telegram_plan,
                    sub.plan_code AS subscription_plan_code,
                    sub.status AS subscription_status,
                    sub.started_at AS subscription_started_at,
                    sub.expires_at AS subscription_expires_at
                FROM dashboard_user_sessions session_row
                JOIN dashboard_user_access access
                  ON access.user_id = session_row.user_id
                LEFT JOIN telegram_users tu
                  ON tu.telegram_id = access.user_id
                LEFT JOIN dashboard_user_subscriptions sub
                  ON sub.user_id = access.user_id
                WHERE session_row.session_token = ?
                  AND session_row.revoked = 0
                LIMIT 1
                ''',
                (normalized_token,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            payload = dict(row)
            if not bool(payload.get("is_active")):
                return None

            expires_dt = self._to_utc_datetime(payload.get("session_expires_at"))
            if not expires_dt or expires_dt <= datetime.now(UTC):
                cursor.execute(
                    '''
                    UPDATE dashboard_user_sessions
                    SET revoked = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE session_token = ?
                    ''',
                    (normalized_token,),
                )
                conn.commit()
                return None

            current_seen = datetime.now(UTC).isoformat()
            cursor.execute(
                '''
                UPDATE dashboard_user_sessions
                SET last_seen_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_token = ?
                ''',
                (current_seen, normalized_token),
            )
            conn.commit()
            response = self._build_dashboard_auth_response(payload, expires_at_override=expires_dt)
            response["session_token"] = normalized_token
            return response
        finally:
            conn.close()

    def revoke_dashboard_user_session(self, session_token: str) -> bool:
        normalized_token = str(session_token or "").strip()
        if not normalized_token:
            return False

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE dashboard_user_sessions
                SET revoked = 1, updated_at = CURRENT_TIMESTAMP
                WHERE session_token = ?
                ''',
                (normalized_token,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def change_dashboard_user_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM dashboard_user_access
                WHERE user_id = ?
                LIMIT 1
                ''',
                (int(user_id),),
            )
            row = cursor.fetchone()
            if not row:
                return False
            payload = dict(row)
            if not self._verify_dashboard_password(
                password=str(current_password),
                salt_hex=str(payload.get("password_salt") or ""),
                stored_hash_hex=str(payload.get("password_hash") or ""),
            ):
                return False

            password_payload = self._hash_dashboard_password(str(new_password))
            cursor.execute(
                '''
                UPDATE dashboard_user_access
                SET password_salt = ?,
                    password_hash = ?,
                    require_password_change = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                ''',
                (
                    password_payload["salt_hex"],
                    password_payload["hash_hex"],
                    int(user_id),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def list_dashboard_user_access(self, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    access.id,
                    access.user_id,
                    access.login_name,
                    access.is_active,
                    access.require_password_change,
                    access.notes,
                    access.last_login_at,
                    access.created_at,
                    access.updated_at,
                    tu.username AS telegram_username,
                    tu.first_name AS telegram_first_name,
                    tu.plan AS telegram_plan,
                    COALESCE(sub.plan_code, 'free') AS plan_code,
                    COALESCE(sub.status, 'inactive') AS subscription_status,
                    sub.expires_at AS subscription_expires_at,
                    (
                        SELECT COUNT(*)
                        FROM user_accounts ua
                        WHERE ua.user_id = access.user_id
                    ) AS account_count
                FROM dashboard_user_access access
                LEFT JOIN telegram_users tu
                  ON tu.telegram_id = access.user_id
                LEFT JOIN dashboard_user_subscriptions sub
                  ON sub.user_id = access.user_id
                ORDER BY access.user_id ASC
                LIMIT ?
                ''',
                (int(limit),),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def register_dashboard_user_selfservice(self, signup_data: Dict[str, Any]) -> Dict[str, Any]:
        login_name = self._normalize_dashboard_login_name(signup_data.get("login_name"))
        if not login_name:
            raise ValueError("Informe um login válido.")

        password = str(signup_data.get("password") or "")
        password_payload = self._hash_dashboard_password(password)
        notes = str(signup_data.get("notes") or "").strip() or None
        display_name = str(signup_data.get("display_name") or "").strip() or None
        contact_text = str(signup_data.get("contact_text") or "").strip() or None

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT id
                FROM dashboard_user_access
                WHERE lower(login_name) = ?
                LIMIT 1
                ''',
                (login_name,),
            )
            if cursor.fetchone():
                raise ValueError("Este login já está em uso.")

            user_id = self._next_dashboard_user_id(cursor)
            cursor.execute(
                '''
                INSERT INTO dashboard_user_access (
                    user_id,
                    login_name,
                    password_salt,
                    password_hash,
                    is_active,
                    require_password_change,
                    notes,
                    updated_at
                ) VALUES (?, ?, ?, ?, 1, 0, ?, CURRENT_TIMESTAMP)
                ''',
                (
                    int(user_id),
                    login_name,
                    password_payload["salt_hex"],
                    password_payload["hash_hex"],
                    notes,
                ),
            )
            access_id = int(cursor.lastrowid)

            cursor.execute(
                '''
                INSERT INTO dashboard_user_subscriptions (
                    user_id, plan_code, status, started_at, expires_at, auto_renew, credits_balance, notes, updated_at
                ) VALUES (?, 'free', 'inactive', NULL, NULL, 0, 0.0, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO NOTHING
                ''',
                (int(user_id), notes),
            )

            if display_name:
                cursor.execute(
                    '''
                    INSERT INTO telegram_users (
                        telegram_id, username, first_name, plan, is_admin, joined_date
                    ) VALUES (?, ?, ?, 'free', 0, CURRENT_TIMESTAMP)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        username = COALESCE(excluded.username, telegram_users.username),
                        first_name = COALESCE(excluded.first_name, telegram_users.first_name)
                    ''',
                    (int(user_id), login_name, display_name),
                )
            elif contact_text:
                cursor.execute(
                    '''
                    INSERT INTO telegram_users (
                        telegram_id, username, first_name, plan, is_admin, joined_date
                    ) VALUES (?, ?, ?, 'free', 0, CURRENT_TIMESTAMP)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        username = COALESCE(excluded.username, telegram_users.username)
                    ''',
                    (int(user_id), login_name, contact_text),
                )

            conn.commit()
            return {
                "access_id": access_id,
                "user_id": int(user_id),
                "login_name": login_name,
                "subscription": self._build_subscription_snapshot(
                    plan_code="free",
                    status="inactive",
                    started_at=None,
                    expires_at=None,
                ),
            }
        finally:
            conn.close()

    def get_dashboard_user_subscription(self, user_id: int) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO dashboard_user_subscriptions (
                    user_id, plan_code, status, started_at, expires_at, auto_renew, credits_balance, updated_at
                ) VALUES (?, 'free', 'inactive', NULL, NULL, 0, 0.0, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO NOTHING
                ''',
                (int(user_id),),
            )
            cursor.execute(
                '''
                SELECT *
                FROM dashboard_user_subscriptions
                WHERE user_id = ?
                LIMIT 1
                ''',
                (int(user_id),),
            )
            row = cursor.fetchone()
            conn.commit()
            payload = dict(row) if row else {
                "user_id": int(user_id),
                "plan_code": "free",
                "status": "inactive",
                "started_at": None,
                "expires_at": None,
                "auto_renew": False,
                "credits_balance": 0.0,
                "last_payment_at": None,
                "next_billing_at": None,
                "notes": None,
            }
            snapshot = self._build_subscription_snapshot(
                plan_code=payload.get("plan_code"),
                status=payload.get("status"),
                started_at=payload.get("started_at"),
                expires_at=payload.get("expires_at"),
            )
            return {
                **payload,
                **snapshot,
                "user_id": int(payload.get("user_id", user_id)),
                "auto_renew": bool(payload.get("auto_renew", False)),
                "credits_balance": float(payload.get("credits_balance", 0.0) or 0.0),
            }
        finally:
            conn.close()

    def list_dashboard_user_subscriptions(self, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    sub.user_id,
                    access.login_name,
                    sub.plan_code,
                    sub.status,
                    sub.started_at,
                    sub.expires_at,
                    sub.auto_renew,
                    sub.credits_balance,
                    sub.payment_provider,
                    sub.external_subscription_id,
                    sub.last_payment_at,
                    sub.next_billing_at,
                    sub.notes,
                    sub.updated_at
                FROM dashboard_user_subscriptions sub
                LEFT JOIN dashboard_user_access access
                  ON access.user_id = sub.user_id
                ORDER BY
                    CASE WHEN lower(sub.status) = 'active' THEN 0 ELSE 1 END,
                    sub.expires_at ASC,
                    sub.user_id ASC
                LIMIT ?
                ''',
                (int(limit),),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                snapshot = self._build_subscription_snapshot(
                    plan_code=item.get("plan_code"),
                    status=item.get("status"),
                    started_at=item.get("started_at"),
                    expires_at=item.get("expires_at"),
                )
                rows.append({**item, **snapshot})
            return rows
        finally:
            conn.close()

    def activate_dashboard_user_subscription(
        self,
        *,
        user_id: int,
        plan_code: str,
        approved_by: Optional[str] = None,
        extend_from_current: bool = True,
        auto_renew: bool = False,
        payment_provider: Optional[str] = None,
        external_subscription_id: Optional[str] = None,
        credits_delta: float = 0.0,
        notes: Optional[str] = None,
        started_at: Optional[Any] = None,
        expires_at: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized_plan = self._normalize_subscription_plan_code(plan_code)
        now_utc = datetime.now(UTC)
        explicit_start = self._to_utc_datetime(started_at)
        explicit_expiry = self._to_utc_datetime(expires_at)
        duration_days = self._plan_duration_days(normalized_plan)

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM dashboard_user_access
                WHERE user_id = ?
                LIMIT 1
                ''',
                (int(user_id),),
            )
            if not cursor.fetchone():
                raise ValueError(f"User ID {int(user_id)} não encontrado em dashboard_user_access.")

            cursor.execute(
                '''
                SELECT *
                FROM dashboard_user_subscriptions
                WHERE user_id = ?
                LIMIT 1
                ''',
                (int(user_id),),
            )
            current = cursor.fetchone()
            current_payload = dict(current) if current else {}
            current_expiry = self._to_utc_datetime(current_payload.get("expires_at"))

            base_start = explicit_start or now_utc
            if (
                extend_from_current
                and current_expiry is not None
                and current_expiry > now_utc
                and explicit_start is None
            ):
                base_start = current_expiry

            resolved_expiry = explicit_expiry
            if resolved_expiry is None:
                if duration_days <= 0:
                    resolved_expiry = base_start
                else:
                    resolved_expiry = base_start + timedelta(days=duration_days)

            credits_previous = float(current_payload.get("credits_balance", 0.0) or 0.0)
            credits_new = credits_previous + float(credits_delta or 0.0)
            merged_notes = str(notes or "").strip() or current_payload.get("notes")
            reviewer = str(approved_by or "").strip() or "admin"

            cursor.execute(
                '''
                INSERT INTO dashboard_user_subscriptions (
                    user_id, plan_code, status, started_at, expires_at, auto_renew,
                    payment_provider, external_subscription_id, credits_balance,
                    last_payment_at, next_billing_at, notes, updated_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_code = excluded.plan_code,
                    status = 'active',
                    started_at = excluded.started_at,
                    expires_at = excluded.expires_at,
                    auto_renew = excluded.auto_renew,
                    payment_provider = COALESCE(excluded.payment_provider, dashboard_user_subscriptions.payment_provider),
                    external_subscription_id = COALESCE(excluded.external_subscription_id, dashboard_user_subscriptions.external_subscription_id),
                    credits_balance = excluded.credits_balance,
                    last_payment_at = excluded.last_payment_at,
                    next_billing_at = excluded.next_billing_at,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (
                    int(user_id),
                    normalized_plan,
                    base_start.isoformat(),
                    resolved_expiry.isoformat(),
                    int(bool(auto_renew)),
                    str(payment_provider or "").strip() or None,
                    str(external_subscription_id or "").strip() or None,
                    float(credits_new),
                    now_utc.isoformat(),
                    resolved_expiry.isoformat() if bool(auto_renew) else None,
                    merged_notes if merged_notes else f"Ativado por {reviewer}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return self.get_dashboard_user_subscription(int(user_id))

    def set_dashboard_user_subscription_status(
        self,
        *,
        user_id: int,
        status: str,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"inactive", "active", "expired", "blocked"}:
            raise ValueError("Status de assinatura inválido.")

        current = self.get_dashboard_user_subscription(int(user_id))
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE dashboard_user_subscriptions
                SET status = ?,
                    notes = COALESCE(?, notes),
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                ''',
                (
                    normalized_status,
                    str(notes or "").strip() or None,
                    int(user_id),
                ),
            )
            if normalized_status in {"inactive", "expired", "blocked"}:
                cursor.execute(
                    '''
                    UPDATE dashboard_user_subscriptions
                    SET auto_renew = 0
                    WHERE user_id = ?
                    ''',
                    (int(user_id),),
                )
            conn.commit()
        finally:
            conn.close()

        if normalized_status in {"inactive", "expired", "blocked"}:
            return self.get_dashboard_user_subscription(int(user_id))
        return {
            **current,
            "status": normalized_status,
            "is_active": bool(normalized_status == "active" and current.get("is_active")),
        }

    def create_dashboard_signup_request(self, request_data: Dict[str, Any]) -> int:
        login_name = self._normalize_dashboard_login_name(request_data.get("login_name"))
        if not login_name:
            raise ValueError("Informe um login válido para solicitar cadastro.")

        password = str(request_data.get("password") or "")
        password_payload = self._hash_dashboard_password(password)

        display_name = str(request_data.get("display_name") or "").strip() or None
        contact_text = str(request_data.get("contact_text") or "").strip() or None
        notes = str(request_data.get("notes") or "").strip() or None

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT id
                FROM dashboard_user_access
                WHERE lower(login_name) = ?
                LIMIT 1
                ''',
                (login_name,),
            )
            if cursor.fetchone():
                raise ValueError("Este login já está em uso.")

            cursor.execute(
                '''
                INSERT INTO dashboard_signup_requests (
                    login_name, password_salt, password_hash, display_name, contact_text,
                    status, requested_at, reviewed_at, reviewed_by, review_notes, approved_user_id, notes
                ) VALUES (?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP, NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(login_name) DO UPDATE SET
                    password_salt = excluded.password_salt,
                    password_hash = excluded.password_hash,
                    display_name = excluded.display_name,
                    contact_text = excluded.contact_text,
                    status = 'pending',
                    requested_at = CURRENT_TIMESTAMP,
                    reviewed_at = NULL,
                    reviewed_by = NULL,
                    review_notes = NULL,
                    approved_user_id = NULL,
                    notes = excluded.notes
                ''',
                (
                    login_name,
                    password_payload["salt_hex"],
                    password_payload["hash_hex"],
                    display_name,
                    contact_text,
                    notes,
                ),
            )
            cursor.execute(
                '''
                SELECT id
                FROM dashboard_signup_requests
                WHERE login_name = ?
                LIMIT 1
                ''',
                (login_name,),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def list_dashboard_signup_requests(self, limit: int = 200, status: Optional[str] = None) -> List[Dict[str, Any]]:
        normalized_status = str(status or "").strip().lower() or None
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if normalized_status:
                cursor.execute(
                    '''
                    SELECT
                        req.id,
                        req.login_name,
                        req.display_name,
                        req.contact_text,
                        req.status,
                        req.requested_at,
                        req.reviewed_at,
                        req.reviewed_by,
                        req.review_notes,
                        req.approved_user_id,
                        req.notes
                    FROM dashboard_signup_requests req
                    WHERE lower(req.status) = ?
                    ORDER BY
                        CASE WHEN lower(req.status) = 'pending' THEN 0 ELSE 1 END,
                        req.requested_at DESC,
                        req.id DESC
                    LIMIT ?
                    ''',
                    (normalized_status, int(limit)),
                )
            else:
                cursor.execute(
                    '''
                    SELECT
                        req.id,
                        req.login_name,
                        req.display_name,
                        req.contact_text,
                        req.status,
                        req.requested_at,
                        req.reviewed_at,
                        req.reviewed_by,
                        req.review_notes,
                        req.approved_user_id,
                        req.notes
                    FROM dashboard_signup_requests req
                    ORDER BY
                        CASE WHEN lower(req.status) = 'pending' THEN 0 ELSE 1 END,
                        req.requested_at DESC,
                        req.id DESC
                    LIMIT ?
                    ''',
                    (int(limit),),
                )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _next_dashboard_user_id(self, cursor) -> int:
        cursor.execute(
            '''
            SELECT MAX(value) AS max_user_id
            FROM (
                SELECT COALESCE(MAX(user_id), 0) AS value FROM dashboard_user_access
                UNION ALL
                SELECT COALESCE(MAX(telegram_id), 0) AS value FROM telegram_users
                UNION ALL
                SELECT COALESCE(MAX(user_id), 0) AS value FROM user_accounts
            )
            '''
        )
        row = cursor.fetchone()
        max_user_id = int((row or {"max_user_id": 0})["max_user_id"] or 0)
        return max(max_user_id + 1, 1000)

    def review_dashboard_signup_request(
        self,
        *,
        request_id: int,
        action: str,
        reviewed_by: Optional[str] = None,
        review_notes: Optional[str] = None,
        approved_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"approve", "reject"}:
            raise ValueError("Ação inválida para revisão. Use approve ou reject.")

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM dashboard_signup_requests
                WHERE id = ?
                LIMIT 1
                ''',
                (int(request_id),),
            )
            request_row = cursor.fetchone()
            if not request_row:
                raise ValueError("Solicitação de cadastro não encontrada.")

            request_payload = dict(request_row)
            current_status = str(request_payload.get("status") or "").strip().lower()
            if current_status != "pending":
                raise ValueError(f"Solicitação já revisada com status {current_status}.")

            reviewer = str(reviewed_by or "").strip() or "admin"
            notes = str(review_notes or "").strip() or None
            reviewed_at = datetime.now(UTC).isoformat()

            if normalized_action == "reject":
                cursor.execute(
                    '''
                    UPDATE dashboard_signup_requests
                    SET status = 'rejected',
                        reviewed_at = ?,
                        reviewed_by = ?,
                        review_notes = ?,
                        approved_user_id = NULL
                    WHERE id = ?
                    ''',
                    (reviewed_at, reviewer, notes, int(request_id)),
                )
                conn.commit()
                return {
                    "request_id": int(request_id),
                    "status": "rejected",
                    "login_name": request_payload.get("login_name"),
                    "approved_user_id": None,
                }

            login_name = self._normalize_dashboard_login_name(request_payload.get("login_name"))
            cursor.execute(
                '''
                SELECT *
                FROM dashboard_user_access
                WHERE lower(login_name) = ?
                LIMIT 1
                ''',
                (login_name,),
            )
            existing_access = cursor.fetchone()

            if existing_access:
                access_payload = dict(existing_access)
                target_user_id = int(access_payload["user_id"])
                cursor.execute(
                    '''
                    UPDATE dashboard_user_access
                    SET password_salt = ?,
                        password_hash = ?,
                        is_active = 1,
                        require_password_change = 1,
                        notes = COALESCE(?, notes),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    ''',
                    (
                        str(request_payload["password_salt"]),
                        str(request_payload["password_hash"]),
                        notes,
                        target_user_id,
                    ),
                )
            else:
                if approved_user_id is not None and int(approved_user_id) > 0:
                    target_user_id = int(approved_user_id)
                    cursor.execute(
                        '''
                        SELECT id
                        FROM dashboard_user_access
                        WHERE user_id = ?
                        LIMIT 1
                        ''',
                        (target_user_id,),
                    )
                    if cursor.fetchone():
                        raise ValueError(f"User ID {target_user_id} já está em uso.")
                else:
                    target_user_id = self._next_dashboard_user_id(cursor)

                cursor.execute(
                    '''
                    INSERT INTO dashboard_user_access (
                        user_id,
                        login_name,
                        password_salt,
                        password_hash,
                        is_active,
                        require_password_change,
                        notes,
                        updated_at
                    ) VALUES (?, ?, ?, ?, 1, 1, ?, CURRENT_TIMESTAMP)
                    ''',
                    (
                        target_user_id,
                        login_name,
                        str(request_payload["password_salt"]),
                        str(request_payload["password_hash"]),
                        notes,
                    ),
                )

            cursor.execute(
                '''
                UPDATE dashboard_signup_requests
                SET status = 'approved',
                    reviewed_at = ?,
                    reviewed_by = ?,
                    review_notes = ?,
                    approved_user_id = ?
                WHERE id = ?
                ''',
                (reviewed_at, reviewer, notes, int(target_user_id), int(request_id)),
            )
            cursor.execute(
                '''
                INSERT INTO dashboard_user_subscriptions (
                    user_id, plan_code, status, started_at, expires_at, auto_renew, credits_balance, updated_at
                ) VALUES (?, 'free', 'inactive', NULL, NULL, 0, 0.0, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO NOTHING
                ''',
                (int(target_user_id),),
            )

            conn.commit()
            return {
                "request_id": int(request_id),
                "status": "approved",
                "login_name": login_name,
                "approved_user_id": int(target_user_id),
            }
        finally:
            conn.close()

    def get_user_workspace_accounts(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    a.user_id,
                    a.account_id,
                    a.account_alias,
                    a.exchange,
                    a.status,
                    a.live_enabled,
                    a.paper_enabled,
                    a.capital_base,
                    COALESCE(rp.risk_mode, a.risk_mode) AS risk_mode,
                    COALESCE(rp.allowed_position_count, 0) AS allowed_position_count,
                    COALESCE(rp.max_risk_per_trade, 0.0) AS max_risk_per_trade,
                    COALESCE(rp.max_daily_loss, 0.0) AS max_daily_loss,
                    COALESCE(rp.max_drawdown, 0.0) AS max_drawdown,
                    COALESCE(rp.max_portfolio_open_risk_pct, 0.0) AS max_portfolio_open_risk_pct,
                    COALESCE(rp.leverage_cap, 0.0) AS leverage_cap,
                    COALESCE(rp.is_valid, 0) AS risk_profile_valid,
                    COALESCE(cred.permission_status, 'unknown') AS permission_status,
                    COALESCE(cred.token_status, 'unknown') AS token_status,
                    COALESCE(cred.reconciliation_status, 'unknown') AS reconciliation_status,
                    cred.api_key_ref,
                    cred.token_ref,
                    (
                        SELECT COUNT(*)
                        FROM user_live_positions lp
                        WHERE lp.user_id = a.user_id
                          AND lp.account_id = a.account_id
                          AND lower(lp.status) = 'open'
                    ) AS open_positions,
                    (
                        SELECT COUNT(*)
                        FROM user_live_orders lo
                        WHERE lo.user_id = a.user_id
                          AND lo.account_id = a.account_id
                          AND lower(lo.status) IN ('pending', 'open', 'new')
                    ) AS pending_orders
                FROM user_accounts a
                LEFT JOIN user_risk_profiles rp
                  ON rp.user_id = a.user_id AND rp.account_id = a.account_id
                LEFT JOIN user_exchange_credentials cred
                  ON cred.user_id = a.user_id AND cred.account_id = a.account_id AND cred.exchange = a.exchange
                WHERE a.user_id = ?
                ORDER BY CASE WHEN lower(a.status) = 'active' THEN 0 ELSE 1 END, a.account_id ASC
                LIMIT ?
                ''',
                (int(user_id), int(limit)),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                base_account = self.get_user_accounts(
                    user_id=int(item["user_id"]),
                    account_id=str(item["account_id"]),
                    status=None,
                )
                if base_account:
                    item["allowed_symbols"] = self._to_list(base_account[0].get("allowed_symbols"))
                    item["allowed_timeframes"] = self._to_list(base_account[0].get("allowed_timeframes"))
                    item["notes"] = base_account[0].get("notes")
                else:
                    item["allowed_symbols"] = []
                    item["allowed_timeframes"] = []
                    item["notes"] = None
                rows.append(item)
            return rows
        finally:
            conn.close()

    def upsert_user_account(self, account_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            user_id = int(account_data["user_id"])
            account_id = str(account_data["account_id"])
            exchange = str(account_data["exchange"])
            allowed_symbols = self._to_json_text(account_data.get("allowed_symbols"))
            allowed_timeframes = self._to_json_text(account_data.get("allowed_timeframes"))

            cursor.execute(
                '''
                INSERT INTO user_accounts (
                    user_id, account_id, account_alias, exchange, status,
                    live_enabled, paper_enabled, capital_base, risk_mode,
                    allowed_symbols, allowed_timeframes, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, account_id) DO UPDATE SET
                    account_alias = excluded.account_alias,
                    exchange = excluded.exchange,
                    status = excluded.status,
                    live_enabled = excluded.live_enabled,
                    paper_enabled = excluded.paper_enabled,
                    capital_base = excluded.capital_base,
                    risk_mode = excluded.risk_mode,
                    allowed_symbols = excluded.allowed_symbols,
                    allowed_timeframes = excluded.allowed_timeframes,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (
                    user_id,
                    account_id,
                    account_data.get("account_alias"),
                    exchange,
                    account_data.get("status", "active"),
                    int(bool(account_data.get("live_enabled", False))),
                    int(bool(account_data.get("paper_enabled", True))),
                    float(account_data.get("capital_base", 0.0) or 0.0),
                    account_data.get("risk_mode", "normal"),
                    allowed_symbols,
                    allowed_timeframes,
                    account_data.get("notes"),
                ),
            )

            cursor.execute(
                '''
                SELECT id FROM user_accounts
                WHERE user_id = ? AND account_id = ?
                LIMIT 1
                ''',
                (user_id, account_id),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def get_user_accounts(
        self,
        user_id: Optional[int] = None,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_accounts
                WHERE (? IS NULL OR user_id = ?)
                  AND (? IS NULL OR account_id = ?)
                  AND (? IS NULL OR status = ?)
                ORDER BY user_id ASC, account_id ASC
                ''',
                (
                    user_id,
                    user_id,
                    account_id,
                    account_id,
                    status,
                    status,
                ),
            )
            rows = cursor.fetchall()
            accounts: List[Dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["allowed_symbols"] = self._to_list(item.get("allowed_symbols"))
                item["allowed_timeframes"] = self._to_list(item.get("allowed_timeframes"))
                accounts.append(item)
            return accounts
        finally:
            conn.close()

    def set_user_account_operational_state(
        self,
        *,
        user_id: int,
        account_id: str,
        live_enabled: Optional[bool] = None,
        paper_enabled: Optional[bool] = None,
        status: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        updates = []
        params: List[Any] = []
        if live_enabled is not None:
            updates.append("live_enabled = ?")
            params.append(int(bool(live_enabled)))
        if paper_enabled is not None:
            updates.append("paper_enabled = ?")
            params.append(int(bool(paper_enabled)))
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([int(user_id), str(account_id)])

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f'''
                UPDATE user_accounts
                SET {", ".join(updates)}
                WHERE user_id = ? AND account_id = ?
                ''',
                tuple(params),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def upsert_user_risk_profile(self, profile_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            user_id = int(profile_data["user_id"])
            account_id = str(profile_data["account_id"])
            preferred_symbols = self._to_json_text(profile_data.get("preferred_symbols"))
            cursor.execute(
                '''
                INSERT INTO user_risk_profiles (
                    user_id, account_id,
                    max_risk_per_trade, max_daily_loss, max_drawdown, max_portfolio_open_risk_pct,
                    allowed_position_count, preferred_symbols, leverage_cap, risk_mode,
                    live_enabled, paper_enabled, is_valid, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, account_id) DO UPDATE SET
                    max_risk_per_trade = excluded.max_risk_per_trade,
                    max_daily_loss = excluded.max_daily_loss,
                    max_drawdown = excluded.max_drawdown,
                    max_portfolio_open_risk_pct = excluded.max_portfolio_open_risk_pct,
                    allowed_position_count = excluded.allowed_position_count,
                    preferred_symbols = excluded.preferred_symbols,
                    leverage_cap = excluded.leverage_cap,
                    risk_mode = excluded.risk_mode,
                    live_enabled = excluded.live_enabled,
                    paper_enabled = excluded.paper_enabled,
                    is_valid = excluded.is_valid,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (
                    user_id,
                    account_id,
                    float(profile_data.get("max_risk_per_trade", 0.0) or 0.0),
                    float(profile_data.get("max_daily_loss", 0.0) or 0.0),
                    float(profile_data.get("max_drawdown", 0.0) or 0.0),
                    float(profile_data.get("max_portfolio_open_risk_pct", 0.0) or 0.0),
                    int(profile_data.get("allowed_position_count", 0) or 0),
                    preferred_symbols,
                    float(profile_data.get("leverage_cap", 0.0) or 0.0),
                    profile_data.get("risk_mode", "normal"),
                    int(bool(profile_data.get("live_enabled", False))),
                    int(bool(profile_data.get("paper_enabled", True))),
                    int(bool(profile_data.get("is_valid", True))),
                    profile_data.get("notes"),
                ),
            )
            cursor.execute(
                '''
                SELECT id FROM user_risk_profiles
                WHERE user_id = ? AND account_id = ?
                LIMIT 1
                ''',
                (user_id, account_id),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def get_user_risk_profile(self, user_id: int, account_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_risk_profiles
                WHERE user_id = ? AND account_id = ?
                LIMIT 1
                ''',
                (int(user_id), str(account_id)),
            )
            row = cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            item["preferred_symbols"] = self._to_list(item.get("preferred_symbols"))
            return item
        finally:
            conn.close()

    def upsert_user_exchange_credential(self, credential_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            user_id = int(credential_data["user_id"])
            account_id = str(credential_data["account_id"])
            exchange = str(credential_data["exchange"])
            cursor.execute(
                '''
                INSERT INTO user_exchange_credentials (
                    user_id, account_id, exchange, credential_alias, api_key_ref, token_ref,
                    encrypted_api_key, encrypted_api_secret,
                    permissions_read, permissions_trade, permissions_withdraw,
                    permission_status, token_status, reconciliation_status,
                    last_validated_at, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, account_id, exchange) DO UPDATE SET
                    credential_alias = excluded.credential_alias,
                    api_key_ref = excluded.api_key_ref,
                    token_ref = excluded.token_ref,
                    encrypted_api_key = excluded.encrypted_api_key,
                    encrypted_api_secret = excluded.encrypted_api_secret,
                    permissions_read = excluded.permissions_read,
                    permissions_trade = excluded.permissions_trade,
                    permissions_withdraw = excluded.permissions_withdraw,
                    permission_status = excluded.permission_status,
                    token_status = excluded.token_status,
                    reconciliation_status = excluded.reconciliation_status,
                    last_validated_at = excluded.last_validated_at,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (
                    user_id,
                    account_id,
                    exchange,
                    credential_data.get("credential_alias"),
                    credential_data.get("api_key_ref"),
                    credential_data.get("token_ref"),
                    credential_data["encrypted_api_key"],
                    credential_data["encrypted_api_secret"],
                    int(bool(credential_data.get("permissions_read", True))),
                    int(bool(credential_data.get("permissions_trade", True))),
                    int(bool(credential_data.get("permissions_withdraw", False))),
                    credential_data.get("permission_status", "unknown"),
                    credential_data.get("token_status", "unknown"),
                    credential_data.get("reconciliation_status", "unknown"),
                    credential_data.get("last_validated_at"),
                    credential_data.get("notes"),
                ),
            )
            cursor.execute(
                '''
                SELECT id
                FROM user_exchange_credentials
                WHERE user_id = ? AND account_id = ? AND exchange = ?
                LIMIT 1
                ''',
                (user_id, account_id, exchange),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def get_user_exchange_credential(
        self,
        *,
        user_id: int,
        account_id: str,
        exchange: str,
        include_encrypted: bool = False,
    ) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_exchange_credentials
                WHERE user_id = ? AND account_id = ? AND exchange = ?
                LIMIT 1
                ''',
                (int(user_id), str(account_id), str(exchange)),
            )
            row = cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            if not include_encrypted:
                item.pop("encrypted_api_key", None)
                item.pop("encrypted_api_secret", None)
            return item
        finally:
            conn.close()

    def update_user_exchange_credential_status(
        self,
        *,
        user_id: int,
        account_id: str,
        exchange: str,
        permission_status: Optional[str] = None,
        token_status: Optional[str] = None,
        reconciliation_status: Optional[str] = None,
        last_validated_at: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        updates = []
        params: List[Any] = []

        if permission_status is not None:
            updates.append("permission_status = ?")
            params.append(str(permission_status))
        if token_status is not None:
            updates.append("token_status = ?")
            params.append(str(token_status))
        if reconciliation_status is not None:
            updates.append("reconciliation_status = ?")
            params.append(str(reconciliation_status))
        if last_validated_at is not None:
            updates.append("last_validated_at = ?")
            params.append(str(last_validated_at))
        if notes is not None:
            updates.append("notes = ?")
            params.append(str(notes))

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([int(user_id), str(account_id), str(exchange)])

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f'''
                UPDATE user_exchange_credentials
                SET {', '.join(updates)}
                WHERE user_id = ? AND account_id = ? AND exchange = ?
                ''',
                tuple(params),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def upsert_user_governance_state(self, governance_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            user_id = int(governance_data["user_id"])
            account_id = str(governance_data["account_id"])
            exchange = str(governance_data.get("exchange") or "")
            symbol = str(governance_data.get("symbol") or "")
            timeframe = str(governance_data.get("timeframe") or "")
            strategy_version = str(governance_data.get("strategy_version") or "")

            cursor.execute(
                '''
                INSERT INTO user_governance_state (
                    user_id, account_id, exchange, symbol, timeframe, strategy_version,
                    governance_status, governance_mode, blocked, block_reason, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, account_id, exchange, symbol, timeframe, strategy_version) DO UPDATE SET
                    governance_status = excluded.governance_status,
                    governance_mode = excluded.governance_mode,
                    blocked = excluded.blocked,
                    block_reason = excluded.block_reason,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (
                    user_id,
                    account_id,
                    exchange,
                    symbol,
                    timeframe,
                    strategy_version,
                    governance_data.get("governance_status", "unknown"),
                    governance_data.get("governance_mode", "blocked"),
                    int(bool(governance_data.get("blocked", False))),
                    governance_data.get("block_reason"),
                    governance_data.get("notes"),
                ),
            )
            cursor.execute(
                '''
                SELECT id FROM user_governance_state
                WHERE user_id = ? AND account_id = ? AND exchange = ? AND symbol = ? AND timeframe = ? AND strategy_version = ?
                LIMIT 1
                ''',
                (user_id, account_id, exchange, symbol, timeframe, strategy_version),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def get_user_governance_state(
        self,
        *,
        user_id: int,
        account_id: str,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy_version: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_governance_state
                WHERE user_id = ?
                  AND account_id = ?
                  AND (? IS NULL OR exchange = ?)
                  AND (? IS NULL OR symbol = ?)
                  AND (? IS NULL OR timeframe = ?)
                  AND (? IS NULL OR strategy_version = ?)
                ORDER BY updated_at DESC
                LIMIT 1
                ''',
                (
                    int(user_id),
                    str(account_id),
                    exchange,
                    exchange,
                    symbol,
                    symbol,
                    timeframe,
                    timeframe,
                    strategy_version,
                    strategy_version,
                ),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_user_live_order(self, order_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO user_live_orders (
                    user_id, account_id, exchange, symbol, timeframe, strategy_version,
                    client_order_id, exchange_order_id, side, order_type, quantity, price, status, source, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''',
                (
                    int(order_data["user_id"]),
                    str(order_data["account_id"]),
                    order_data.get("exchange"),
                    order_data.get("symbol"),
                    order_data.get("timeframe"),
                    order_data.get("strategy_version"),
                    order_data.get("client_order_id"),
                    order_data.get("exchange_order_id"),
                    order_data.get("side"),
                    order_data.get("order_type"),
                    float(order_data.get("quantity", 0.0) or 0.0),
                    float(order_data.get("price", 0.0) or 0.0),
                    order_data.get("status", "pending"),
                    order_data.get("source"),
                    order_data.get("notes"),
                ),
            )
            order_id = cursor.lastrowid
            conn.commit()
            return int(order_id)
        finally:
            conn.close()

    def upsert_user_live_order(self, order_data: Dict[str, Any]) -> int:
        user_id = int(order_data["user_id"])
        account_id = str(order_data["account_id"])
        exchange_order_id = str(order_data.get("exchange_order_id") or "").strip()
        client_order_id = str(order_data.get("client_order_id") or "").strip()
        if not exchange_order_id and not client_order_id:
            raise ValueError("exchange_order_id ou client_order_id e obrigatorio para upsert de ordem.")

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            existing_row = None
            if exchange_order_id:
                cursor.execute(
                    '''
                    SELECT id
                    FROM user_live_orders
                    WHERE user_id = ? AND account_id = ? AND exchange_order_id = ?
                    LIMIT 1
                    ''',
                    (user_id, account_id, exchange_order_id),
                )
                existing_row = cursor.fetchone()
            if existing_row is None and client_order_id:
                cursor.execute(
                    '''
                    SELECT id
                    FROM user_live_orders
                    WHERE user_id = ? AND account_id = ? AND client_order_id = ?
                    LIMIT 1
                    ''',
                    (user_id, account_id, client_order_id),
                )
                existing_row = cursor.fetchone()

            payload = (
                order_data.get("exchange"),
                order_data.get("symbol"),
                order_data.get("timeframe"),
                order_data.get("strategy_version"),
                client_order_id or None,
                exchange_order_id or None,
                order_data.get("side"),
                order_data.get("order_type"),
                float(order_data.get("quantity", 0.0) or 0.0),
                float(order_data.get("price", 0.0) or 0.0),
                order_data.get("status", "pending"),
                order_data.get("source"),
                order_data.get("notes"),
            )

            if existing_row:
                row_id = int(existing_row["id"] if isinstance(existing_row, dict) else existing_row[0])
                cursor.execute(
                    '''
                    UPDATE user_live_orders
                    SET exchange = ?, symbol = ?, timeframe = ?, strategy_version = ?,
                        client_order_id = ?, exchange_order_id = ?, side = ?, order_type = ?,
                        quantity = ?, price = ?, status = ?, source = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    payload + (row_id,),
                )
                conn.commit()
                return row_id

            cursor.execute(
                '''
                INSERT INTO user_live_orders (
                    user_id, account_id, exchange, symbol, timeframe, strategy_version,
                    client_order_id, exchange_order_id, side, order_type, quantity, price, status, source, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''',
                (
                    user_id,
                    account_id,
                    *payload,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            conn.commit()
            return row_id
        finally:
            conn.close()

    def create_user_live_position(self, position_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO user_live_positions (
                    user_id, account_id, exchange, symbol, timeframe, strategy_version,
                    side, quantity, entry_price, mark_price, unrealized_pnl, status, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''',
                (
                    int(position_data["user_id"]),
                    str(position_data["account_id"]),
                    position_data.get("exchange"),
                    position_data.get("symbol"),
                    position_data.get("timeframe"),
                    position_data.get("strategy_version"),
                    position_data.get("side"),
                    float(position_data.get("quantity", 0.0) or 0.0),
                    float(position_data.get("entry_price", 0.0) or 0.0),
                    float(position_data.get("mark_price", 0.0) or 0.0),
                    float(position_data.get("unrealized_pnl", 0.0) or 0.0),
                    position_data.get("status", "open"),
                    position_data.get("notes"),
                ),
            )
            position_id = cursor.lastrowid
            conn.commit()
            return int(position_id)
        finally:
            conn.close()

    def sync_user_live_positions_snapshot(
        self,
        *,
        user_id: int,
        account_id: str,
        exchange: str,
        symbol: Optional[str],
        timeframe: Optional[str],
        strategy_version: Optional[str],
        positions: List[Dict[str, Any]],
    ) -> List[int]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_live_positions
                WHERE user_id = ?
                  AND account_id = ?
                  AND (? IS NULL OR exchange = ?)
                  AND (? IS NULL OR symbol = ?)
                  AND status = 'open'
                ''',
                (int(user_id), str(account_id), exchange, exchange, symbol, symbol),
            )
            existing_rows = [dict(row) for row in cursor.fetchall()]
            existing_map = {
                (
                    str(row.get("symbol") or ""),
                    str(row.get("timeframe") or ""),
                    str(row.get("strategy_version") or ""),
                    str(row.get("side") or ""),
                ): row
                for row in existing_rows
            }

            active_keys = set()
            persisted_ids: List[int] = []
            for position in positions:
                position_key = (
                    str(position.get("symbol") or symbol or ""),
                    str(position.get("timeframe") or timeframe or ""),
                    str(position.get("strategy_version") or strategy_version or ""),
                    str(position.get("side") or ""),
                )
                active_keys.add(position_key)
                existing_row = existing_map.get(position_key)
                payload = (
                    exchange,
                    position.get("symbol") or symbol,
                    position.get("timeframe") or timeframe,
                    position.get("strategy_version") or strategy_version,
                    position.get("side"),
                    float(position.get("quantity", 0.0) or 0.0),
                    float(position.get("entry_price", 0.0) or 0.0),
                    float(position.get("mark_price", 0.0) or 0.0),
                    float(position.get("unrealized_pnl", 0.0) or 0.0),
                    position.get("status", "open"),
                    position.get("notes"),
                )

                if existing_row:
                    row_id = int(existing_row["id"])
                    cursor.execute(
                        '''
                        UPDATE user_live_positions
                        SET exchange = ?, symbol = ?, timeframe = ?, strategy_version = ?, side = ?,
                            quantity = ?, entry_price = ?, mark_price = ?, unrealized_pnl = ?,
                            status = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        ''',
                        payload + (row_id,),
                    )
                    persisted_ids.append(row_id)
                else:
                    cursor.execute(
                        '''
                        INSERT INTO user_live_positions (
                            user_id, account_id, exchange, symbol, timeframe, strategy_version,
                            side, quantity, entry_price, mark_price, unrealized_pnl, status, notes, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ''',
                        (
                            int(user_id),
                            str(account_id),
                            *payload,
                        ),
                    )
                    persisted_ids.append(int(cursor.lastrowid or 0))

            for existing_key, existing_row in existing_map.items():
                if existing_key in active_keys:
                    continue
                cursor.execute(
                    '''
                    UPDATE user_live_positions
                    SET status = 'closed', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (int(existing_row["id"]),),
                )

            conn.commit()
            return persisted_ids
        finally:
            conn.close()

    def sync_user_live_orders_snapshot(
        self,
        *,
        user_id: int,
        account_id: str,
        exchange: str,
        symbol: Optional[str],
        timeframe: Optional[str],
        strategy_version: Optional[str],
        open_orders: List[Dict[str, Any]],
        absent_status: str = "closed",
    ) -> List[int]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_live_orders
                WHERE user_id = ?
                  AND account_id = ?
                  AND (? IS NULL OR exchange = ?)
                  AND (? IS NULL OR symbol = ?)
                  AND status IN ('pending', 'open', 'new', 'partially_filled')
                ''',
                (int(user_id), str(account_id), exchange, exchange, symbol, symbol),
            )
            existing_rows = [dict(row) for row in cursor.fetchall()]
            existing_map = {}
            for row in existing_rows:
                order_key = str(row.get("exchange_order_id") or row.get("client_order_id") or row.get("id"))
                existing_map[order_key] = row

            active_keys = set()
            persisted_ids: List[int] = []
            for order in open_orders:
                order_id_key = str(order.get("exchange_order_id") or order.get("client_order_id") or "").strip()
                if not order_id_key:
                    continue
                active_keys.add(order_id_key)
                persisted_ids.append(
                    self.upsert_user_live_order(
                        {
                            "user_id": int(user_id),
                            "account_id": str(account_id),
                            "exchange": exchange,
                            "symbol": order.get("symbol") or symbol,
                            "timeframe": order.get("timeframe") or timeframe,
                            "strategy_version": order.get("strategy_version") or strategy_version,
                            "client_order_id": order.get("client_order_id"),
                            "exchange_order_id": order.get("exchange_order_id"),
                            "side": order.get("side"),
                            "order_type": order.get("order_type"),
                            "quantity": order.get("quantity"),
                            "price": order.get("price"),
                            "status": order.get("status", "open"),
                            "source": order.get("source"),
                            "notes": order.get("notes"),
                        }
                    )
                )

            stale_ids = [
                int(row["id"])
                for order_key, row in existing_map.items()
                if order_key not in active_keys
            ]
            for row_id in stale_ids:
                cursor.execute(
                    '''
                    UPDATE user_live_orders
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (str(absent_status), row_id),
                )

            conn.commit()
            return persisted_ids
        finally:
            conn.close()

    def get_user_live_orders(
        self,
        user_id: Optional[int] = None,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_live_orders
                WHERE (? IS NULL OR user_id = ?)
                  AND (? IS NULL OR account_id = ?)
                  AND (? IS NULL OR status = ?)
                ORDER BY created_at DESC
                ''',
                (user_id, user_id, account_id, account_id, status, status),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_user_live_positions(
        self,
        user_id: Optional[int] = None,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_live_positions
                WHERE (? IS NULL OR user_id = ?)
                  AND (? IS NULL OR account_id = ?)
                  AND (? IS NULL OR status = ?)
                ORDER BY created_at DESC
                ''',
                (user_id, user_id, account_id, account_id, status, status),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def save_user_execution_event(self, event_data: Dict[str, Any]) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            details_json = self._to_json_text(event_data.get("details_json"))
            cursor.execute(
                '''
                INSERT INTO user_execution_events (
                    user_id, account_id, exchange, symbol, timeframe, strategy_version,
                    event_type, event_status, message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    int(event_data["user_id"]),
                    str(event_data["account_id"]),
                    event_data.get("exchange"),
                    event_data.get("symbol"),
                    event_data.get("timeframe"),
                    event_data.get("strategy_version"),
                    event_data.get("event_type"),
                    event_data.get("event_status"),
                    event_data.get("message"),
                    details_json,
                ),
            )
            event_id = cursor.lastrowid
            conn.commit()
            return int(event_id)
        finally:
            conn.close()

    def get_user_execution_events(
        self,
        user_id: Optional[int] = None,
        account_id: Optional[str] = None,
        event_status: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM user_execution_events
                WHERE (? IS NULL OR user_id = ?)
                  AND (? IS NULL OR account_id = ?)
                  AND (? IS NULL OR event_status = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                ''',
                (
                    user_id,
                    user_id,
                    account_id,
                    account_id,
                    event_status,
                    event_status,
                    int(limit),
                ),
            )
            events = []
            for row in cursor.fetchall():
                item = dict(row)
                item["details_json"] = self._decode_json_field(item.get("details_json"), {})
                events.append(item)
            return events
        finally:
            conn.close()

    def list_eligible_accounts_for_runtime(
        self,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy_version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    a.user_id,
                    a.account_id,
                    a.account_alias,
                    a.exchange,
                    a.status,
                    a.live_enabled,
                    a.paper_enabled,
                    a.capital_base,
                    a.risk_mode,
                    a.allowed_symbols,
                    a.allowed_timeframes,
                    a.notes,
                    rp.max_risk_per_trade,
                    rp.max_daily_loss,
                    rp.max_drawdown,
                    rp.max_portfolio_open_risk_pct,
                    rp.allowed_position_count,
                    rp.preferred_symbols,
                    rp.leverage_cap,
                    rp.live_enabled AS risk_live_enabled,
                    rp.paper_enabled AS risk_paper_enabled,
                    rp.is_valid AS risk_profile_valid,
                    rp.risk_mode AS risk_profile_mode,
                    cred.api_key_ref,
                    cred.token_ref,
                    cred.permission_status,
                    cred.token_status,
                    cred.reconciliation_status
                FROM user_accounts a
                LEFT JOIN user_risk_profiles rp
                    ON rp.user_id = a.user_id AND rp.account_id = a.account_id
                LEFT JOIN user_exchange_credentials cred
                    ON cred.user_id = a.user_id AND cred.account_id = a.account_id AND cred.exchange = a.exchange
                WHERE a.status = 'active'
                  AND a.live_enabled = 1
                ORDER BY a.user_id ASC, a.account_id ASC
                '''
            )
            rows = cursor.fetchall()
            contexts = []
            for row in rows:
                item = dict(row)
                context = self.build_account_execution_context(
                    user_id=int(item["user_id"]),
                    account_id=str(item["account_id"]),
                    exchange=item.get("exchange"),
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_version=strategy_version,
                    preload_row=item,
                )
                contexts.append(context)
            return contexts
        finally:
            conn.close()

    def build_account_execution_context(
        self,
        *,
        user_id: int,
        account_id: str,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy_version: Optional[str] = None,
        preload_row: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        account_rows = self.get_user_accounts(user_id=user_id, account_id=account_id, status="active")
        if not account_rows:
            raise ValueError("Conta nao encontrada ou inativa.")
        account = account_rows[0]
        resolved_exchange = exchange or account.get("exchange")

        risk_profile = self.get_user_risk_profile(user_id=user_id, account_id=account_id) or {}
        credential = self.get_user_exchange_credential(
            user_id=user_id,
            account_id=account_id,
            exchange=str(resolved_exchange),
            include_encrypted=False,
        ) or {}

        governance_state = self.get_user_governance_state(
            user_id=user_id,
            account_id=account_id,
            exchange=resolved_exchange,
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        ) or {}

        if preload_row:
            credential = {
                **credential,
                "api_key_ref": preload_row.get("api_key_ref", credential.get("api_key_ref")),
                "token_ref": preload_row.get("token_ref", credential.get("token_ref")),
                "permission_status": preload_row.get("permission_status", credential.get("permission_status")),
                "token_status": preload_row.get("token_status", credential.get("token_status")),
                "reconciliation_status": preload_row.get(
                    "reconciliation_status",
                    credential.get("reconciliation_status"),
                ),
            }

        return {
            "user_id": int(user_id),
            "account_id": str(account_id),
            "account_alias": account.get("account_alias") or str(account_id),
            "exchange_name": resolved_exchange,
            "api_key_ref": credential.get("api_key_ref"),
            "token_ref": credential.get("token_ref"),
            "live_enabled": bool(account.get("live_enabled")),
            "paper_enabled": bool(account.get("paper_enabled")),
            "governance_status": governance_state.get("governance_status") or "unknown",
            "governance_mode": governance_state.get("governance_mode") or "blocked",
            "governance_blocked": bool(governance_state.get("blocked", False)),
            "governance_block_reason": governance_state.get("block_reason"),
            "risk_profile": risk_profile,
            "allowed_symbols": self._to_list(account.get("allowed_symbols")),
            "allowed_timeframes": self._to_list(account.get("allowed_timeframes")),
            "capital_base": float(account.get("capital_base", 0.0) or 0.0),
            "risk_mode": account.get("risk_mode") or "normal",
            "notes": account.get("notes"),
            "permission_status": credential.get("permission_status") or "unknown",
            "token_status": credential.get("token_status") or "unknown",
            "reconciliation_status": credential.get("reconciliation_status") or "unknown",
        }

    def get_multiuser_dashboard_summary(self) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS total FROM user_accounts WHERE status = 'active'")
            active_accounts = int((cursor.fetchone() or {"total": 0})["total"] or 0)

            cursor.execute(
                '''
                SELECT COUNT(*) AS total
                FROM user_accounts
                WHERE status = 'active' AND paper_enabled = 1 AND live_enabled = 0
                '''
            )
            paper_accounts = int((cursor.fetchone() or {"total": 0})["total"] or 0)

            cursor.execute(
                '''
                SELECT COUNT(DISTINCT a.user_id || ':' || a.account_id) AS total
                FROM user_accounts a
                LEFT JOIN user_governance_state g
                  ON g.user_id = a.user_id AND g.account_id = a.account_id
                WHERE a.status = 'active' AND (a.live_enabled = 0 OR g.blocked = 1 OR lower(g.governance_mode) = 'blocked')
                '''
            )
            blocked_accounts = int((cursor.fetchone() or {"total": 0})["total"] or 0)

            cursor.execute(
                '''
                SELECT COUNT(DISTINCT user_id || ':' || account_id) AS total
                FROM user_execution_events
                WHERE event_status = 'error'
                  AND created_at >= datetime('now', '-24 hours')
                '''
            )
            operational_error_accounts = int((cursor.fetchone() or {"total": 0})["total"] or 0)

            cursor.execute(
                '''
                SELECT COUNT(*) AS total
                FROM user_exchange_credentials
                WHERE lower(reconciliation_status) IN ('broken', 'mismatch', 'reconciliation_mismatch')
                '''
            )
            mismatch_accounts = int((cursor.fetchone() or {"total": 0})["total"] or 0)

            return {
                "active_accounts": active_accounts,
                "paper_accounts": paper_accounts,
                "blocked_accounts": blocked_accounts,
                "operational_error_accounts": operational_error_accounts,
                "mismatch_accounts": mismatch_accounts,
            }
        finally:
            conn.close()

    def get_multiuser_account_overview(self, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    a.user_id,
                    a.account_id,
                    a.account_alias,
                    a.exchange,
                    a.status,
                    a.live_enabled,
                    a.paper_enabled,
                    a.capital_base,
                    COALESCE(rp.risk_mode, a.risk_mode) AS risk_mode,
                    COALESCE(rp.allowed_position_count, 0) AS allowed_position_count,
                    COALESCE(cred.permission_status, 'unknown') AS permission_status,
                    COALESCE(cred.token_status, 'unknown') AS token_status,
                    COALESCE(cred.reconciliation_status, 'unknown') AS reconciliation_status,
                    (
                        SELECT COUNT(*)
                        FROM user_live_positions lp
                        WHERE lp.user_id = a.user_id
                          AND lp.account_id = a.account_id
                          AND lower(lp.status) = 'open'
                    ) AS open_positions,
                    (
                        SELECT COUNT(*)
                        FROM user_live_orders lo
                        WHERE lo.user_id = a.user_id
                          AND lo.account_id = a.account_id
                          AND lower(lo.status) IN ('pending', 'open', 'new')
                    ) AS pending_orders
                FROM user_accounts a
                LEFT JOIN user_risk_profiles rp
                    ON rp.user_id = a.user_id AND rp.account_id = a.account_id
                LEFT JOIN user_exchange_credentials cred
                    ON cred.user_id = a.user_id AND cred.account_id = a.account_id AND cred.exchange = a.exchange
                WHERE a.status = 'active'
                ORDER BY a.user_id ASC, a.account_id ASC
                LIMIT ?
                ''',
                (int(limit),),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def save_strategy_profile(self, profile_data: Dict[str, Any]) -> int:
        """Criar ou atualizar um perfil/versionamento de estrategia."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            symbol = profile_data.get('symbol')
            timeframe = profile_data.get('timeframe')
            strategy_version = profile_data.get('strategy_version')

            cursor.execute(
                '''
                SELECT id FROM strategy_profiles
                WHERE symbol = ? AND timeframe = ? AND strategy_version = ?
                ORDER BY id DESC
                LIMIT 1
                ''',
                (symbol, timeframe, strategy_version),
            )
            existing = cursor.fetchone()
            now_br = format_brazil_time()

            values = {
                'status': profile_data.get('status', 'draft'),
                'market_state': profile_data.get('market_state'),
                'allowed_market_states': self._to_json_text(profile_data.get('allowed_market_states')),
                'setup_type': profile_data.get('setup_type'),
                'allowed_setup_types': self._to_json_text(profile_data.get('allowed_setup_types')),
                'rsi_period': profile_data.get('rsi_period'),
                'rsi_min': profile_data.get('rsi_min'),
                'rsi_max': profile_data.get('rsi_max'),
                'context_timeframe': profile_data.get('context_timeframe'),
                'stop_loss_pct': profile_data.get('stop_loss_pct', 0.0),
                'take_profit_pct': profile_data.get('take_profit_pct', 0.0),
                'require_volume': int(bool(profile_data.get('require_volume', False))),
                'require_trend': int(bool(profile_data.get('require_trend', False))),
                'avoid_ranging': int(bool(profile_data.get('avoid_ranging', False))),
                'source_run_id': profile_data.get('source_run_id'),
                'notes': profile_data.get('notes'),
                'promoted_at_br': profile_data.get('promoted_at_br'),
                'deactivated_at_br': profile_data.get('deactivated_at_br'),
                'updated_at_br': now_br,
            }

            if existing:
                cursor.execute(
                    '''
                    UPDATE strategy_profiles
                    SET status = ?, market_state = ?, allowed_market_states = ?, setup_type = ?, allowed_setup_types = ?, rsi_period = ?, rsi_min = ?, rsi_max = ?,
                        context_timeframe = ?, stop_loss_pct = ?, take_profit_pct = ?, require_volume = ?, require_trend = ?, avoid_ranging = ?,
                        source_run_id = ?, notes = ?, promoted_at_br = ?, deactivated_at_br = ?,
                        updated_at = CURRENT_TIMESTAMP, updated_at_br = ?
                    WHERE id = ?
                    ''',
                    (
                        values['status'],
                        values['market_state'],
                        values['allowed_market_states'],
                        values['setup_type'],
                        values['allowed_setup_types'],
                        values['rsi_period'],
                        values['rsi_min'],
                        values['rsi_max'],
                        values['context_timeframe'],
                        values['stop_loss_pct'],
                        values['take_profit_pct'],
                        values['require_volume'],
                        values['require_trend'],
                        values['avoid_ranging'],
                        values['source_run_id'],
                        values['notes'],
                        values['promoted_at_br'],
                        values['deactivated_at_br'],
                        values['updated_at_br'],
                        existing['id'],
                    ),
                )
                profile_id = existing['id']
            else:
                cursor.execute(
                    '''
                    INSERT INTO strategy_profiles (
                        symbol, timeframe, context_timeframe, strategy_version, market_state, allowed_market_states, setup_type, allowed_setup_types, status, rsi_period, rsi_min, rsi_max,
                        stop_loss_pct, take_profit_pct, require_volume, require_trend, avoid_ranging, source_run_id,
                        notes, promoted_at_br, deactivated_at_br, created_at_br, updated_at_br
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        symbol,
                        timeframe,
                        values['context_timeframe'],
                        strategy_version,
                        values['market_state'],
                        values['allowed_market_states'],
                        values['setup_type'],
                        values['allowed_setup_types'],
                        values['status'],
                        values['rsi_period'],
                        values['rsi_min'],
                        values['rsi_max'],
                        values['stop_loss_pct'],
                        values['take_profit_pct'],
                        values['require_volume'],
                        values['require_trend'],
                        values['avoid_ranging'],
                        values['source_run_id'],
                        values['notes'],
                        values['promoted_at_br'],
                        values['deactivated_at_br'],
                        now_br,
                        now_br,
                    ),
                )
                profile_id = cursor.lastrowid

            conn.commit()
            return profile_id
        finally:
            conn.close()

    def get_strategy_profiles(
        self,
        symbol: str = None,
        timeframe: str = None,
        status: str = None,
        limit: int = 50,
    ) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM strategy_profiles
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR status = ?)
            ORDER BY
              CASE WHEN status = 'active' THEN 0 ELSE 1 END,
              COALESCE(updated_at, created_at) DESC
            LIMIT ?
            ''',
            (symbol, symbol, timeframe, timeframe, status, status, limit),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            row["allowed_market_states"] = self._to_list(row.get("allowed_market_states"))
            row["allowed_setup_types"] = self._to_list(row.get("allowed_setup_types"))
        conn.close()
        return rows

    def get_active_strategy_profile(self, symbol: str, timeframe: str) -> Optional[Dict]:
        profiles = self.get_strategy_profiles(symbol=symbol, timeframe=timeframe, status='active', limit=1)
        return profiles[0] if profiles else None

    def _calculate_run_period_days(self, run: Dict[str, Any]) -> float:
        start_date = run.get('start_date')
        end_date = run.get('end_date')
        if not start_date or not end_date:
            return 0.0
        try:
            start_dt = datetime.fromisoformat(str(start_date))
            end_dt = datetime.fromisoformat(str(end_date))
        except ValueError:
            return 0.0
        return max((end_dt - start_dt).total_seconds() / 86400.0, 0.0)

    def _cap_profit_factor(self, value: float) -> float:
        try:
            normalized = float(value or 0.0)
        except (TypeError, ValueError):
            normalized = 0.0
        if normalized <= 0:
            return 0.0
        return min(normalized, float(ProductionConfig.MAX_STATISTICAL_PROFIT_FACTOR))

    def _rank_setup_candidates(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not rows:
            return None

        min_setup_trades = max(int(ProductionConfig.MIN_PROMOTION_SETUP_TRADES), 1)
        candidates = []
        for row in rows:
            setup_type = str(row.get('setup_type') or row.get('setup_name') or '').strip().lower()
            if not setup_type:
                continue
            total_trades = int(row.get('total_trades', 0) or 0)
            profit_factor = self._cap_profit_factor(float(row.get('profit_factor', 0.0) or 0.0))
            win_rate = float(row.get('win_rate', 0.0) or 0.0)
            net_profit = float(row.get('net_profit', 0.0) or 0.0)
            setup_score = (
                min(max(profit_factor - 1.0, 0.0), 1.5) * 35.0
                + min(max(win_rate, 0.0), 100.0) * 0.25
                + (10.0 if net_profit > 0 else -6.0)
                + min(total_trades, 120) * 0.10
            )
            candidates.append(
                {
                    'setup_type': setup_type,
                    'total_trades': total_trades,
                    'profit_factor': round(profit_factor, 2),
                    'win_rate': round(win_rate, 2),
                    'net_profit': round(net_profit, 2),
                    'meets_min_sample': total_trades >= min_setup_trades,
                    'meets_min_pf': profit_factor >= float(ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR),
                    'setup_score': round(max(setup_score, 0.0), 2),
                }
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                bool(item.get('meets_min_sample')),
                bool(item.get('meets_min_pf')),
                float(item.get('net_profit', 0.0) or 0.0) > 0,
                float(item.get('setup_score', 0.0) or 0.0),
                int(item.get('total_trades', 0) or 0),
            ),
            reverse=True,
        )
        return candidates[0]

    @staticmethod
    def _market_states_to_setup_allowlist(market_states: Optional[List[str]]) -> List[str]:
        return market_states_to_setup_allowlist(market_states)

    @staticmethod
    def _setup_types_to_market_state_allowlist(setup_types: Optional[List[str]]) -> List[str]:
        return setup_types_to_market_state_allowlist(setup_types)

    def _rank_market_state_candidates(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not rows:
            return None

        min_market_state_trades = max(int(ProductionConfig.MIN_PROMOTION_SETUP_TRADES), 1)
        candidates = []
        for row in rows:
            market_state = str(row.get('market_state') or row.get('state') or '').strip().lower()
            if not market_state:
                continue
            total_trades = int(row.get('total_trades', 0) or 0)
            profit_factor = self._cap_profit_factor(float(row.get('profit_factor', 0.0) or 0.0))
            win_rate = float(row.get('win_rate', 0.0) or 0.0)
            net_profit = float(row.get('net_profit', 0.0) or 0.0)
            state_score = (
                min(max(profit_factor - 1.0, 0.0), 1.5) * 35.0
                + min(max(win_rate, 0.0), 100.0) * 0.25
                + (10.0 if net_profit > 0 else -6.0)
                + min(total_trades, 120) * 0.10
            )
            candidates.append(
                {
                    'market_state': market_state,
                    'total_trades': total_trades,
                    'profit_factor': round(profit_factor, 2),
                    'win_rate': round(win_rate, 2),
                    'net_profit': round(net_profit, 2),
                    'meets_min_sample': total_trades >= min_market_state_trades,
                    'meets_min_pf': profit_factor >= float(ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR),
                    'state_score': round(max(state_score, 0.0), 2),
                }
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                bool(item.get('meets_min_sample')),
                bool(item.get('meets_min_pf')),
                float(item.get('net_profit', 0.0) or 0.0) > 0,
                float(item.get('state_score', 0.0) or 0.0),
                int(item.get('total_trades', 0) or 0),
            ),
            reverse=True,
        )
        return candidates[0]

    def _derive_approved_market_state_from_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    COALESCE(market_state, '') AS market_state,
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN profit_loss > 0 THEN profit_loss ELSE 0 END) AS gross_profit,
                    ABS(SUM(CASE WHEN profit_loss < 0 THEN profit_loss ELSE 0 END)) AS gross_loss,
                    AVG(CASE WHEN profit_loss_pct IS NOT NULL THEN profit_loss_pct ELSE 0 END) AS expectancy_pct,
                    AVG(CASE WHEN profit_loss > 0 THEN 1.0 ELSE 0.0 END) * 100 AS win_rate,
                    SUM(COALESCE(profit_loss, 0.0)) AS net_profit
                FROM backtest_trades
                WHERE run_id = ?
                GROUP BY COALESCE(market_state, '')
                HAVING total_trades > 0
                ''',
                (run_id,),
            )
            rows = cursor.fetchall()
            if not rows:
                return None
            return self._rank_market_state_candidates(
                [
                    {
                        'market_state': row['market_state'],
                        'total_trades': row['total_trades'],
                        'profit_factor': (
                            float(row['gross_profit'] or 0.0) / float(row['gross_loss'] or 1.0)
                            if float(row['gross_loss'] or 0.0) > 0
                            else float(row['gross_profit'] or 0.0)
                        ),
                        'win_rate': row['win_rate'],
                        'net_profit': row['net_profit'],
                        'expectancy_pct': row['expectancy_pct'],
                    }
                    for row in rows
                ]
            )
        finally:
            conn.close()

    def _derive_approved_setup_from_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT
                    COALESCE(setup_name, '') AS setup_type,
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN profit_loss > 0 THEN profit_loss ELSE 0 END) AS gross_profit,
                    ABS(SUM(CASE WHEN profit_loss < 0 THEN profit_loss ELSE 0 END)) AS gross_loss,
                    AVG(CASE WHEN profit_loss_pct IS NOT NULL THEN profit_loss_pct ELSE 0 END) AS expectancy_pct,
                    AVG(CASE WHEN profit_loss > 0 THEN 1.0 ELSE 0.0 END) * 100 AS win_rate,
                    SUM(COALESCE(profit_loss, 0.0)) AS net_profit
                FROM backtest_trades
                WHERE run_id = ?
                GROUP BY COALESCE(setup_name, '')
                HAVING total_trades > 0
                ''',
                (run_id,),
            )
            trade_rows = cursor.fetchall()
            if trade_rows:
                return self._rank_setup_candidates(
                    [
                        {
                            'setup_type': row['setup_type'],
                            'total_trades': row['total_trades'],
                            'profit_factor': (
                                float(row['gross_profit'] or 0.0) / float(row['gross_loss'] or 1.0)
                                if float(row['gross_loss'] or 0.0) > 0
                                else float(row['gross_profit'] or 0.0)
                            ),
                            'win_rate': row['win_rate'],
                            'net_profit': row['net_profit'],
                            'expectancy_pct': row['expectancy_pct'],
                        }
                        for row in trade_rows
                    ]
                )

            cursor.execute(
                '''
                SELECT
                    COALESCE(setup_type, '') AS setup_type,
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN pnl_abs > 0 THEN pnl_abs ELSE 0 END) AS gross_profit,
                    ABS(SUM(CASE WHEN pnl_abs < 0 THEN pnl_abs ELSE 0 END)) AS gross_loss,
                    AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct ELSE 0 END) AS expectancy_pct,
                    AVG(CASE WHEN pnl_abs > 0 THEN 1.0 ELSE 0.0 END) * 100 AS win_rate,
                    SUM(COALESCE(pnl_abs, 0.0)) AS net_profit
                FROM trade_analytics
                WHERE run_id = ?
                GROUP BY COALESCE(setup_type, '')
                HAVING total_trades > 0
                ''',
                (run_id,),
            )
            analytics_rows = cursor.fetchall()
            if analytics_rows:
                return self._rank_setup_candidates(
                    [
                        {
                            'setup_type': row['setup_type'],
                            'total_trades': row['total_trades'],
                            'profit_factor': (
                                float(row['gross_profit'] or 0.0) / float(row['gross_loss'] or 1.0)
                                if float(row['gross_loss'] or 0.0) > 0
                                else float(row['gross_profit'] or 0.0)
                            ),
                            'win_rate': row['win_rate'],
                            'net_profit': row['net_profit'],
                            'expectancy_pct': row['expectancy_pct'],
                        }
                        for row in analytics_rows
                    ]
                )
            return None
        finally:
            conn.close()

    def get_backtest_run_promotion_readiness(self, run_id: int) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM backtest_runs WHERE id = ?', (run_id,))
            run = cursor.fetchone()
        finally:
            conn.close()

        if not run:
            return {
                "ready": False,
                "reasons": ["Backtest nao encontrado."],
                "run": None,
            }

        run = dict(run)
        reasons = []
        total_trades = int(run.get('total_trades', 0) or 0)
        total_return_pct = float(run.get('total_return_pct', 0.0) or 0.0)
        profit_factor = self._cap_profit_factor(float(run.get('profit_factor', 0.0) or 0.0))
        expectancy_pct = float(run.get('expectancy_pct', 0.0) or 0.0)
        max_drawdown = float(run.get('max_drawdown', 0.0) or 0.0)
        evaluation_period_days = float(run.get('evaluation_period_days', 0.0) or 0.0) or self._calculate_run_period_days(run)
        objective_status = str(run.get('objective_status') or '').strip().lower()
        out_of_sample_passed = bool(run.get('out_of_sample_passed', False))
        out_of_sample_trades = int(run.get('out_of_sample_total_trades', 0) or 0)
        out_of_sample_profit_factor = self._cap_profit_factor(float(run.get('out_of_sample_profit_factor', 0.0) or 0.0))
        out_of_sample_expectancy_pct = float(run.get('out_of_sample_expectancy_pct', 0.0) or 0.0)
        walk_forward_windows = int(run.get('walk_forward_windows', 0) or 0)
        walk_forward_passed = bool(run.get('walk_forward_passed', False))
        walk_forward_pass_rate_pct = float(run.get('walk_forward_pass_rate_pct', 0.0) or 0.0)
        walk_forward_oos_profit_factor = self._cap_profit_factor(float(run.get('walk_forward_avg_oos_profit_factor', 0.0) or 0.0))
        approved_market_state = str(run.get('approved_market_state') or '').strip().lower()
        approved_market_states = [
            str(item or '').strip().lower()
            for item in self._to_list(run.get('approved_market_states'))
            if str(item or '').strip()
        ]
        approved_market_state_trades = int(run.get('approved_market_state_trades', 0) or 0)
        approved_market_state_profit_factor = self._cap_profit_factor(
            float(run.get('approved_market_state_profit_factor', 0.0) or 0.0)
        )
        approved_setup_type = str(run.get('approved_setup_type') or '').strip().lower()
        approved_setup_types = [
            str(item or '').strip().lower()
            for item in self._to_list(run.get('approved_setup_types'))
            if str(item or '').strip()
        ]
        approved_setup_trades = int(run.get('approved_setup_trades', 0) or 0)
        approved_setup_profit_factor = self._cap_profit_factor(float(run.get('approved_setup_profit_factor', 0.0) or 0.0))
        if approved_market_state and approved_market_state not in approved_market_states:
            approved_market_states.insert(0, approved_market_state)
        if approved_setup_type and approved_setup_type not in approved_setup_types:
            approved_setup_types.insert(0, approved_setup_type)
        basket_mode = len(approved_market_states) > 1 if approved_market_states else len(approved_setup_types) > 1

        if (not approved_market_state and not approved_market_states) or approved_market_state_trades <= 0:
            derived_market_state = self._derive_approved_market_state_from_run(run_id)
            if derived_market_state:
                approved_market_state = str(derived_market_state.get('market_state') or '').strip().lower()
                approved_market_states = [approved_market_state] if approved_market_state else approved_market_states
                approved_market_state_trades = int(derived_market_state.get('total_trades', 0) or 0)
                approved_market_state_profit_factor = self._cap_profit_factor(
                    float(derived_market_state.get('profit_factor', 0.0) or 0.0)
                )
        if (not approved_setup_type and not approved_setup_types) or approved_setup_trades <= 0:
            derived_setup = self._derive_approved_setup_from_run(run_id)
            if derived_setup:
                approved_setup_type = str(derived_setup.get('setup_type') or '').strip().lower()
                approved_setup_types = [approved_setup_type] if approved_setup_type else approved_setup_types
                approved_setup_trades = int(derived_setup.get('total_trades', 0) or 0)
                approved_setup_profit_factor = self._cap_profit_factor(
                    float(derived_setup.get('profit_factor', 0.0) or 0.0)
                )
        if approved_setup_type and approved_setup_type not in approved_setup_types:
            approved_setup_types.insert(0, approved_setup_type)
        if approved_setup_types and not approved_market_states:
            approved_market_states = self._setup_types_to_market_state_allowlist(approved_setup_types)
            approved_market_state = approved_market_states[0] if approved_market_states else approved_market_state
        if approved_market_state and approved_market_state not in approved_market_states:
            approved_market_states.insert(0, approved_market_state)
        if approved_market_states and approved_market_state_trades <= 0:
            approved_market_state_trades = approved_setup_trades
        if approved_market_states and approved_market_state_profit_factor <= 0:
            approved_market_state_profit_factor = approved_setup_profit_factor
        if approved_market_states and not approved_setup_types:
            approved_setup_types = self._market_states_to_setup_allowlist(approved_market_states)
            approved_setup_type = approved_setup_types[0] if approved_setup_types else approved_setup_type
        basket_mode = len(approved_market_states) > 1 if approved_market_states else len(approved_setup_types) > 1
        if basket_mode:
            approved_market_state_trades = max(approved_market_state_trades, total_trades)
            approved_market_state_profit_factor = max(approved_market_state_profit_factor, profit_factor)
            approved_setup_trades = max(approved_setup_trades, total_trades)
            approved_setup_profit_factor = max(approved_setup_profit_factor, profit_factor)

        if total_trades < ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION:
            reasons.append(
                f"Amostra de backtest insuficiente: {total_trades} trades "
                f"(minimo {ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION})."
            )
        if evaluation_period_days < float(ProductionConfig.MIN_PROMOTION_PERIOD_DAYS):
            reasons.append(
                f"Periodo avaliado insuficiente: {evaluation_period_days:.1f} dias "
                f"(minimo {ProductionConfig.MIN_PROMOTION_PERIOD_DAYS})."
            )
        if total_return_pct <= 0:
            reasons.append("Retorno total do backtest nao positivo.")
        if profit_factor < ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR:
            reasons.append(
                f"Profit factor abaixo do minimo: {profit_factor:.2f} "
                f"(minimo {ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR:.2f})."
            )
        if expectancy_pct < ProductionConfig.MIN_PROMOTION_EXPECTANCY_PCT:
            reasons.append(
                f"Expectancy do backtest abaixo do minimo: {expectancy_pct:.3f}% "
                f"(minimo {ProductionConfig.MIN_PROMOTION_EXPECTANCY_PCT:.3f}%)."
            )
        if max_drawdown > ProductionConfig.MAX_PROMOTION_DRAWDOWN:
            reasons.append(
                f"Drawdown acima do limite: {max_drawdown:.2f}% "
                f"(maximo {ProductionConfig.MAX_PROMOTION_DRAWDOWN:.2f}%)."
            )
        if not approved_market_state and not approved_market_states:
            reasons.append("Backtest nao definiu estado de mercado aprovado para o runtime.")
        if approved_market_state_trades < ProductionConfig.MIN_PROMOTION_SETUP_TRADES:
            reasons.append(
                (
                    f"Cesta de estados de mercado com amostra insuficiente: {approved_market_state_trades} trades "
                    f"(minimo {ProductionConfig.MIN_PROMOTION_SETUP_TRADES})."
                    if basket_mode
                    else f"Estado de mercado foco com amostra insuficiente: {approved_market_state_trades} trades "
                    f"(minimo {ProductionConfig.MIN_PROMOTION_SETUP_TRADES})."
                )
            )
        if (approved_market_state or approved_market_states) and approved_market_state_profit_factor < ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR:
            reasons.append(
                (
                    f"Cesta de estados de mercado com PF insuficiente: {approved_market_state_profit_factor:.2f} "
                    f"(minimo {ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR:.2f})."
                    if basket_mode
                    else f"Estado de mercado foco com PF insuficiente: {approved_market_state_profit_factor:.2f} "
                    f"(minimo {ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR:.2f})."
                )
            )
        if out_of_sample_trades < ProductionConfig.MIN_PROMOTION_OOS_TRADES:
            reasons.append(
                f"Amostra OOS insuficiente: {out_of_sample_trades} trades "
                f"(minimo {ProductionConfig.MIN_PROMOTION_OOS_TRADES})."
            )
        if out_of_sample_profit_factor < ProductionConfig.MIN_PROMOTION_OOS_PROFIT_FACTOR:
            reasons.append(
                f"PF OOS abaixo do minimo: {out_of_sample_profit_factor:.2f} "
                f"(minimo {ProductionConfig.MIN_PROMOTION_OOS_PROFIT_FACTOR:.2f})."
            )
        if out_of_sample_expectancy_pct < ProductionConfig.MIN_PROMOTION_OOS_EXPECTANCY_PCT:
            reasons.append(
                f"Expectancy OOS abaixo do minimo: {out_of_sample_expectancy_pct:.3f}% "
                f"(minimo {ProductionConfig.MIN_PROMOTION_OOS_EXPECTANCY_PCT:.3f}%)."
            )
        if not out_of_sample_passed:
            reasons.append("Setup nao passou na validacao fora da amostra.")
        if walk_forward_windows > 0 and not walk_forward_passed:
            reasons.append("Setup nao passou no walk-forward.")
        if walk_forward_windows > 0 and walk_forward_pass_rate_pct < ProductionConfig.MIN_WALK_FORWARD_PASS_RATE_PCT:
            reasons.append(
                f"Walk-forward abaixo do minimo: {walk_forward_pass_rate_pct:.2f}% "
                f"(minimo {ProductionConfig.MIN_WALK_FORWARD_PASS_RATE_PCT:.2f}%)."
            )
        if walk_forward_windows > 0 and walk_forward_oos_profit_factor < ProductionConfig.MIN_WALK_FORWARD_OOS_PROFIT_FACTOR:
            reasons.append(
                f"PF medio do walk-forward abaixo do minimo: {walk_forward_oos_profit_factor:.2f} "
                f"(minimo {ProductionConfig.MIN_WALK_FORWARD_OOS_PROFIT_FACTOR:.2f})."
            )
        if objective_status == 'blocked':
            reasons.append("Check objetivo do backtest bloqueou a configuracao.")

        return {
            "ready": not reasons,
            "reasons": reasons,
            "run": run,
            "approved_market_state": approved_market_state,
            "approved_market_states": approved_market_states,
            "approved_setup_types": approved_setup_types,
            "thresholds": {
                "min_backtest_trades": ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION,
                "min_setup_trades": ProductionConfig.MIN_PROMOTION_SETUP_TRADES,
                "min_period_days": ProductionConfig.MIN_PROMOTION_PERIOD_DAYS,
                "min_profit_factor": ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR,
                "min_expectancy_pct": ProductionConfig.MIN_PROMOTION_EXPECTANCY_PCT,
                "min_oos_trades": ProductionConfig.MIN_PROMOTION_OOS_TRADES,
                "min_oos_profit_factor": ProductionConfig.MIN_PROMOTION_OOS_PROFIT_FACTOR,
                "max_drawdown": ProductionConfig.MAX_PROMOTION_DRAWDOWN,
            },
        }

    def activate_strategy_profile(self, profile_id: int) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM strategy_profiles WHERE id = ?', (profile_id,))
            profile = cursor.fetchone()
            if not profile:
                return None

            now_br = format_brazil_time()
            cursor.execute(
                '''
                UPDATE strategy_profiles
                SET status = 'inactive',
                    updated_at = CURRENT_TIMESTAMP,
                    updated_at_br = ?
                WHERE symbol = ? AND timeframe = ? AND status = 'active' AND id != ?
                ''',
                (now_br, profile['symbol'], profile['timeframe'], profile_id),
            )
            cursor.execute(
                '''
                UPDATE strategy_profiles
                SET status = 'active',
                    promoted_at_br = ?,
                    deactivated_at_br = NULL,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_at_br = ?
                WHERE id = ?
                ''',
                (now_br, now_br, profile_id),
            )
            conn.commit()
        finally:
            conn.close()

        return self.get_strategy_profiles(limit=1, symbol=profile['symbol'], timeframe=profile['timeframe'], status='active')[0]

    def deactivate_strategy_profile(self, profile_id: int, reason: str = None) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM strategy_profiles WHERE id = ?', (profile_id,))
            profile = cursor.fetchone()
            if not profile:
                return None

            notes = reason if reason else profile['notes']
            now_br = format_brazil_time()
            cursor.execute(
                '''
                UPDATE strategy_profiles
                SET status = 'disabled',
                    notes = ?,
                    deactivated_at_br = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_at_br = ?
                WHERE id = ?
                ''',
                (notes, now_br, now_br, profile_id),
            )
            conn.commit()
        finally:
            conn.close()

        profiles = self.get_strategy_profiles(limit=1, symbol=profile['symbol'], timeframe=profile['timeframe'])
        return profiles[0] if profiles else None

    def promote_backtest_run(self, run_id: int, notes: str = None, require_ready: bool = True) -> Optional[Dict]:
        if require_ready:
            readiness = self.get_backtest_run_promotion_readiness(run_id)
            if not readiness.get('ready'):
                return None

        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM backtest_runs WHERE id = ?', (run_id,))
        run = cursor.fetchone()
        conn.close()
        if not run:
            return None

        run = dict(run)
        readiness = readiness if require_ready else self.get_backtest_run_promotion_readiness(run_id)
        approved_market_state = str(run.get('approved_market_state') or '').strip().lower() or (
            str((readiness or {}).get('approved_market_states', [None])[0] or '').strip().lower()
        )
        approved_market_states = [
            str(item or '').strip().lower()
            for item in self._to_list(run.get('approved_market_states'))
            if str(item or '').strip()
        ]
        if approved_market_state and approved_market_state not in approved_market_states:
            approved_market_states.insert(0, approved_market_state)
        if not approved_market_states:
            approved_market_states = list((readiness or {}).get('approved_market_states') or [])
            approved_market_state = approved_market_states[0] if approved_market_states else approved_market_state
        approved_setup = self._derive_approved_setup_from_run(run_id)
        approved_setup_type = str(run.get('approved_setup_type') or '').strip().lower() or (
            str((approved_setup or {}).get('setup_type') or '').strip().lower()
        )
        approved_setup_types = [
            str(item or '').strip().lower()
            for item in self._to_list(run.get('approved_setup_types'))
            if str(item or '').strip()
        ]
        if approved_setup_type and approved_setup_type not in approved_setup_types:
            approved_setup_types.insert(0, approved_setup_type)
        if approved_setup_types and not approved_market_states:
            approved_market_states = self._setup_types_to_market_state_allowlist(approved_setup_types)
            approved_market_state = approved_market_states[0] if approved_market_states else approved_market_state
        if approved_market_state and approved_market_state not in approved_market_states:
            approved_market_states.insert(0, approved_market_state)
        if approved_market_states and not approved_setup_types:
            approved_setup_types = self._market_states_to_setup_allowlist(approved_market_states)
            approved_setup_type = approved_setup_types[0] if approved_setup_types else approved_setup_type
        if not approved_setup_types and approved_setup_type:
            approved_setup_types = [approved_setup_type]
        strategy_version = run.get('strategy_version') or build_strategy_version(
            symbol=run.get('symbol'),
            timeframe=run.get('timeframe'),
            context_timeframe=run.get('context_timeframe'),
            rsi_period=run.get('rsi_period'),
            rsi_min=run.get('rsi_min'),
            rsi_max=run.get('rsi_max'),
            stop_loss_pct=run.get('stop_loss_pct', 0.0),
            take_profit_pct=run.get('take_profit_pct', 0.0),
            require_volume=bool(run.get('require_volume', False)),
            require_trend=bool(run.get('require_trend', False)),
            avoid_ranging=bool(run.get('avoid_ranging', False)),
        )
        profile_id = self.save_strategy_profile(
            {
                'symbol': run.get('symbol'),
                'timeframe': run.get('timeframe'),
                'context_timeframe': run.get('context_timeframe'),
                'strategy_version': strategy_version,
                'market_state': approved_market_state or None,
                'allowed_market_states': approved_market_states,
                'setup_type': approved_setup_type or None,
                'allowed_setup_types': approved_setup_types,
                'status': 'active',
                'rsi_period': run.get('rsi_period'),
                'rsi_min': run.get('rsi_min'),
                'rsi_max': run.get('rsi_max'),
                'stop_loss_pct': run.get('stop_loss_pct', 0.0),
                'take_profit_pct': run.get('take_profit_pct', 0.0),
                'require_volume': bool(run.get('require_volume', False)),
                'require_trend': bool(run.get('require_trend', False)),
                'avoid_ranging': bool(run.get('avoid_ranging', False)),
                'source_run_id': run_id,
                'notes': notes,
                'promoted_at_br': format_brazil_time(),
            }
        )
        return self.activate_strategy_profile(profile_id)
    
    def save_trading_signal(self, signal_data: Dict[str, Any]) -> int:
        """Salvar um sinal de trading"""
        conn = self.get_connection()
        cursor = conn.cursor()
        candle_timestamp = signal_data.get('candle_timestamp')
        try:
            cursor.execute(
                '''
                INSERT INTO trading_signals
                (
                    symbol, timeframe, context_timeframe, strategy_version, regime, signal_type, price, rsi, macd_signal, macd_value,
                    signal_strength, volume, candle_timestamp, created_at_br, sent_telegram, sent_telegram_at, telegram_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    signal_data.get('symbol'),
                    signal_data.get('timeframe'),
                    signal_data.get('context_timeframe'),
                    signal_data.get('strategy_version'),
                    signal_data.get('regime'),
                    signal_data.get('signal'),
                    signal_data.get('price'),
                    signal_data.get('rsi'),
                    signal_data.get('macd_signal'),
                    signal_data.get('macd_value'),
                    signal_data.get('signal_strength', 0.0),
                    signal_data.get('volume'),
                    candle_timestamp,
                    format_brazil_time(),
                    int(bool(signal_data.get('sent_telegram', False))),
                    signal_data.get('sent_telegram_at'),
                    signal_data.get('telegram_error'),
                ),
            )
            signal_id = cursor.lastrowid
            conn.commit()
            return int(signal_id)
        except Exception as exc:
            is_sqlite_integrity = isinstance(exc, sqlite3.IntegrityError)
            is_postgres_unique = "duplicate key value violates unique constraint" in str(exc).lower()
            if not (is_sqlite_integrity or is_postgres_unique):
                raise
            if candle_timestamp:
                cursor.execute(
                    '''
                    SELECT id
                    FROM trading_signals
                    WHERE symbol = ? AND timeframe = ? AND signal_type = ? AND candle_timestamp = ?
                    ORDER BY id DESC
                    LIMIT 1
                    ''',
                    (
                        signal_data.get('symbol'),
                        signal_data.get('timeframe'),
                        signal_data.get('signal'),
                        candle_timestamp,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    return int(row['id'])
            raise
        finally:
            conn.close()

    def has_signal_for_candle(
        self,
        symbol: str,
        timeframe: str,
        signal: str,
        candle_timestamp: Optional[str],
    ) -> bool:
        if not candle_timestamp:
            return False

        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT 1
            FROM trading_signals
            WHERE symbol = ? AND timeframe = ? AND signal_type = ? AND candle_timestamp = ?
            LIMIT 1
            ''',
            (symbol, timeframe, signal, candle_timestamp),
        )
        exists = cursor.fetchone() is not None
        conn.close()
        return exists

    def get_pending_telegram_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT *
            FROM trading_signals
            WHERE sent_telegram = 0
              AND signal_type IN ('COMPRA', 'VENDA', 'COMPRA_FRACA', 'VENDA_FRACA')
            ORDER BY id ASC
            LIMIT ?
            ''',
            (limit,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def mark_trading_signal_telegram_sent(self, signal_id: int, error: Optional[str] = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        if error:
            cursor.execute(
                '''
                UPDATE trading_signals
                SET telegram_error = ?
                WHERE id = ?
                ''',
                (str(error), signal_id),
            )
        else:
            cursor.execute(
                '''
                UPDATE trading_signals
                SET sent_telegram = 1,
                    sent_telegram_at = ?,
                    telegram_error = NULL
                WHERE id = ?
                ''',
                (format_brazil_time(), signal_id),
            )
        conn.commit()
        conn.close()

    def create_paper_trade(self, trade_data: Dict[str, Any]) -> int:
        """Criar um paper trade para acompanhamento de outcome do sinal."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            columns = [
                'symbol', 'timeframe', 'context_timeframe', 'setup_name', 'strategy_version', 'execution_mode', 'regime', 'signal_score', 'atr', 'sample_type',
                'signal', 'side', 'source', 'entry_timestamp', 'entry_reason', 'entry_quality', 'rejection_reason', 'entry_price',
                'stop_loss_pct', 'take_profit_pct', 'fee_rate', 'slippage', 'stop_loss_price', 'take_profit_price',
                'initial_stop_price', 'initial_take_price', 'final_stop_price', 'final_take_price',
                'break_even_active', 'trailing_active', 'protection_level', 'regime_exit_flag', 'structure_exit_flag',
                'post_pump_protection', 'mfe_pct', 'mae_pct', 'max_unrealized_rr',
                'planned_risk_pct', 'planned_risk_amount', 'planned_position_notional', 'planned_quantity',
                'account_reference_balance', 'risk_mode', 'size_reduced', 'risk_reason',
                'status', 'outcome', 'close_reason', 'exit_reason', 'exit_timestamp', 'exit_price', 'result_pct',
                'created_at_br'
            ]
            values = {
                'symbol': trade_data.get('symbol'),
                'timeframe': trade_data.get('timeframe'),
                'context_timeframe': trade_data.get('context_timeframe'),
                'setup_name': trade_data.get('setup_name') or trade_data.get('strategy_version'),
                'strategy_version': trade_data.get('strategy_version'),
                'execution_mode': trade_data.get('execution_mode'),
                'regime': trade_data.get('regime'),
                'signal_score': trade_data.get('signal_score', 0.0),
                'atr': trade_data.get('atr', 0.0),
                'sample_type': trade_data.get('sample_type', 'paper'),
                'signal': trade_data.get('signal'),
                'side': trade_data.get('side'),
                'source': trade_data.get('source', 'system'),
                'entry_timestamp': trade_data.get('entry_timestamp'),
                'entry_reason': trade_data.get('entry_reason') or trade_data.get('signal'),
                'entry_quality': trade_data.get('entry_quality'),
                'rejection_reason': trade_data.get('rejection_reason'),
                'entry_price': trade_data.get('entry_price'),
                'stop_loss_pct': trade_data.get('stop_loss_pct', 0.0),
                'take_profit_pct': trade_data.get('take_profit_pct', 0.0),
                'fee_rate': trade_data.get('fee_rate', 0.0),
                'slippage': trade_data.get('slippage', 0.0),
                'stop_loss_price': trade_data.get('stop_loss_price'),
                'take_profit_price': trade_data.get('take_profit_price'),
                'initial_stop_price': trade_data.get('initial_stop_price', trade_data.get('stop_loss_price')),
                'initial_take_price': trade_data.get('initial_take_price', trade_data.get('take_profit_price')),
                'final_stop_price': trade_data.get('final_stop_price', trade_data.get('stop_loss_price')),
                'final_take_price': trade_data.get('final_take_price', trade_data.get('take_profit_price')),
                'break_even_active': int(bool(trade_data.get('break_even_active', False))),
                'trailing_active': int(bool(trade_data.get('trailing_active', False))),
                'protection_level': trade_data.get('protection_level', 'normal'),
                'regime_exit_flag': int(bool(trade_data.get('regime_exit_flag', False))),
                'structure_exit_flag': int(bool(trade_data.get('structure_exit_flag', False))),
                'post_pump_protection': int(bool(trade_data.get('post_pump_protection', False))),
                'mfe_pct': trade_data.get('mfe_pct', 0.0),
                'mae_pct': trade_data.get('mae_pct', 0.0),
                'max_unrealized_rr': trade_data.get('max_unrealized_rr', 0.0),
                'planned_risk_pct': trade_data.get('planned_risk_pct', 0.0),
                'planned_risk_amount': trade_data.get('planned_risk_amount', 0.0),
                'planned_position_notional': trade_data.get('planned_position_notional', 0.0),
                'planned_quantity': trade_data.get('planned_quantity', 0.0),
                'account_reference_balance': trade_data.get('account_reference_balance', 0.0),
                'risk_mode': trade_data.get('risk_mode', 'normal'),
                'size_reduced': int(bool(trade_data.get('size_reduced', False))),
                'risk_reason': trade_data.get('risk_reason'),
                'status': trade_data.get('status', 'OPEN'),
                'outcome': trade_data.get('outcome', 'OPEN'),
                'close_reason': trade_data.get('close_reason'),
                'exit_reason': trade_data.get('exit_reason') or trade_data.get('close_reason'),
                'exit_timestamp': trade_data.get('exit_timestamp'),
                'exit_price': trade_data.get('exit_price'),
                'result_pct': trade_data.get('result_pct', 0.0),
                'created_at_br': format_brazil_time(),
            }
            placeholders = ', '.join(['?'] * len(columns))
            cursor.execute(
                f"INSERT INTO paper_trades ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_open_paper_trades(self, symbol: str = None, timeframe: str = None, strategy_version: str = None) -> List[Dict]:
        """Buscar paper trades ainda abertos."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM paper_trades
            WHERE status = 'OPEN'
              AND (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY entry_timestamp ASC
        ''', (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version))
        trades = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return trades

    def get_open_portfolio_risk_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
                COUNT(*) AS open_trades,
                COALESCE(SUM(planned_risk_pct), 0) AS total_open_risk_pct,
                COALESCE(SUM(planned_risk_amount), 0) AS total_open_risk_amount,
                COALESCE(SUM(planned_position_notional), 0) AS total_open_position_notional
            FROM paper_trades
            WHERE status = 'OPEN'
              AND (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ''',
            (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version),
        )
        summary = dict(cursor.fetchone())
        conn.close()
        return summary

    def get_daily_paper_guardrail_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        session_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        reference_dt = session_date or get_brazil_datetime_naive()
        if hasattr(reference_dt, "to_pydatetime"):
            reference_dt = reference_dt.to_pydatetime()
        day_start = reference_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
                id,
                outcome,
                result_pct,
                planned_position_notional,
                account_reference_balance,
                exit_timestamp
            FROM paper_trades
            WHERE status = 'CLOSED'
              AND exit_timestamp IS NOT NULL
              AND exit_timestamp >= ?
              AND exit_timestamp < ?
              AND (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY exit_timestamp DESC
            ''',
            (
                day_start.isoformat(),
                day_end.isoformat(),
                symbol,
                symbol,
                timeframe,
                timeframe,
                strategy_version,
                strategy_version,
            ),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        reference_balance = max(
            float(row.get("account_reference_balance", 0.0) or 0.0) for row in rows
        ) if rows else float(ProductionConfig.PAPER_ACCOUNT_BALANCE)
        if reference_balance <= 0:
            reference_balance = float(ProductionConfig.PAPER_ACCOUNT_BALANCE)

        realized_pnl = sum(
            float(row.get("planned_position_notional", 0.0) or 0.0) * float(row.get("result_pct", 0.0) or 0.0) / 100
            for row in rows
        )
        realized_pnl_pct = (realized_pnl / reference_balance * 100) if reference_balance else 0.0

        consecutive_losses = 0
        for row in rows:
            if row.get("outcome") == "LOSS":
                consecutive_losses += 1
                continue
            if row.get("outcome") in {"WIN", "FLAT"}:
                break

        wins = sum(1 for row in rows if row.get("outcome") == "WIN")
        losses = sum(1 for row in rows if row.get("outcome") == "LOSS")
        flats = sum(1 for row in rows if row.get("outcome") == "FLAT")

        return {
            "session_date": day_start.date().isoformat(),
            "closed_trades": len(rows),
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "realized_pnl": round(realized_pnl, 2),
            "realized_pnl_pct": round(realized_pnl_pct, 4),
            "reference_balance": round(reference_balance, 2),
            "consecutive_losses": consecutive_losses,
        }

    def get_recent_paper_trades(
        self,
        limit: int = 50,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> List[Dict]:
        """Buscar paper trades recentes, abertos ou fechados."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM paper_trades
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY created_at DESC
            LIMIT ?
        ''', (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version, limit))
        trades = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return trades

    def get_paper_drawdown_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
                planned_position_notional,
                account_reference_balance,
                result_pct,
                exit_timestamp
            FROM paper_trades
            WHERE status = 'CLOSED'
              AND exit_timestamp IS NOT NULL
              AND (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY exit_timestamp ASC, id ASC
            ''',
            (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        starting_balance = max(
            (float(row.get("account_reference_balance", 0.0) or 0.0) for row in rows),
            default=float(ProductionConfig.PAPER_ACCOUNT_BALANCE),
        )
        if starting_balance <= 0:
            starting_balance = float(ProductionConfig.PAPER_ACCOUNT_BALANCE)

        equity = starting_balance
        peak_equity = starting_balance
        current_drawdown_pct = 0.0
        max_drawdown_pct = 0.0
        for row in rows:
            pnl = (
                float(row.get("planned_position_notional", 0.0) or 0.0)
                * float(row.get("result_pct", 0.0) or 0.0)
                / 100.0
            )
            equity += pnl
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                current_drawdown_pct = max(((peak_equity - equity) / peak_equity) * 100.0, 0.0)
                max_drawdown_pct = max(max_drawdown_pct, current_drawdown_pct)

        return {
            "closed_trades": len(rows),
            "starting_balance": round(starting_balance, 2),
            "current_equity": round(equity, 2),
            "peak_equity": round(peak_equity, 2),
            "current_drawdown_pct": round(current_drawdown_pct, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
        }

    def close_paper_trade(
        self,
        trade_id: int,
        exit_timestamp: str,
        exit_price: float,
        outcome: str,
        close_reason: str,
        result_pct: float,
        final_stop_price: float = None,
        final_take_price: float = None,
        break_even_active: bool = False,
        trailing_active: bool = False,
        protection_level: str = None,
        regime_exit_flag: bool = False,
        structure_exit_flag: bool = False,
        post_pump_protection: bool = False,
        mfe_pct: float = 0.0,
        mae_pct: float = 0.0,
        max_unrealized_rr: float = 0.0,
    ):
        """Fechar paper trade com outcome calculado."""
        conn = self.get_connection()
        trade = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT symbol, timeframe, strategy_version FROM paper_trades WHERE id = ?',
                (trade_id,),
            )
            trade = cursor.fetchone()
            cursor.execute('''
                UPDATE paper_trades
                SET status = 'CLOSED',
                    outcome = ?,
                    close_reason = ?,
                    exit_reason = ?,
                    exit_timestamp = ?,
                    exit_price = ?,
                    result_pct = ?,
                    final_stop_price = COALESCE(?, final_stop_price),
                    final_take_price = COALESCE(?, final_take_price),
                    break_even_active = ?,
                    trailing_active = ?,
                    protection_level = COALESCE(?, protection_level),
                    regime_exit_flag = ?,
                    structure_exit_flag = ?,
                    post_pump_protection = ?,
                    mfe_pct = ?,
                    mae_pct = ?,
                    max_unrealized_rr = ?
                WHERE id = ?
            ''', (
                outcome,
                close_reason,
                close_reason,
                exit_timestamp,
                exit_price,
                result_pct,
                final_stop_price,
                final_take_price,
                int(bool(break_even_active)),
                int(bool(trailing_active)),
                protection_level,
                int(bool(regime_exit_flag)),
                int(bool(structure_exit_flag)),
                int(bool(post_pump_protection)),
                mfe_pct,
                mae_pct,
                max_unrealized_rr,
                trade_id,
            ))
            conn.commit()
        finally:
            conn.close()

        if trade:
            self.compute_strategy_metrics(
                symbol=trade['symbol'],
                timeframe=trade['timeframe'],
                strategy_version=trade['strategy_version'],
                evaluation_type='paper',
                persist=True,
                notes=f"Snapshot apos fechamento do paper trade #{trade_id}",
            )

    def update_paper_trade_management(
        self,
        trade_id: int,
        stop_loss_price: float = None,
        take_profit_price: float = None,
        break_even_active: bool = False,
        trailing_active: bool = False,
        protection_level: str = None,
        regime_exit_flag: bool = False,
        structure_exit_flag: bool = False,
        post_pump_protection: bool = False,
        mfe_pct: float = 0.0,
        mae_pct: float = 0.0,
        max_unrealized_rr: float = 0.0,
    ):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE paper_trades
                SET stop_loss_price = COALESCE(?, stop_loss_price),
                    take_profit_price = COALESCE(?, take_profit_price),
                    final_stop_price = COALESCE(?, final_stop_price),
                    final_take_price = COALESCE(?, final_take_price),
                    break_even_active = ?,
                    trailing_active = ?,
                    protection_level = COALESCE(?, protection_level),
                    regime_exit_flag = ?,
                    structure_exit_flag = ?,
                    post_pump_protection = ?,
                    mfe_pct = ?,
                    mae_pct = ?,
                    max_unrealized_rr = ?
                WHERE id = ?
                ''',
                (
                    stop_loss_price,
                    take_profit_price,
                    stop_loss_price,
                    take_profit_price,
                    int(bool(break_even_active)),
                    int(bool(trailing_active)),
                    protection_level,
                    int(bool(regime_exit_flag)),
                    int(bool(structure_exit_flag)),
                    int(bool(post_pump_protection)),
                    mfe_pct,
                    mae_pct,
                    max_unrealized_rr,
                    trade_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_paper_trade_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        """Resumo agregado de paper trades para medir edge live/paper."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                COUNT(*) AS total_trades,
                COALESCE(SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_trades,
                COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END), 0) AS closed_trades,
                COALESCE(SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN outcome = 'FLAT' THEN 1 ELSE 0 END), 0) AS flats,
                COALESCE(AVG(CASE WHEN status = 'CLOSED' THEN result_pct END), 0) AS avg_result_pct,
                COALESCE(AVG(CASE WHEN outcome = 'WIN' THEN result_pct END), 0) AS avg_win_pct,
                COALESCE(AVG(CASE WHEN outcome = 'LOSS' THEN result_pct END), 0) AS avg_loss_pct,
                COALESCE(SUM(CASE WHEN status = 'CLOSED' AND result_pct > 0 THEN result_pct ELSE 0 END), 0) AS gross_profit_pct,
                COALESCE(SUM(CASE WHEN status = 'CLOSED' AND result_pct < 0 THEN ABS(result_pct) ELSE 0 END), 0) AS gross_loss_pct,
                COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN result_pct ELSE 0 END), 0) AS total_result_pct
            FROM paper_trades
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
        ''', (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version))
        summary = dict(cursor.fetchone())
        closed = summary.get('closed_trades', 0) or 0
        wins = summary.get('wins', 0) or 0
        summary['win_rate'] = round((wins / closed * 100), 2) if closed else 0.0
        gross_profit = float(summary.get('gross_profit_pct', 0.0) or 0.0)
        gross_loss = float(summary.get('gross_loss_pct', 0.0) or 0.0)
        if gross_loss > 0:
            summary['profit_factor'] = round(gross_profit / gross_loss, 4)
        elif gross_profit > 0:
            summary['profit_factor'] = float('inf')
        else:
            summary['profit_factor'] = 0.0
        conn.close()
        return summary

    def _merge_backtest_trade_aggregates(self, target: Dict[str, Any], trade_summary: Dict[str, Any]) -> Dict[str, Any]:
        total_trade_rows = int(trade_summary.get("total_trade_rows", 0) or 0)
        if total_trade_rows <= 0:
            return target

        gross_profit = float(trade_summary.get("gross_profit", 0.0) or 0.0)
        gross_loss = float(trade_summary.get("gross_loss", 0.0) or 0.0)
        avg_trade_result_pct = float(trade_summary.get("avg_trade_result_pct", 0.0) or 0.0)
        winning_trades = int(trade_summary.get("winning_trades", 0) or 0)
        aggregate_win_rate = (winning_trades / total_trade_rows) * 100

        if gross_loss > 0:
            aggregate_profit_factor = gross_profit / gross_loss
        else:
            aggregate_profit_factor = gross_profit if gross_profit > 0 else 0.0

        aggregate_profit_factor = self._cap_profit_factor(aggregate_profit_factor)
        target["avg_profit_factor"] = round(aggregate_profit_factor, 2)
        target["avg_expectancy_pct"] = round(avg_trade_result_pct, 2)
        target["avg_win_rate"] = round(aggregate_win_rate, 2)
        target["aggregate_total_trades"] = total_trade_rows
        target["aggregate_profit_factor"] = round(aggregate_profit_factor, 4)
        target["aggregate_expectancy_pct"] = round(avg_trade_result_pct, 4)
        target["aggregate_win_rate"] = round(aggregate_win_rate, 2)
        return target
    
    def get_recent_signals(self, limit: int = 100, symbol: str = None) -> List[Dict]:
        """Buscar sinais recentes"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        query = '''
            SELECT * FROM trading_signals 
            WHERE (? IS NULL OR symbol = ?)
            ORDER BY created_at DESC 
            LIMIT ?
        '''
        cursor.execute(query, (symbol, symbol, limit))
        signals = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        return signals
    
    def get_signals_by_date_range(self, start_date: str, end_date: str, symbol: str = None) -> List[Dict]:
        """Buscar sinais por período"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        query = '''
            SELECT * FROM trading_signals 
            WHERE created_at BETWEEN ? AND ?
            AND (? IS NULL OR symbol = ?)
            ORDER BY created_at DESC
        '''
        cursor.execute(query, (start_date, end_date, symbol, symbol))
        signals = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        return signals
    
    def save_setting(self, key: str, value: Any):
        """Salvar configuração"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO settings (key, value)
            VALUES (?, ?)
        ''', (key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)))
        
        conn.commit()
        conn.close()
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Buscar configuração"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        result = cursor.fetchone()
        
        conn.close()
        
        if result:
            value = result['value']
            # Tentar fazer parse de JSON
            try:
                return json.loads(value)
            except:
                return value
        return default
    
    def save_analysis(self, symbol: str, timeframe: str, analysis_data: Dict):
        """Salvar dados de análise"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO analysis_history (symbol, timeframe, analysis_data, created_at_br)
            VALUES (?, ?, ?, ?)
        ''', (symbol, timeframe, json.dumps(analysis_data), format_brazil_time()))
        
        conn.commit()
        conn.close()
    
    def save_backtest_result(
        self,
        run_data: Dict[str, Any],
        trades: List[Dict[str, Any]],
        trade_analytics: Optional[List[Dict[str, Any]]] = None,
        signal_audit: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """Salvar um backtest completo com resumo e trades."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            column_values = {
                'symbol': run_data.get('symbol'),
                'timeframe': run_data.get('timeframe'),
                'context_timeframe': run_data.get('context_timeframe'),
                'strategy_version': run_data.get('strategy_version'),
                'start_date': run_data.get('start_date'),
                'end_date': run_data.get('end_date'),
                'initial_balance': run_data.get('initial_balance'),
                'final_balance': run_data.get('final_balance'),
                'net_profit': run_data.get('net_profit'),
                'total_return_pct': run_data.get('total_return_pct'),
                'total_trades': run_data.get('total_trades', 0),
                'winning_trades': run_data.get('winning_trades', 0),
                'losing_trades': run_data.get('losing_trades', 0),
                'win_rate': run_data.get('win_rate', 0.0),
                'max_drawdown': run_data.get('max_drawdown', 0.0),
                'sharpe_ratio': run_data.get('sharpe_ratio', 0.0),
                'profit_factor': run_data.get('profit_factor', 0.0),
                'avg_profit': run_data.get('avg_profit', 0.0),
                'avg_loss': run_data.get('avg_loss', 0.0),
                'expectancy_pct': run_data.get('expectancy_pct', 0.0),
                'rsi_period': run_data.get('rsi_period'),
                'rsi_min': run_data.get('rsi_min'),
                'rsi_max': run_data.get('rsi_max'),
                'stop_loss_pct': run_data.get('stop_loss_pct', 0.0),
                'take_profit_pct': run_data.get('take_profit_pct', 0.0),
                'fee_rate': run_data.get('fee_rate', 0.0),
                'slippage': run_data.get('slippage', 0.0),
                'position_size_pct': run_data.get('position_size_pct', 1.0),
                'require_volume': int(bool(run_data.get('require_volume', False))),
                'require_trend': int(bool(run_data.get('require_trend', False))),
                'avoid_ranging': int(bool(run_data.get('avoid_ranging', False))),
                'validation_split_pct': run_data.get('validation_split_pct', 0.0),
                'in_sample_end': run_data.get('in_sample_end'),
                'out_of_sample_start': run_data.get('out_of_sample_start'),
                'in_sample_return_pct': run_data.get('in_sample_return_pct', 0.0),
                'in_sample_profit_factor': run_data.get('in_sample_profit_factor', 0.0),
                'in_sample_win_rate': run_data.get('in_sample_win_rate', 0.0),
                'in_sample_total_trades': run_data.get('in_sample_total_trades', 0),
                'out_of_sample_return_pct': run_data.get('out_of_sample_return_pct', 0.0),
                'out_of_sample_profit_factor': run_data.get('out_of_sample_profit_factor', 0.0),
                'out_of_sample_win_rate': run_data.get('out_of_sample_win_rate', 0.0),
                'out_of_sample_total_trades': run_data.get('out_of_sample_total_trades', 0),
                'out_of_sample_expectancy_pct': run_data.get('out_of_sample_expectancy_pct', 0.0),
                'out_of_sample_passed': int(bool(run_data.get('out_of_sample_passed', False))),
                'walk_forward_windows': run_data.get('walk_forward_windows', 0),
                'walk_forward_passed': int(bool(run_data.get('walk_forward_passed', False))),
                'walk_forward_pass_rate_pct': run_data.get('walk_forward_pass_rate_pct', 0.0),
                'walk_forward_avg_oos_return_pct': run_data.get('walk_forward_avg_oos_return_pct', 0.0),
                'walk_forward_avg_oos_profit_factor': run_data.get('walk_forward_avg_oos_profit_factor', 0.0),
                'walk_forward_avg_oos_expectancy_pct': run_data.get('walk_forward_avg_oos_expectancy_pct', 0.0),
                'objective_status': run_data.get('objective_status'),
                'objective_score': run_data.get('objective_score', 0.0),
                'approved_market_state': run_data.get('approved_market_state'),
                'approved_market_states': self._to_json_text(run_data.get('approved_market_states')),
                'approved_market_state_trades': run_data.get('approved_market_state_trades', 0),
                'approved_market_state_profit_factor': run_data.get('approved_market_state_profit_factor', 0.0),
                'approved_setup_type': run_data.get('approved_setup_type'),
                'approved_setup_types': self._to_json_text(run_data.get('approved_setup_types')),
                'approved_setup_trades': run_data.get('approved_setup_trades', 0),
                'approved_setup_profit_factor': run_data.get('approved_setup_profit_factor', 0.0),
                'evaluation_period_days': run_data.get('evaluation_period_days', 0.0),
                'created_at_br': format_brazil_time(),
            }
            columns = list(column_values.keys())
            placeholders = ', '.join(['?'] * len(columns))
            cursor.execute(
                f"INSERT INTO backtest_runs ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(column_values[column] for column in columns),
            )

            run_id = cursor.lastrowid

            trade_rows = [
                (
                    run_id,
                    run_data.get('symbol'),
                    run_data.get('timeframe'),
                    trade.get('context_timeframe') or run_data.get('context_timeframe'),
                    trade.get('setup_name') or run_data.get('strategy_version'),
                    run_data.get('strategy_version'),
                    trade.get('regime'),
                    trade.get('market_state'),
                    trade.get('execution_mode'),
                    trade.get('signal_score', 0.0),
                    trade.get('atr', 0.0),
                    self._normalize_timestamp(trade.get('entry_timestamp')),
                    trade.get('entry_reason') or trade.get('signal'),
                    trade.get('entry_quality'),
                    trade.get('rejection_reason'),
                    self._normalize_timestamp(trade.get('timestamp')),
                    trade.get('entry_price'),
                    trade.get('price'),
                    trade.get('initial_stop_price'),
                    trade.get('initial_take_price'),
                    trade.get('final_stop_price'),
                    trade.get('final_take_price'),
                    int(bool(trade.get('break_even_active', False))),
                    int(bool(trade.get('trailing_active', False))),
                    trade.get('protection_level'),
                    int(bool(trade.get('regime_exit_flag', False))),
                    int(bool(trade.get('structure_exit_flag', False))),
                    int(bool(trade.get('post_pump_protection', False))),
                    trade.get('mfe_pct', 0.0),
                    trade.get('mae_pct', 0.0),
                    trade.get('max_unrealized_rr', 0.0),
                    trade.get('risk_mode', 'normal'),
                    trade.get('risk_amount', 0.0),
                    trade.get('position_notional', 0.0),
                    trade.get('quantity', 0.0),
                    int(bool(trade.get('size_reduced', False))),
                    trade.get('risk_reason'),
                    trade.get('exit_reason') or trade.get('reason'),
                    trade.get('profit_loss_pct'),
                    trade.get('profit_loss'),
                    trade.get('signal'),
                    trade.get('side'),
                    trade.get('reason'),
                    trade.get('sample_type', 'backtest'),
                )
                for trade in trades
            ]

            if trade_rows:
                trade_placeholders = ', '.join(['?'] * len(trade_rows[0]))
                cursor.executemany('''
                    INSERT INTO backtest_trades (
                        run_id, symbol, timeframe, context_timeframe, setup_name, strategy_version, regime, market_state, execution_mode,
                        signal_score, atr, entry_timestamp,
                        entry_reason, entry_quality, rejection_reason, exit_timestamp, entry_price, exit_price,
                        initial_stop_price, initial_take_price, final_stop_price, final_take_price,
                        break_even_active, trailing_active, protection_level, regime_exit_flag, structure_exit_flag,
                        post_pump_protection, mfe_pct, mae_pct, max_unrealized_rr,
                        risk_mode, risk_amount, position_notional, quantity, size_reduced, risk_reason,
                        exit_reason, profit_loss_pct, profit_loss, signal, side, reason, sample_type
                    ) VALUES (''' + trade_placeholders + ''')
                ''', trade_rows)

            analytics_rows = [
                (
                    run_id,
                    run_data.get('symbol'),
                    run_data.get('timeframe'),
                    trade.get('strategy_version') or run_data.get('strategy_version'),
                    trade.get('setup_name'),
                    trade.get('regime'),
                    trade.get('regime_score', 0.0),
                    trade.get('trend_state'),
                    trade.get('volatility_state'),
                    trade.get('context_bias'),
                    trade.get('directional_bias'),
                    trade.get('structure_state'),
                    trade.get('event_type'),
                    trade.get('regime_phase'),
                    trade.get('context_score', 0.0),
                    trade.get('confirmation_state'),
                    trade.get('entry_quality'),
                    trade.get('entry_score', 0.0),
                    trade.get('risk_mode'),
                    trade.get('reading_execution_mode'),
                    trade.get('context_source'),
                    trade.get('quantity', 0.0),
                    trade.get('position_notional', 0.0),
                    trade.get('risk_amount', 0.0),
                    trade.get('initial_stop_price'),
                    trade.get('initial_take_price'),
                    trade.get('final_stop_price'),
                    trade.get('final_take_price'),
                    trade.get('exit_reason') or trade.get('reason'),
                    self._normalize_timestamp(trade.get('entry_timestamp')),
                    self._normalize_timestamp(trade.get('timestamp')),
                    trade.get('holding_time_minutes', 0.0),
                    trade.get('holding_candles', 0),
                    trade.get('profit_loss_pct', 0.0),
                    trade.get('profit_loss', 0.0),
                    trade.get('mfe_pct', 0.0),
                    trade.get('mae_pct', 0.0),
                    trade.get('rr_realized', 0.0),
                    int(bool(trade.get('break_even_active', False))),
                    int(bool(trade.get('trailing_active', False))),
                    int(bool(trade.get('regime_shift_during_trade', False))),
                    trade.get('profit_given_back_pct', 0.0),
                    json.dumps(trade.get('notes') or []),
                )
                for trade in (trade_analytics or trades or [])
            ]

            if analytics_rows:
                analytics_placeholders = ', '.join(['?'] * len(analytics_rows[0]))
                cursor.executemany('''
                    INSERT INTO trade_analytics (
                        run_id, symbol, timeframe, strategy_version, setup_type, regime, regime_score,
                        trend_state, volatility_state, context_bias, directional_bias, structure_state,
                        event_type, regime_phase, context_score, confirmation_state, entry_quality, entry_score,
                        risk_mode, reading_execution_mode, context_source, position_size, position_notional, risk_amount,
                        stop_initial, take_initial, stop_final, take_final, exit_reason, entry_timestamp,
                        exit_timestamp, holding_time_minutes, holding_candles, pnl_pct, pnl_abs, mfe_pct,
                        mae_pct, rr_realized, break_even_activated, trailing_activated,
                        regime_shift_during_trade, profit_given_back_pct, notes
                    ) VALUES (''' + analytics_placeholders + ''')
                ''', analytics_rows)

            signal_audit_rows = [
                (
                    run_id,
                    audit.get('symbol') or run_data.get('symbol'),
                    audit.get('timeframe') or run_data.get('timeframe'),
                    audit.get('strategy_version') or run_data.get('strategy_version'),
                    self._normalize_timestamp(audit.get('timestamp')),
                    audit.get('candidate_signal'),
                    audit.get('approved_signal'),
                    audit.get('blocked_signal'),
                    audit.get('block_reason'),
                    audit.get('regime'),
                    audit.get('regime_score', 0.0),
                    audit.get('trend_state'),
                    audit.get('volatility_state'),
                    audit.get('context_bias'),
                    audit.get('directional_bias'),
                    audit.get('structure_state'),
                    audit.get('event_type'),
                    audit.get('regime_phase'),
                    audit.get('context_score', 0.0),
                    audit.get('confirmation_state'),
                    audit.get('entry_quality'),
                    audit.get('entry_score', 0.0),
                    audit.get('scenario_score', 0.0),
                    audit.get('setup_type'),
                    audit.get('market_state'),
                    audit.get('execution_mode'),
                    audit.get('reading_execution_mode'),
                    audit.get('context_source'),
                    audit.get('risk_mode'),
                    json.dumps(audit.get('notes') or []),
                )
                for audit in (signal_audit or [])
            ]

            if signal_audit_rows:
                signal_audit_placeholders = ', '.join(['?'] * len(signal_audit_rows[0]))
                cursor.executemany('''
                    INSERT INTO signal_audit (
                        run_id, symbol, timeframe, strategy_version, timestamp, candidate_signal,
                        approved_signal, blocked_signal, block_reason, regime, regime_score, trend_state,
                        volatility_state, context_bias, directional_bias, structure_state, event_type,
                        regime_phase, context_score, confirmation_state, entry_quality, entry_score,
                        scenario_score, setup_type, market_state, execution_mode, reading_execution_mode,
                        context_source, risk_mode, notes
                    ) VALUES (''' + signal_audit_placeholders + ''')
                ''', signal_audit_rows)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.compute_strategy_metrics(
            symbol=run_data.get('symbol'),
            timeframe=run_data.get('timeframe'),
            strategy_version=run_data.get('strategy_version'),
            evaluation_type='backtest',
            persist=True,
            notes=f"Snapshot apos backtest run #{run_id}",
        )
        return run_id

    def get_backtest_runs(
        self,
        limit: int = 50,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> List[Dict]:
        """Buscar execucoes recentes de backtest."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM backtest_runs
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY created_at DESC
            LIMIT ?
        ''', (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version, limit))
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            row["approved_market_states"] = self._to_list(row.get("approved_market_states"))
            row["approved_setup_types"] = self._to_list(row.get("approved_setup_types"))

        conn.close()
        return rows

    def get_backtest_trades(self, run_id: int) -> List[Dict]:
        """Buscar trades de uma execucao especifica."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM backtest_trades
            WHERE run_id = ?
            ORDER BY exit_timestamp ASC
        ''', (run_id,))
        trades = [dict(row) for row in cursor.fetchall()]

        conn.close()
        return trades

    def get_trade_analytics(
        self,
        run_id: int = None,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        limit: int = 500,
    ) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM trade_analytics
            WHERE (? IS NULL OR run_id = ?)
              AND (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY exit_timestamp DESC, id DESC
            LIMIT ?
            ''',
            (run_id, run_id, symbol, symbol, timeframe, timeframe, strategy_version, strategy_version, limit),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            notes = row.get("notes")
            if isinstance(notes, str) and notes:
                try:
                    row["notes"] = json.loads(notes)
                except json.JSONDecodeError:
                    pass
        conn.close()
        return rows

    def get_signal_audit(
        self,
        run_id: int = None,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        limit: int = 1000,
    ) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM signal_audit
            WHERE (? IS NULL OR run_id = ?)
              AND (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            ''',
            (run_id, run_id, symbol, symbol, timeframe, timeframe, strategy_version, strategy_version, limit),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            notes = row.get("notes")
            if isinstance(notes, str) and notes:
                try:
                    row["notes"] = json.loads(notes)
                except json.JSONDecodeError:
                    pass
        conn.close()
        return rows

    @staticmethod
    def _decode_json_field(raw_value: Any, default: Any) -> Any:
        if isinstance(raw_value, str) and raw_value:
            try:
                return json.loads(raw_value)
            except json.JSONDecodeError:
                return default
        return raw_value if raw_value not in (None, "") else default

    @staticmethod
    def _normalize_governance_regime(regime: Optional[str], parabolic: bool = False) -> Optional[str]:
        if parabolic:
            return "parabolic"
        if regime is None:
            return None
        normalized = str(regime).strip().lower()
        return normalized or None

    @staticmethod
    def _summarize_result_rows(
        rows: List[Dict[str, Any]],
        result_key: str,
        giveback_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        total_trades = len(rows)
        if total_trades <= 0:
            return {
                "trade_count": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "expectancy_pct": 0.0,
                "total_return_pct": 0.0,
                "avg_profit_giveback_pct": 0.0,
            }

        results = [float(row.get(result_key, 0.0) or 0.0) for row in rows]
        gross_profit = sum(value for value in results if value > 0)
        gross_loss = sum(abs(value) for value in results if value < 0)
        wins = sum(1 for value in results if value > 0)
        total_return_pct = sum(results)
        expectancy_pct = total_return_pct / total_trades if total_trades else 0.0
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = 999.0
        else:
            profit_factor = 0.0

        givebacks: List[float] = []
        if giveback_key:
            for row in rows:
                givebacks.append(float(row.get(giveback_key, 0.0) or 0.0))

        return {
            "trade_count": total_trades,
            "win_rate": round((wins / total_trades) * 100, 2) if total_trades else 0.0,
            "profit_factor": round(profit_factor, 4),
            "expectancy_pct": round(expectancy_pct, 4),
            "total_return_pct": round(total_return_pct, 4),
            "avg_profit_giveback_pct": round(sum(givebacks) / len(givebacks), 4) if givebacks else 0.0,
        }

    def _get_recent_trade_analytics_rows(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        regime: str = None,
        window_days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            window_modifier = f"-{int(window_days)} days" if window_days else None
            cursor.execute(
                '''
                SELECT ta.*
                FROM trade_analytics ta
                WHERE (? IS NULL OR ta.symbol = ?)
                  AND (? IS NULL OR ta.timeframe = ?)
                  AND (? IS NULL OR ta.strategy_version = ?)
                  AND (? IS NULL OR ta.regime = ?)
                  AND (? IS NULL OR ta.created_at >= datetime('now', ?))
                ORDER BY ta.id DESC
                ''',
                (
                    symbol, symbol,
                    timeframe, timeframe,
                    strategy_version, strategy_version,
                    regime, regime,
                    window_modifier, window_modifier,
                ),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row["notes"] = self._decode_json_field(row.get("notes"), [])
            return rows
        finally:
            conn.close()

    def _get_recent_runtime_trade_rows(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        regime: str = None,
        sample_type: str = "paper",
        window_days: Optional[int] = None,
        max_trades: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            window_modifier = f"-{int(window_days)} days" if window_days else None
            limit = int(max_trades or 500)
            cursor.execute(
                '''
                SELECT *
                FROM paper_trades
                WHERE status = 'CLOSED'
                  AND (? IS NULL OR symbol = ?)
                  AND (? IS NULL OR timeframe = ?)
                  AND (? IS NULL OR strategy_version = ?)
                  AND (? IS NULL OR regime = ?)
                  AND (? IS NULL OR sample_type = ?)
                  AND (? IS NULL OR created_at >= datetime('now', ?))
                ORDER BY id DESC
                LIMIT ?
                ''',
                (
                    symbol, symbol,
                    timeframe, timeframe,
                    strategy_version, strategy_version,
                    regime, regime,
                    sample_type, sample_type,
                    window_modifier, window_modifier,
                    limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _build_regime_baseline_status(self, trade_count: int, profit_factor: float, expectancy_pct: float) -> str:
        if trade_count >= ProductionConfig.GOVERNANCE_MIN_REGIME_TRADES and profit_factor >= ProductionConfig.GOVERNANCE_APPROVED_PF and expectancy_pct >= ProductionConfig.GOVERNANCE_MIN_EXPECTANCY_PCT:
            return "approved"
        reduced_min_trades = max(3, ProductionConfig.GOVERNANCE_MIN_REGIME_TRADES // 2)
        if trade_count >= reduced_min_trades and profit_factor >= ProductionConfig.GOVERNANCE_REDUCED_PF:
            return "reduced"
        return "blocked"

    def refresh_setup_regime_baselines(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        persist: bool = True,
    ) -> List[Dict[str, Any]]:
        backtest_summary = self.get_backtest_performance_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        rows = self._get_recent_trade_analytics_rows(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            window_days=ProductionConfig.GOVERNANCE_LOOKBACK_DAYS,
        )
        if not rows:
            return []

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            regime_key = self._normalize_governance_regime(row.get("regime")) or "unknown"
            grouped.setdefault(regime_key, []).append(row)

        baseline_source = "oos" if float(backtest_summary.get("avg_out_of_sample_profit_factor", 0.0) or 0.0) > 0 else "backtest"
        baselines = []
        for regime_key, regime_rows in grouped.items():
            metrics = self._summarize_result_rows(regime_rows, "pnl_pct", "profit_given_back_pct")
            status = self._build_regime_baseline_status(
                metrics["trade_count"],
                float(metrics["profit_factor"] or 0.0),
                float(metrics["expectancy_pct"] or 0.0),
            )
            notes = []
            if metrics["trade_count"] < ProductionConfig.GOVERNANCE_MIN_REGIME_TRADES:
                notes.append("insufficient_sample")
            if metrics["avg_profit_giveback_pct"] >= ProductionConfig.GOVERNANCE_MAX_PROFIT_GIVEBACK_WARNING_PCT:
                notes.append("high_profit_giveback")

            baselines.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "strategy_version": strategy_version,
                    "regime": regime_key,
                    "baseline_source": baseline_source,
                    "baseline_profit_factor": float(metrics["profit_factor"] or 0.0),
                    "baseline_expectancy_pct": float(metrics["expectancy_pct"] or 0.0),
                    "baseline_win_rate": float(metrics["win_rate"] or 0.0),
                    "baseline_drawdown": float(backtest_summary.get("avg_max_drawdown", 0.0) or 0.0),
                    "baseline_trade_count": int(metrics["trade_count"] or 0),
                    "total_return_pct": float(metrics["total_return_pct"] or 0.0),
                    "oos_profit_factor": float(backtest_summary.get("avg_out_of_sample_profit_factor", 0.0) or 0.0),
                    "oos_expectancy_pct": float(backtest_summary.get("avg_out_of_sample_expectancy_pct", 0.0) or 0.0),
                    "walk_forward_pass_rate_pct": float(backtest_summary.get("avg_walk_forward_pass_rate_pct", 0.0) or 0.0),
                    "performance_status": status,
                    "window_days": int(ProductionConfig.GOVERNANCE_LOOKBACK_DAYS),
                    "notes": notes,
                }
            )

        if persist and symbol and timeframe and strategy_version:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    '''
                    DELETE FROM setup_regime_baselines
                    WHERE symbol = ? AND timeframe = ? AND strategy_version = ?
                    ''',
                    (symbol, timeframe, strategy_version),
                )
                for baseline in baselines:
                    cursor.execute(
                        '''
                        INSERT INTO setup_regime_baselines (
                            symbol, timeframe, strategy_version, regime, baseline_source,
                            baseline_profit_factor, baseline_expectancy_pct, baseline_win_rate,
                            baseline_drawdown, baseline_trade_count, total_return_pct,
                            oos_profit_factor, oos_expectancy_pct, walk_forward_pass_rate_pct,
                            performance_status, window_days, notes, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ''',
                        (
                            baseline["symbol"],
                            baseline["timeframe"],
                            baseline["strategy_version"],
                            baseline["regime"],
                            baseline["baseline_source"],
                            baseline["baseline_profit_factor"],
                            baseline["baseline_expectancy_pct"],
                            baseline["baseline_win_rate"],
                            baseline["baseline_drawdown"],
                            baseline["baseline_trade_count"],
                            baseline["total_return_pct"],
                            baseline["oos_profit_factor"],
                            baseline["oos_expectancy_pct"],
                            baseline["walk_forward_pass_rate_pct"],
                            baseline["performance_status"],
                            baseline["window_days"],
                            json.dumps(baseline["notes"]),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

        return baselines

    def get_setup_regime_baselines(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        if refresh and symbol and timeframe and strategy_version:
            self.refresh_setup_regime_baselines(
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                persist=True,
            )

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM setup_regime_baselines
                WHERE (? IS NULL OR symbol = ?)
                  AND (? IS NULL OR timeframe = ?)
                  AND (? IS NULL OR strategy_version = ?)
                ORDER BY baseline_trade_count DESC, regime ASC
                ''',
                (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row["notes"] = self._decode_json_field(row.get("notes"), [])
            return rows
        finally:
            conn.close()

    def _resolve_governance_identity(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        resolved_symbol = symbol
        resolved_timeframe = timeframe
        resolved_strategy_version = strategy_version
        active_profile = None

        if resolved_symbol and resolved_timeframe:
            active_profile = self.get_active_strategy_profile(resolved_symbol, resolved_timeframe)
            if active_profile and not resolved_strategy_version:
                resolved_strategy_version = active_profile.get("strategy_version")

        if resolved_strategy_version and (not resolved_symbol or not resolved_timeframe):
            profiles = self.get_strategy_profiles(status=None, limit=200)
            for profile in profiles:
                if profile.get("strategy_version") != resolved_strategy_version:
                    continue
                active_profile = profile if profile.get("status") == "active" else active_profile
                resolved_symbol = profile.get("symbol") or resolved_symbol
                resolved_timeframe = profile.get("timeframe") or resolved_timeframe
                break

        if resolved_strategy_version and resolved_symbol and resolved_timeframe and active_profile is None:
            profiles = self.get_strategy_profiles(
                symbol=resolved_symbol,
                timeframe=resolved_timeframe,
                status=None,
                limit=200,
            )
            for profile in profiles:
                if profile.get("strategy_version") != resolved_strategy_version:
                    continue
                if profile.get("status") == "active":
                    active_profile = profile
                break

        if resolved_strategy_version is None:
            recent_runs = self.get_backtest_runs(
                symbol=resolved_symbol,
                timeframe=resolved_timeframe,
                limit=1,
            )
            if recent_runs:
                resolved_symbol = recent_runs[0].get("symbol") or resolved_symbol
                resolved_timeframe = recent_runs[0].get("timeframe") or resolved_timeframe
                resolved_strategy_version = recent_runs[0].get("strategy_version") or resolved_strategy_version

        return {
            "symbol": resolved_symbol,
            "timeframe": resolved_timeframe,
            "strategy_version": resolved_strategy_version,
            "active_profile": active_profile,
        }

    def _evaluate_alignment_state(
        self,
        sample_metrics: Dict[str, Any],
        baseline_metrics: Dict[str, Any],
        label: str,
    ) -> Dict[str, Any]:
        sample_trade_count = int(sample_metrics.get("trade_count", 0) or 0)
        baseline_trade_count = int(baseline_metrics.get("baseline_trade_count", 0) or 0)
        sample_pf = float(sample_metrics.get("profit_factor", 0.0) or 0.0)
        sample_expectancy = float(sample_metrics.get("expectancy_pct", 0.0) or 0.0)
        sample_win_rate = float(sample_metrics.get("win_rate", 0.0) or 0.0)
        baseline_pf = float(baseline_metrics.get("baseline_profit_factor", 0.0) or 0.0)
        baseline_expectancy = float(baseline_metrics.get("baseline_expectancy_pct", 0.0) or 0.0)
        baseline_win_rate = float(baseline_metrics.get("baseline_win_rate", 0.0) or 0.0)
        avg_giveback = float(sample_metrics.get("avg_profit_giveback_pct", 0.0) or 0.0)

        pf_alignment_pct = round((sample_pf / baseline_pf) * 100, 2) if baseline_pf > 0 else 0.0
        if abs(baseline_expectancy) > 1e-9:
            expectancy_alignment_pct = round((sample_expectancy / baseline_expectancy) * 100, 2)
        elif sample_expectancy >= 0:
            expectancy_alignment_pct = 100.0
        else:
            expectancy_alignment_pct = 0.0
        win_rate_delta_pct = round(sample_win_rate - baseline_win_rate, 2)

        notes: List[str] = []
        if sample_trade_count < ProductionConfig.GOVERNANCE_MIN_ALIGNMENT_TRADES:
            notes.append(f"{label}_insufficient_sample")
        if baseline_trade_count < ProductionConfig.GOVERNANCE_MIN_REGIME_TRADES:
            notes.append(f"{label}_baseline_small_sample")

        if notes:
            return {
                "status": "insufficient",
                "pf_alignment_pct": pf_alignment_pct,
                "expectancy_alignment_pct": expectancy_alignment_pct,
                "win_rate_delta_pct": win_rate_delta_pct,
                "notes": notes,
            }

        status = "aligned"
        if sample_pf < max(1.0, baseline_pf * ProductionConfig.GOVERNANCE_ALIGNMENT_BROKEN_PF_MULTIPLIER):
            status = "broken"
            notes.append(f"{label}_profit_factor_broken")
        elif sample_pf < max(1.0, baseline_pf * ProductionConfig.GOVERNANCE_ALIGNMENT_WARNING_PF_MULTIPLIER):
            status = "warning"
            notes.append(f"{label}_profit_factor_warning")

        if baseline_expectancy > 0:
            if sample_expectancy < baseline_expectancy * ProductionConfig.GOVERNANCE_ALIGNMENT_BROKEN_EXPECTANCY_MULTIPLIER:
                status = "broken"
                notes.append(f"{label}_expectancy_broken")
            elif sample_expectancy < baseline_expectancy * ProductionConfig.GOVERNANCE_ALIGNMENT_WARNING_EXPECTANCY_MULTIPLIER and status == "aligned":
                status = "warning"
                notes.append(f"{label}_expectancy_warning")
        elif sample_expectancy < 0 and status == "aligned":
            status = "degraded"
            notes.append(f"{label}_negative_expectancy")

        if baseline_win_rate > 0:
            if sample_win_rate < baseline_win_rate - ProductionConfig.GOVERNANCE_ALIGNMENT_BROKEN_WINRATE_GAP:
                status = "broken"
                notes.append(f"{label}_winrate_broken")
            elif sample_win_rate < baseline_win_rate - ProductionConfig.GOVERNANCE_ALIGNMENT_WARNING_WINRATE_GAP and status == "aligned":
                status = "warning"
                notes.append(f"{label}_winrate_warning")

        if avg_giveback >= ProductionConfig.GOVERNANCE_MAX_PROFIT_GIVEBACK_BLOCK_PCT:
            status = "broken"
            notes.append(f"{label}_profit_giveback_broken")
        elif avg_giveback >= ProductionConfig.GOVERNANCE_MAX_PROFIT_GIVEBACK_WARNING_PCT and status == "aligned":
            status = "degraded"
            notes.append(f"{label}_profit_giveback_warning")

        if sample_pf < 1.0 and status == "warning":
            status = "degraded"
            notes.append(f"{label}_profit_factor_below_one")

        return {
            "status": status,
            "pf_alignment_pct": pf_alignment_pct,
            "expectancy_alignment_pct": expectancy_alignment_pct,
            "win_rate_delta_pct": win_rate_delta_pct,
            "notes": notes,
        }

    def _build_alignment_snapshot(
        self,
        symbol: str,
        timeframe: str,
        strategy_version: str,
        current_regime: Optional[str] = None,
    ) -> Dict[str, Any]:
        baselines = self.get_setup_regime_baselines(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            refresh=True,
        )
        backtest_summary = self.get_backtest_performance_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )

        current_baseline = None
        if current_regime:
            for baseline in baselines:
                if baseline.get("regime") == current_regime:
                    current_baseline = baseline
                    break

        use_oos = float(backtest_summary.get("avg_out_of_sample_profit_factor", 0.0) or 0.0) > 0
        overall_baseline = {
            "baseline_source": "oos" if use_oos else "backtest",
            "baseline_profit_factor": float(backtest_summary.get("avg_out_of_sample_profit_factor" if use_oos else "avg_profit_factor", 0.0) or 0.0),
            "baseline_expectancy_pct": float(backtest_summary.get("avg_out_of_sample_expectancy_pct" if use_oos else "avg_expectancy_pct", 0.0) or 0.0),
            "baseline_win_rate": float(backtest_summary.get("aggregate_win_rate", backtest_summary.get("avg_win_rate", 0.0)) or 0.0),
            "baseline_trade_count": int(backtest_summary.get("aggregate_total_trades", backtest_summary.get("total_trades", 0)) or 0),
        }
        baseline_metrics = current_baseline or overall_baseline

        paper_rows = self._get_recent_runtime_trade_rows(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            regime=current_regime,
            sample_type="paper",
            window_days=ProductionConfig.GOVERNANCE_LOOKBACK_DAYS,
            max_trades=ProductionConfig.GOVERNANCE_LOOKBACK_TRADES,
        )
        paper_scope = "paper_regime"
        if current_regime and len(paper_rows) < ProductionConfig.GOVERNANCE_MIN_ALIGNMENT_TRADES:
            paper_rows = self._get_recent_runtime_trade_rows(
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                regime=None,
                sample_type="paper",
                window_days=ProductionConfig.GOVERNANCE_LOOKBACK_DAYS,
                max_trades=ProductionConfig.GOVERNANCE_LOOKBACK_TRADES,
            )
            paper_scope = "paper_overall"
        paper_metrics = self._summarize_result_rows(paper_rows, "result_pct")
        paper_alignment = self._evaluate_alignment_state(paper_metrics, baseline_metrics, paper_scope)

        live_rows = self._get_recent_runtime_trade_rows(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            regime=current_regime,
            sample_type="live",
            window_days=ProductionConfig.GOVERNANCE_LOOKBACK_DAYS,
            max_trades=ProductionConfig.GOVERNANCE_LOOKBACK_TRADES,
        )
        live_scope = "live_regime"
        if current_regime and len(live_rows) < ProductionConfig.GOVERNANCE_MIN_ALIGNMENT_TRADES:
            live_rows = self._get_recent_runtime_trade_rows(
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                regime=None,
                sample_type="live",
                window_days=ProductionConfig.GOVERNANCE_LOOKBACK_DAYS,
                max_trades=ProductionConfig.GOVERNANCE_LOOKBACK_TRADES,
            )
            live_scope = "live_overall"
        live_metrics = self._summarize_result_rows(live_rows, "result_pct")
        live_reference = baseline_metrics
        if paper_metrics.get("trade_count", 0) >= ProductionConfig.GOVERNANCE_MIN_ALIGNMENT_TRADES:
            live_reference = {
                "baseline_profit_factor": paper_metrics.get("profit_factor", 0.0),
                "baseline_expectancy_pct": paper_metrics.get("expectancy_pct", 0.0),
                "baseline_win_rate": paper_metrics.get("win_rate", 0.0),
                "baseline_trade_count": paper_metrics.get("trade_count", 0),
            }
        live_alignment = self._evaluate_alignment_state(live_metrics, live_reference, live_scope)

        severity_rank = {"aligned": 0, "insufficient": 1, "warning": 2, "degraded": 3, "broken": 4}
        alignment_status = paper_alignment["status"]
        if live_metrics.get("trade_count", 0) <= 0:
            live_alignment["status"] = "unavailable"
            live_alignment["notes"] = []
        elif severity_rank.get(live_alignment["status"], 0) > severity_rank.get(alignment_status, 0):
            alignment_status = live_alignment["status"]

        notes = list(paper_alignment.get("notes", [])) + list(live_alignment.get("notes", []))
        return {
            "current_regime": current_regime,
            "baseline_metrics": baseline_metrics,
            "paper_metrics": paper_metrics,
            "live_metrics": live_metrics,
            "paper_alignment": paper_alignment,
            "live_alignment": live_alignment,
            "alignment_status": alignment_status,
            "notes": notes,
            "window_days": int(ProductionConfig.GOVERNANCE_LOOKBACK_DAYS),
            "window_trades": int(ProductionConfig.GOVERNANCE_LOOKBACK_TRADES),
        }

    def _persist_alignment_snapshot(
        self,
        symbol: str,
        timeframe: str,
        strategy_version: str,
        snapshot: Dict[str, Any],
    ) -> None:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO alignment_metrics (
                    symbol, timeframe, strategy_version, regime, window_days, window_trades,
                    baseline_source, baseline_profit_factor, baseline_expectancy_pct, baseline_win_rate,
                    baseline_trade_count, paper_profit_factor, paper_expectancy_pct, paper_win_rate,
                    paper_trade_count, live_profit_factor, live_expectancy_pct, live_win_rate, live_trade_count,
                    paper_pf_alignment_pct, paper_expectancy_alignment_pct, paper_win_rate_delta_pct,
                    live_pf_alignment_pct, live_expectancy_alignment_pct, live_win_rate_delta_pct,
                    alignment_status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    symbol,
                    timeframe,
                    strategy_version,
                    snapshot.get("current_regime"),
                    snapshot.get("window_days", 0),
                    snapshot.get("window_trades", 0),
                    snapshot.get("baseline_metrics", {}).get("baseline_source"),
                    snapshot.get("baseline_metrics", {}).get("baseline_profit_factor", 0.0),
                    snapshot.get("baseline_metrics", {}).get("baseline_expectancy_pct", 0.0),
                    snapshot.get("baseline_metrics", {}).get("baseline_win_rate", 0.0),
                    snapshot.get("baseline_metrics", {}).get("baseline_trade_count", 0),
                    snapshot.get("paper_metrics", {}).get("profit_factor", 0.0),
                    snapshot.get("paper_metrics", {}).get("expectancy_pct", 0.0),
                    snapshot.get("paper_metrics", {}).get("win_rate", 0.0),
                    snapshot.get("paper_metrics", {}).get("trade_count", 0),
                    snapshot.get("live_metrics", {}).get("profit_factor", 0.0),
                    snapshot.get("live_metrics", {}).get("expectancy_pct", 0.0),
                    snapshot.get("live_metrics", {}).get("win_rate", 0.0),
                    snapshot.get("live_metrics", {}).get("trade_count", 0),
                    snapshot.get("paper_alignment", {}).get("pf_alignment_pct", 0.0),
                    snapshot.get("paper_alignment", {}).get("expectancy_alignment_pct", 0.0),
                    snapshot.get("paper_alignment", {}).get("win_rate_delta_pct", 0.0),
                    snapshot.get("live_alignment", {}).get("pf_alignment_pct", 0.0),
                    snapshot.get("live_alignment", {}).get("expectancy_alignment_pct", 0.0),
                    snapshot.get("live_alignment", {}).get("win_rate_delta_pct", 0.0),
                    snapshot.get("alignment_status"),
                    json.dumps(snapshot.get("notes", [])),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_alignment_metrics(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM alignment_metrics
                WHERE (? IS NULL OR symbol = ?)
                  AND (? IS NULL OR timeframe = ?)
                  AND (? IS NULL OR strategy_version = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                ''',
                (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row["notes"] = self._decode_json_field(row.get("notes"), [])
            return rows
        finally:
            conn.close()

    def _persist_governance_decision(self, decision: Dict[str, Any]) -> None:
        if not decision.get("symbol") or not decision.get("timeframe") or not decision.get("strategy_version"):
            return

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT governance_status, governance_mode
                FROM governance_decisions
                WHERE symbol = ? AND timeframe = ? AND strategy_version = ?
                ORDER BY id DESC
                LIMIT 1
                ''',
                (decision["symbol"], decision["timeframe"], decision["strategy_version"]),
            )
            previous = cursor.fetchone()
            previous_status = previous["governance_status"] if previous else None
            previous_mode = previous["governance_mode"] if previous else None

            cursor.execute(
                '''
                INSERT INTO governance_decisions (
                    symbol, timeframe, strategy_version, regime, governance_status, governance_mode,
                    current_regime_status, alignment_status, promotion_status, degradation_status,
                    action, action_reason, allowed_regimes, reduced_regimes, blocked_regimes, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    decision["symbol"],
                    decision["timeframe"],
                    decision["strategy_version"],
                    decision.get("current_regime"),
                    decision.get("governance_status"),
                    decision.get("governance_mode"),
                    decision.get("current_regime_status"),
                    decision.get("alignment_status"),
                    decision.get("promotion_status"),
                    decision.get("degradation_status"),
                    decision.get("action"),
                    decision.get("action_reason"),
                    json.dumps(decision.get("allowed_regimes", [])),
                    json.dumps(decision.get("reduced_regimes", [])),
                    json.dumps(decision.get("blocked_regimes", [])),
                    json.dumps(decision.get("notes", [])),
                ),
            )

            if previous_status != decision.get("governance_status") or previous_mode != decision.get("governance_mode"):
                cursor.execute(
                    '''
                    INSERT INTO setup_governance_history (
                        symbol, timeframe, strategy_version, regime, previous_status, previous_mode,
                        governance_status, governance_mode, alignment_status, promotion_status,
                        degradation_status, action, action_reason, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        decision["symbol"],
                        decision["timeframe"],
                        decision["strategy_version"],
                        decision.get("current_regime"),
                        previous_status,
                        previous_mode,
                        decision.get("governance_status"),
                        decision.get("governance_mode"),
                        decision.get("alignment_status"),
                        decision.get("promotion_status"),
                        decision.get("degradation_status"),
                        decision.get("action"),
                        decision.get("action_reason"),
                        json.dumps(decision.get("notes", [])),
                    ),
                )

            conn.commit()
        finally:
            conn.close()

    def get_governance_history(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT *
                FROM setup_governance_history
                WHERE (? IS NULL OR symbol = ?)
                  AND (? IS NULL OR timeframe = ?)
                  AND (? IS NULL OR strategy_version = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                ''',
                (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row["notes"] = self._decode_json_field(row.get("notes"), [])
            return rows
        finally:
            conn.close()

    def evaluate_strategy_governance(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        current_regime: str = None,
        persist: bool = False,
    ) -> Dict[str, Any]:
        identity = self._resolve_governance_identity(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        symbol = identity.get("symbol")
        timeframe = identity.get("timeframe")
        strategy_version = identity.get("strategy_version")
        active_profile = identity.get("active_profile")

        if not symbol or not timeframe:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy_version": strategy_version,
                "governance_status": "research",
                "governance_mode": "blocked",
                "allowed_regimes": [],
                "reduced_regimes": [],
                "blocked_regimes": [],
                "alignment_status": "insufficient",
                "promotion_status": "insufficient_sample",
                "degradation_status": "none",
                "action": "block",
                "action_reason": "runtime_governance: setup_identity_unresolved",
                "notes": ["setup_identity_unresolved"],
            }

        backtest_summary = self.get_backtest_performance_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        paper_summary = self.get_paper_trade_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        edge_summary = self.get_edge_monitor_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        baselines = self.refresh_setup_regime_baselines(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            persist=persist,
        ) if strategy_version else []

        allowed_regimes = [row["regime"] for row in baselines if row.get("performance_status") == "approved"]
        reduced_regimes = [row["regime"] for row in baselines if row.get("performance_status") == "reduced"]
        blocked_regimes = [row["regime"] for row in baselines if row.get("performance_status") == "blocked"]

        current_regime = self._normalize_governance_regime(current_regime)
        current_regime_status = "unknown"
        if current_regime in allowed_regimes:
            current_regime_status = "approved"
        elif current_regime in reduced_regimes:
            current_regime_status = "reduced"
        elif current_regime in blocked_regimes or current_regime == "parabolic":
            current_regime_status = "blocked"

        alignment = self._build_alignment_snapshot(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            current_regime=current_regime,
        ) if strategy_version else {
            "alignment_status": "insufficient",
            "notes": ["alignment_unavailable"],
            "baseline_metrics": {},
            "paper_metrics": {},
            "live_metrics": {},
            "paper_alignment": {},
            "live_alignment": {},
        }
        alignment_status = alignment.get("alignment_status", "insufficient")

        total_backtest_trades = int(backtest_summary.get("aggregate_total_trades", backtest_summary.get("total_trades", 0)) or 0)
        has_backtest = total_backtest_trades > 0
        quality_score = self._compute_strategy_quality_score(
            backtest_summary=backtest_summary,
            paper_summary=paper_summary,
            edge_summary=edge_summary,
        )

        latest_run = None
        promotion_ready = False
        if strategy_version:
            recent_runs = self.get_backtest_runs(
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                limit=1,
            )
            latest_run = recent_runs[0] if recent_runs else None
        if latest_run:
            readiness = self.get_backtest_run_promotion_readiness(latest_run["id"])
            promotion_ready = bool(readiness.get("ready"))
        elif has_backtest:
            promotion_ready = (
                total_backtest_trades >= ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION
                and float(backtest_summary.get("avg_profit_factor", 0.0) or 0.0) >= ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR
            )

        governance_status = "research"
        governance_mode = "blocked"
        promotion_status = "insufficient_sample"
        degradation_status = "none"
        action = "block"
        action_reason = "runtime_governance: insufficient_sample"
        notes = list(alignment.get("notes", []))

        if not has_backtest:
            governance_status = "research"
            promotion_status = "insufficient_sample"
            action_reason = "runtime_governance: no_backtest"
            notes.append("no_backtest")
        elif not active_profile:
            if promotion_ready:
                governance_status = "candidate"
                promotion_status = "eligible"
                action_reason = "runtime_governance: candidate_waiting_activation"
                notes.append("candidate_waiting_activation")
            else:
                governance_status = "research"
                promotion_status = "not_ready"
                action_reason = "runtime_governance: setup_not_ready_for_activation"
                notes.append("setup_not_ready_for_activation")
        else:
            promotion_status = "approved" if promotion_ready else "active"
            if current_regime_status == "blocked":
                governance_status = "suspended" if current_regime == "parabolic" else "blocked"
                governance_mode = "blocked"
                action = "block"
                action_reason = "regime_not_approved"
                notes.append(f"regime_not_approved:{current_regime}")
            elif alignment_status == "broken":
                governance_status = "blocked"
                governance_mode = "blocked"
                degradation_status = "broken"
                action = "block"
                action_reason = "live_degradation"
            elif alignment_status == "degraded":
                governance_status = "degraded"
                governance_mode = "blocked"
                degradation_status = "degraded"
                action = "block"
                action_reason = "setup_degraded"
            elif current_regime_status == "reduced" or alignment_status == "warning":
                governance_status = "reduced"
                governance_mode = "reduced"
                degradation_status = "warning" if alignment_status == "warning" else "none"
                action = "reduce"
                action_reason = "paper_alignment_warning" if alignment_status == "warning" else "regime_reduced"
            elif alignment_status == "insufficient":
                governance_status = "observing"
                governance_mode = "reduced"
                action = "observe"
                action_reason = "paper_alignment_insufficient"
            else:
                governance_status = "approved"
                governance_mode = "normal"
                action = "allow"
                action_reason = "approved_for_runtime"

            if quality_score < ProductionConfig.MIN_LIVE_QUALITY_SCORE and governance_mode != "blocked":
                governance_status = "reduced"
                governance_mode = "reduced"
                action = "reduce"
                action_reason = "quality_score_warning"
                notes.append("quality_score_warning")

        decision = {
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_version": strategy_version,
            "active_profile": active_profile,
            "current_regime": current_regime,
            "current_regime_status": current_regime_status,
            "governance_status": governance_status,
            "governance_mode": governance_mode,
            "allowed_regimes": allowed_regimes,
            "reduced_regimes": reduced_regimes,
            "blocked_regimes": blocked_regimes,
            "alignment_status": alignment_status,
            "promotion_status": promotion_status,
            "degradation_status": degradation_status,
            "action": action,
            "action_reason": action_reason,
            "notes": notes,
            "quality_score": round(float(quality_score or 0.0), 2),
            "edge_status": edge_summary.get("status"),
            "baseline_regimes": baselines,
            "alignment_metrics": alignment,
            "governance_reduction_multiplier": float(ProductionConfig.GOVERNANCE_REDUCED_SIZE_MULTIPLIER),
        }

        if persist and strategy_version:
            self._persist_alignment_snapshot(symbol, timeframe, strategy_version, alignment)
            self._persist_governance_decision(decision)

        return decision

    def get_backtest_performance_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        """Retornar agregados de backtest por simbolo/timeframe."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(SUM(total_trades), 0) AS total_trades,
                COALESCE(AVG(total_return_pct), 0) AS avg_return_pct,
                COALESCE(AVG(win_rate), 0) AS avg_win_rate,
                COALESCE(AVG(profit_factor), 0) AS avg_profit_factor,
                COALESCE(AVG(expectancy_pct), 0) AS avg_expectancy_pct,
                COALESCE(AVG(out_of_sample_return_pct), 0) AS avg_out_of_sample_return_pct,
                COALESCE(AVG(out_of_sample_profit_factor), 0) AS avg_out_of_sample_profit_factor,
                COALESCE(AVG(out_of_sample_expectancy_pct), 0) AS avg_out_of_sample_expectancy_pct,
                COALESCE(SUM(CASE WHEN out_of_sample_passed = 1 THEN 1 ELSE 0 END), 0) AS passed_oos_runs,
                COALESCE(AVG(walk_forward_pass_rate_pct), 0) AS avg_walk_forward_pass_rate_pct,
                COALESCE(AVG(walk_forward_avg_oos_return_pct), 0) AS avg_walk_forward_oos_return_pct,
                COALESCE(AVG(walk_forward_avg_oos_profit_factor), 0) AS avg_walk_forward_oos_profit_factor,
                COALESCE(SUM(CASE WHEN walk_forward_passed = 1 THEN 1 ELSE 0 END), 0) AS passed_walk_forward_runs,
                COALESCE(AVG(max_drawdown), 0) AS avg_max_drawdown,
                COALESCE(SUM(net_profit), 0) AS total_net_profit,
                COALESCE(MAX(total_return_pct), 0) AS best_return_pct,
                COALESCE(MIN(total_return_pct), 0) AS worst_return_pct
            FROM backtest_runs
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
        ''', (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version))
        summary = dict(cursor.fetchone())

        cursor.execute(
            '''
            SELECT
                COUNT(bt.id) AS total_trade_rows,
                COALESCE(SUM(CASE WHEN bt.profit_loss > 0 THEN bt.profit_loss ELSE 0 END), 0) AS gross_profit,
                COALESCE(SUM(CASE WHEN bt.profit_loss < 0 THEN -bt.profit_loss ELSE 0 END), 0) AS gross_loss,
                COALESCE(AVG(bt.profit_loss_pct), 0) AS avg_trade_result_pct,
                COALESCE(SUM(CASE WHEN bt.profit_loss > 0 THEN 1 ELSE 0 END), 0) AS winning_trades
            FROM backtest_trades bt
            JOIN backtest_runs br ON bt.run_id = br.id
            WHERE (? IS NULL OR br.symbol = ?)
              AND (? IS NULL OR br.timeframe = ?)
              AND (? IS NULL OR br.strategy_version = ?)
            ''',
            (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version),
        )
        summary = self._merge_backtest_trade_aggregates(summary, dict(cursor.fetchone()))

        cursor.execute('''
            SELECT
                symbol,
                timeframe,
                COUNT(*) AS total_runs,
                COALESCE(SUM(total_trades), 0) AS total_trades,
                ROUND(COALESCE(AVG(total_return_pct), 0), 2) AS avg_return_pct,
                ROUND(COALESCE(AVG(win_rate), 0), 2) AS avg_win_rate,
                ROUND(COALESCE(AVG(profit_factor), 0), 2) AS avg_profit_factor,
                ROUND(COALESCE(AVG(expectancy_pct), 0), 2) AS avg_expectancy_pct,
                ROUND(COALESCE(AVG(out_of_sample_return_pct), 0), 2) AS avg_out_of_sample_return_pct,
                ROUND(COALESCE(AVG(out_of_sample_profit_factor), 0), 2) AS avg_out_of_sample_profit_factor,
                ROUND(COALESCE(AVG(out_of_sample_expectancy_pct), 0), 2) AS avg_out_of_sample_expectancy_pct,
                COALESCE(SUM(CASE WHEN out_of_sample_passed = 1 THEN 1 ELSE 0 END), 0) AS passed_oos_runs,
                ROUND(COALESCE(AVG(walk_forward_pass_rate_pct), 0), 2) AS avg_walk_forward_pass_rate_pct,
                ROUND(COALESCE(AVG(walk_forward_avg_oos_return_pct), 0), 2) AS avg_walk_forward_oos_return_pct,
                ROUND(COALESCE(AVG(walk_forward_avg_oos_profit_factor), 0), 2) AS avg_walk_forward_oos_profit_factor,
                COALESCE(SUM(CASE WHEN walk_forward_passed = 1 THEN 1 ELSE 0 END), 0) AS passed_walk_forward_runs,
                ROUND(COALESCE(AVG(max_drawdown), 0), 2) AS avg_max_drawdown,
                ROUND(COALESCE(SUM(net_profit), 0), 2) AS total_net_profit
            FROM backtest_runs
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
            GROUP BY symbol, timeframe
            ORDER BY avg_return_pct DESC, total_runs DESC
        ''', (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version))
        breakdown_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            '''
            SELECT
                br.symbol AS symbol,
                br.timeframe AS timeframe,
                COUNT(bt.id) AS total_trade_rows,
                COALESCE(SUM(CASE WHEN bt.profit_loss > 0 THEN bt.profit_loss ELSE 0 END), 0) AS gross_profit,
                COALESCE(SUM(CASE WHEN bt.profit_loss < 0 THEN -bt.profit_loss ELSE 0 END), 0) AS gross_loss,
                COALESCE(AVG(bt.profit_loss_pct), 0) AS avg_trade_result_pct,
                COALESCE(SUM(CASE WHEN bt.profit_loss > 0 THEN 1 ELSE 0 END), 0) AS winning_trades
            FROM backtest_trades bt
            JOIN backtest_runs br ON bt.run_id = br.id
            WHERE (? IS NULL OR br.symbol = ?)
              AND (? IS NULL OR br.timeframe = ?)
              AND (? IS NULL OR br.strategy_version = ?)
            GROUP BY br.symbol, br.timeframe
            ''',
            (symbol, symbol, timeframe, timeframe, strategy_version, strategy_version),
        )
        aggregate_rows = {
            (row["symbol"], row["timeframe"]): dict(row)
            for row in cursor.fetchall()
        }

        for row in breakdown_rows:
            trade_summary = aggregate_rows.get((row["symbol"], row["timeframe"]))
            if trade_summary:
                self._merge_backtest_trade_aggregates(row, trade_summary)

        summary['breakdown_by_market'] = breakdown_rows

        conn.close()
        return summary

    def save_strategy_evaluation(self, evaluation_data: Dict[str, Any]) -> int:
        """Persistir um snapshot consolidado de metrics da estrategia."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            column_values = {
                'symbol': evaluation_data.get('symbol'),
                'timeframe': evaluation_data.get('timeframe'),
                'strategy_version': evaluation_data.get('strategy_version'),
                'evaluation_type': evaluation_data.get('evaluation_type', 'combined'),
                'total_backtest_runs': evaluation_data.get('total_backtest_runs', 0),
                'total_backtest_trades': evaluation_data.get('total_backtest_trades', 0),
                'avg_return_pct': evaluation_data.get('avg_return_pct', 0.0),
                'avg_profit_factor': evaluation_data.get('avg_profit_factor', 0.0),
                'avg_expectancy_pct': evaluation_data.get('avg_expectancy_pct', 0.0),
                'avg_out_of_sample_return_pct': evaluation_data.get('avg_out_of_sample_return_pct', 0.0),
                'avg_out_of_sample_profit_factor': evaluation_data.get('avg_out_of_sample_profit_factor', 0.0),
                'avg_out_of_sample_expectancy_pct': evaluation_data.get('avg_out_of_sample_expectancy_pct', 0.0),
                'passed_oos_runs': evaluation_data.get('passed_oos_runs', 0),
                'avg_walk_forward_pass_rate_pct': evaluation_data.get('avg_walk_forward_pass_rate_pct', 0.0),
                'avg_walk_forward_oos_return_pct': evaluation_data.get('avg_walk_forward_oos_return_pct', 0.0),
                'avg_walk_forward_oos_profit_factor': evaluation_data.get('avg_walk_forward_oos_profit_factor', 0.0),
                'passed_walk_forward_runs': evaluation_data.get('passed_walk_forward_runs', 0),
                'avg_max_drawdown': evaluation_data.get('avg_max_drawdown', 0.0),
                'total_net_profit': evaluation_data.get('total_net_profit', 0.0),
                'paper_closed_trades': evaluation_data.get('paper_closed_trades', 0),
                'paper_win_rate': evaluation_data.get('paper_win_rate', 0.0),
                'paper_avg_result_pct': evaluation_data.get('paper_avg_result_pct', 0.0),
                'paper_total_result_pct': evaluation_data.get('paper_total_result_pct', 0.0),
                'paper_profit_factor': evaluation_data.get('paper_profit_factor', 0.0),
                'baseline_source': evaluation_data.get('baseline_source'),
                'edge_status': evaluation_data.get('edge_status'),
                'governance_status': evaluation_data.get('governance_status'),
                'quality_score': evaluation_data.get('quality_score', 0.0),
                'notes': evaluation_data.get('notes'),
                'created_at_br': format_brazil_time(),
            }
            columns = list(column_values.keys())
            placeholders = ', '.join(['?'] * len(columns))
            cursor.execute(
                f"INSERT INTO strategy_evaluations ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(column_values[column] for column in columns),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_strategy_evaluations(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        evaluation_type: str = None,
        limit: int = 50,
    ) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM strategy_evaluations
            WHERE (? IS NULL OR symbol = ?)
              AND (? IS NULL OR timeframe = ?)
              AND (? IS NULL OR strategy_version = ?)
              AND (? IS NULL OR evaluation_type = ?)
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            ''',
            (
                symbol,
                symbol,
                timeframe,
                timeframe,
                strategy_version,
                strategy_version,
                evaluation_type,
                evaluation_type,
                limit,
            ),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_strategy_evaluation_overview(
        self,
        symbol: str = None,
        timeframe: str = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Retornar o snapshot mais recente por estrategia para dashboard/admin."""
        raw_limit = max(int(limit or 20) * 8, 50)
        evaluations = self.get_strategy_evaluations(
            symbol=symbol,
            timeframe=timeframe,
            limit=raw_limit,
        )

        latest_by_strategy: List[Dict[str, Any]] = []
        seen_keys = set()
        governance_counts: Dict[str, int] = {}
        edge_counts: Dict[str, int] = {}
        evaluation_type_counts: Dict[str, int] = {}

        for evaluation in evaluations:
            key = (
                evaluation.get("symbol"),
                evaluation.get("timeframe"),
                evaluation.get("strategy_version") or f"snapshot:{evaluation.get('id')}",
            )
            if key in seen_keys:
                continue

            seen_keys.add(key)
            latest_by_strategy.append(evaluation)

            governance_status = evaluation.get("governance_status") or "unknown"
            edge_status = evaluation.get("edge_status") or "unknown"
            evaluation_type = evaluation.get("evaluation_type") or "unknown"

            governance_counts[governance_status] = governance_counts.get(governance_status, 0) + 1
            edge_counts[edge_status] = edge_counts.get(edge_status, 0) + 1
            evaluation_type_counts[evaluation_type] = evaluation_type_counts.get(evaluation_type, 0) + 1

            if len(latest_by_strategy) >= limit:
                break

        return {
            "rows": latest_by_strategy,
            "governance_counts": governance_counts,
            "edge_counts": edge_counts,
            "evaluation_type_counts": evaluation_type_counts,
            "total_strategies": len(latest_by_strategy),
            "sampled_snapshots": len(evaluations),
        }

    def compute_strategy_metrics(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        evaluation_type: str = "combined",
        persist: bool = False,
        notes: str = None,
    ) -> Dict[str, Any]:
        """Consolidar metrics de estrategia a partir de backtest + paper + governanca."""
        backtest_summary = self.get_backtest_performance_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        paper_summary = self.get_paper_trade_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        edge_summary = self.get_edge_monitor_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        governance_state = self.evaluate_strategy_governance(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            persist=persist,
        )
        governance_status = governance_state.get("governance_status")
        symbol = governance_state.get("symbol") or symbol
        timeframe = governance_state.get("timeframe") or timeframe
        strategy_version = governance_state.get("strategy_version") or strategy_version

        if symbol is None and backtest_summary.get("breakdown_by_market"):
            symbol = backtest_summary["breakdown_by_market"][0].get("symbol")
        if timeframe is None and backtest_summary.get("breakdown_by_market"):
            timeframe = backtest_summary["breakdown_by_market"][0].get("timeframe")

        quality_score = self._compute_strategy_quality_score(
            backtest_summary=backtest_summary,
            paper_summary=paper_summary,
            edge_summary=edge_summary,
        )

        paper_profit_factor = paper_summary.get('profit_factor', 0.0)
        if paper_profit_factor == float('inf'):
            paper_profit_factor = 999.0

        metrics = {
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_version": strategy_version,
            "evaluation_type": evaluation_type,
            "total_backtest_runs": int(backtest_summary.get('total_runs', 0) or 0),
            "total_backtest_trades": int(backtest_summary.get('total_trades', 0) or 0),
            "avg_return_pct": round(float(backtest_summary.get('avg_return_pct', 0.0) or 0.0), 4),
            "avg_profit_factor": round(float(backtest_summary.get('avg_profit_factor', 0.0) or 0.0), 4),
            "avg_expectancy_pct": round(float(backtest_summary.get('avg_expectancy_pct', 0.0) or 0.0), 4),
            "avg_out_of_sample_return_pct": round(float(backtest_summary.get('avg_out_of_sample_return_pct', 0.0) or 0.0), 4),
            "avg_out_of_sample_profit_factor": round(float(backtest_summary.get('avg_out_of_sample_profit_factor', 0.0) or 0.0), 4),
            "avg_out_of_sample_expectancy_pct": round(float(backtest_summary.get('avg_out_of_sample_expectancy_pct', 0.0) or 0.0), 4),
            "passed_oos_runs": int(backtest_summary.get('passed_oos_runs', 0) or 0),
            "avg_walk_forward_pass_rate_pct": round(float(backtest_summary.get('avg_walk_forward_pass_rate_pct', 0.0) or 0.0), 4),
            "avg_walk_forward_oos_return_pct": round(float(backtest_summary.get('avg_walk_forward_oos_return_pct', 0.0) or 0.0), 4),
            "avg_walk_forward_oos_profit_factor": round(float(backtest_summary.get('avg_walk_forward_oos_profit_factor', 0.0) or 0.0), 4),
            "passed_walk_forward_runs": int(backtest_summary.get('passed_walk_forward_runs', 0) or 0),
            "avg_max_drawdown": round(float(backtest_summary.get('avg_max_drawdown', 0.0) or 0.0), 4),
            "total_net_profit": round(float(backtest_summary.get('total_net_profit', 0.0) or 0.0), 4),
            "paper_closed_trades": int(paper_summary.get('closed_trades', 0) or 0),
            "paper_win_rate": round(float(paper_summary.get('win_rate', 0.0) or 0.0), 4),
            "paper_avg_result_pct": round(float(paper_summary.get('avg_result_pct', 0.0) or 0.0), 4),
            "paper_total_result_pct": round(float(paper_summary.get('total_result_pct', 0.0) or 0.0), 4),
            "paper_profit_factor": round(float(paper_profit_factor or 0.0), 4),
            "baseline_source": edge_summary.get('baseline_source'),
            "edge_status": edge_summary.get('status'),
            "governance_status": governance_status or "unknown",
            "governance_mode": governance_state.get("governance_mode"),
            "alignment_status": governance_state.get("alignment_status"),
            "quality_score": quality_score,
            "notes": notes,
        }

        if persist:
            metrics["evaluation_id"] = self.save_strategy_evaluation(metrics)

        return metrics

    def _compute_strategy_quality_score(
        self,
        backtest_summary: Dict[str, Any],
        paper_summary: Dict[str, Any],
        edge_summary: Dict[str, Any],
    ) -> float:
        score = 0.0

        score += min(max(float(backtest_summary.get('avg_profit_factor', 0.0) or 0.0) - 1.0, 0.0), 1.5) * 18
        score += min(max(float(backtest_summary.get('avg_out_of_sample_profit_factor', 0.0) or 0.0) - 1.0, 0.0), 1.5) * 22
        score += min(max(float(backtest_summary.get('avg_expectancy_pct', 0.0) or 0.0), 0.0), 5.0) * 4
        score += min(max(float(backtest_summary.get('avg_out_of_sample_expectancy_pct', 0.0) or 0.0), 0.0), 5.0) * 5
        score += min(max(float(backtest_summary.get('avg_walk_forward_pass_rate_pct', 0.0) or 0.0), 0.0), 100.0) * 0.16
        score += min(max(float(paper_summary.get('profit_factor', 0.0) or 0.0) - 1.0, 0.0), 1.5) * 20
        score += min(max(float(paper_summary.get('avg_result_pct', 0.0) or 0.0), 0.0), 3.0) * 5

        if edge_summary.get('status') == 'aligned':
            score += 10
        elif edge_summary.get('status') == 'watchlist':
            score += 4
        elif edge_summary.get('status') == 'degraded':
            score -= 12

        score -= min(max(float(backtest_summary.get('avg_max_drawdown', 0.0) or 0.0), 0.0), 30.0) * 1.1

        total_trades = int(backtest_summary.get('total_trades', 0) or 0)
        min_backtest_trades = max(int(ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION), 1)
        if total_trades < 5:
            score -= 45
        elif total_trades < 10:
            score -= 35
        elif total_trades < 25:
            score -= 22
        elif total_trades < min_backtest_trades:
            score -= 12

        if total_trades < min_backtest_trades:
            score = min(score, max(0.0, float(ProductionConfig.MIN_LIVE_QUALITY_SCORE) - 5.0))

        return round(max(0.0, min(100.0, score)), 2)

    def get_edge_monitor_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        """Comparar baseline de backtest com performance paper/live do mesmo mercado."""
        active_profile = None
        if strategy_version is None and symbol and timeframe:
            active_profile = self.get_active_strategy_profile(symbol=symbol, timeframe=timeframe)
            if active_profile:
                strategy_version = active_profile.get('strategy_version')

        backtest_summary = self.get_backtest_performance_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        paper_summary = self.get_paper_trade_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )

        has_backtest = int(backtest_summary.get('total_runs', 0) or 0) > 0
        has_live_trades = int(paper_summary.get('closed_trades', 0) or 0) > 0

        use_oos_baseline = has_backtest and (
            float(backtest_summary.get('avg_out_of_sample_profit_factor', 0.0) or 0.0) > 0
            or int(backtest_summary.get('passed_oos_runs', 0) or 0) > 0
        )

        baseline_label = "OOS" if use_oos_baseline else "Backtest"
        baseline_return_pct = float(
            backtest_summary.get('avg_out_of_sample_return_pct' if use_oos_baseline else 'avg_return_pct', 0.0) or 0.0
        )
        baseline_profit_factor = float(
            backtest_summary.get('avg_out_of_sample_profit_factor' if use_oos_baseline else 'avg_profit_factor', 0.0) or 0.0
        )
        baseline_expectancy_pct = float(
            backtest_summary.get('avg_out_of_sample_expectancy_pct' if use_oos_baseline else 'avg_expectancy_pct', 0.0) or 0.0
        )

        paper_profit_factor = paper_summary.get('profit_factor', 0.0)
        if paper_profit_factor == float('inf'):
            paper_profit_factor = 999.0
        paper_profit_factor = float(paper_profit_factor or 0.0)
        paper_avg_result_pct = float(paper_summary.get('avg_result_pct', 0.0) or 0.0)
        paper_total_result_pct = float(paper_summary.get('total_result_pct', 0.0) or 0.0)
        closed_trades = int(paper_summary.get('closed_trades', 0) or 0)

        if baseline_profit_factor > 0:
            profit_factor_alignment_pct = round((paper_profit_factor / baseline_profit_factor) * 100, 2)
        else:
            profit_factor_alignment_pct = 0.0

        expectancy_gap_pct = round(paper_avg_result_pct - baseline_expectancy_pct, 4)
        return_gap_pct = round(paper_total_result_pct - baseline_return_pct, 4)

        if not has_backtest:
            status = "no_backtest"
            status_message = "Sem baseline de backtest para comparar o edge live."
        elif not has_live_trades:
            status = "awaiting_live_data"
            status_message = "Aguardando paper trades fechados para validar o edge live."
        elif closed_trades < ProductionConfig.MIN_PAPER_TRADES_FOR_EDGE_VALIDATION:
            status = "insufficient_live_data"
            status_message = (
                "Amostra paper ainda pequena para concluir sobre degradacao. "
                f"Minimo atual: {ProductionConfig.MIN_PAPER_TRADES_FOR_EDGE_VALIDATION} trades fechados."
            )
        else:
            paper_pf_floor = max(1.0, baseline_profit_factor * 0.85) if baseline_profit_factor > 0 else 1.0
            expectancy_floor = baseline_expectancy_pct * 0.7 if baseline_expectancy_pct > 0 else baseline_expectancy_pct - 0.1

            if paper_profit_factor < 1.0 or paper_total_result_pct <= 0 or (
                baseline_profit_factor > 0 and paper_profit_factor < baseline_profit_factor * 0.8
            ):
                status = "degraded"
                status_message = "Paper trade abaixo do baseline. Edge em degradacao."
            elif paper_profit_factor >= paper_pf_floor and paper_avg_result_pct >= expectancy_floor:
                status = "aligned"
                status_message = "Paper trade alinhado com o baseline. Edge live sustentado."
            else:
                status = "watchlist"
                status_message = "Paper trade ainda inconclusivo. Continuar monitorando."

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_version": strategy_version,
            "baseline_source": baseline_label,
            "baseline_return_pct": round(baseline_return_pct, 4),
            "baseline_profit_factor": round(baseline_profit_factor, 4),
            "baseline_expectancy_pct": round(baseline_expectancy_pct, 4),
            "paper_closed_trades": closed_trades,
            "paper_win_rate": round(float(paper_summary.get('win_rate', 0.0) or 0.0), 2),
            "paper_avg_result_pct": round(paper_avg_result_pct, 4),
            "paper_total_result_pct": round(paper_total_result_pct, 4),
            "paper_profit_factor": round(paper_profit_factor, 4),
            "profit_factor_alignment_pct": profit_factor_alignment_pct,
            "expectancy_gap_pct": expectancy_gap_pct,
            "return_gap_pct": return_gap_pct,
            "status": status,
            "status_message": status_message,
            "has_backtest": has_backtest,
            "has_live_trades": has_live_trades,
            "active_profile": active_profile,
        }

    def get_strategy_governance_summary(
        self,
        symbol: str = None,
        timeframe: str = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> Dict[str, Any]:
        status_filter = 'active' if active_only else None
        profiles = self.get_strategy_profiles(
            symbol=symbol,
            timeframe=timeframe,
            status=status_filter,
            limit=limit,
        )

        rows = []
        counts = {
            "approved": 0,
            "observing": 0,
            "reduced": 0,
            "blocked": 0,
            "degraded": 0,
            "suspended": 0,
            "candidate": 0,
            "research": 0,
            "ready_for_paper": 0,
            "needs_work": 0,
            "disabled": 0,
        }

        for profile in profiles:
            readiness = None
            if profile.get('source_run_id'):
                readiness = self.get_backtest_run_promotion_readiness(profile['source_run_id'])
            adaptive = self.evaluate_strategy_governance(
                symbol=profile.get('symbol'),
                timeframe=profile.get('timeframe'),
                strategy_version=profile.get('strategy_version'),
                persist=False,
            )
            edge_summary = self.get_edge_monitor_summary(
                symbol=profile.get('symbol'),
                timeframe=profile.get('timeframe'),
                strategy_version=profile.get('strategy_version'),
            )

            governance_status = adaptive.get("governance_status", "observing")
            governance_message = adaptive.get("action_reason") or "Setup em governanca adaptativa."
            if profile.get('status') == 'disabled':
                governance_status = "disabled"
                governance_message = "Setup desativado."
            elif profile.get('status') != 'active':
                if readiness and readiness.get('ready'):
                    governance_status = "candidate"
                    governance_message = "Setup elegivel para ativacao em paper."
                    counts["ready_for_paper"] = counts.get("ready_for_paper", 0) + 1
                else:
                    governance_status = "research"
                    governance_message = "Setup ainda nao atingiu os criterios minimos."
                    counts["needs_work"] = counts.get("needs_work", 0) + 1

            counts[governance_status] = counts.get(governance_status, 0) + 1
            rows.append(
                {
                    "profile_id": profile.get('id'),
                    "symbol": profile.get('symbol'),
                    "timeframe": profile.get('timeframe'),
                    "strategy_version": profile.get('strategy_version'),
                    "profile_status": profile.get('status'),
                    "governance_status": governance_status,
                    "governance_message": governance_message,
                    "governance_mode": adaptive.get("governance_mode"),
                    "alignment_status": adaptive.get("alignment_status"),
                    "allowed_regimes": adaptive.get("allowed_regimes", []),
                    "blocked_regimes": adaptive.get("blocked_regimes", []),
                    "source_run_id": profile.get('source_run_id'),
                    "paper_closed_trades": edge_summary.get('paper_closed_trades', 0),
                    "baseline_profit_factor": edge_summary.get('baseline_profit_factor', 0.0),
                    "paper_profit_factor": edge_summary.get('paper_profit_factor', 0.0),
                    "edge_status": edge_summary.get('status'),
                    "readiness_ready": bool(readiness.get('ready')) if readiness else None,
                    "readiness_reasons": readiness.get('reasons', []) if readiness else [],
                    "updated_at_br": profile.get('updated_at_br') or profile.get('created_at_br'),
                }
            )

        return {
            "profiles": rows,
            "counts": counts,
            "total_profiles": len(rows),
        }

    def get_live_execution_readiness(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict[str, Any]:
        """Determinar se um setup esta objetivamente apto para execucao live."""
        resolved_symbol = symbol
        resolved_timeframe = timeframe
        resolved_strategy_version = strategy_version
        active_profile = None

        if resolved_symbol and resolved_timeframe:
            active_profile = self.get_active_strategy_profile(resolved_symbol, resolved_timeframe)
            if active_profile and resolved_strategy_version is None:
                resolved_strategy_version = active_profile.get("strategy_version")

        if active_profile is None and resolved_strategy_version is not None:
            profiles = self.get_strategy_profiles(
                symbol=resolved_symbol,
                timeframe=resolved_timeframe,
                status="active",
                limit=100,
            )
            for profile in profiles:
                if profile.get("strategy_version") != resolved_strategy_version:
                    continue
                active_profile = profile
                resolved_symbol = profile.get("symbol") or resolved_symbol
                resolved_timeframe = profile.get("timeframe") or resolved_timeframe
                break

        governance_state = self.evaluate_strategy_governance(
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            strategy_version=resolved_strategy_version,
            persist=False,
        )
        resolved_strategy_version = governance_state.get("strategy_version") or resolved_strategy_version

        metrics = self.compute_strategy_metrics(
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            strategy_version=resolved_strategy_version,
            persist=False,
        )

        governance_status = governance_state.get("governance_status") or metrics.get("governance_status") or "unknown"
        governance_mode = governance_state.get("governance_mode") or "blocked"
        edge_status = metrics.get("edge_status") or "unknown"
        paper_closed_trades = int(metrics.get("paper_closed_trades", 0) or 0)
        quality_score = float(metrics.get("quality_score", 0.0) or 0.0)
        reasons = []

        if not ProductionConfig.ENABLE_LIVE_EXECUTION:
            reasons.append("Execucao live desabilitada por configuracao.")

        if not active_profile:
            reasons.append("Nenhum setup ativo encontrado para execucao live.")

        if ProductionConfig.REQUIRE_APPROVED_GOVERNANCE_FOR_LIVE and governance_status != "approved":
            reasons.append(f"Governanca atual do setup: {governance_status}/{governance_mode}.")

        if edge_status != "aligned":
            reasons.append(f"Edge monitor atual: {edge_status}.")

        if paper_closed_trades < int(ProductionConfig.MIN_PAPER_TRADES_FOR_EDGE_VALIDATION):
            reasons.append(
                "Amostra paper insuficiente para live "
                f"({paper_closed_trades}/{ProductionConfig.MIN_PAPER_TRADES_FOR_EDGE_VALIDATION})."
            )

        if quality_score < float(ProductionConfig.MIN_LIVE_QUALITY_SCORE):
            reasons.append(
                f"Quality score abaixo do minimo ({quality_score:.2f}/{ProductionConfig.MIN_LIVE_QUALITY_SCORE:.2f})."
            )

        allowed = not reasons
        message = (
            "Setup apto para execucao live."
            if allowed
            else "Execucao live bloqueada: " + " ".join(reasons)
        )

        return {
            "allowed": allowed,
            "message": message,
            "reasons": reasons,
            "symbol": resolved_symbol,
            "timeframe": resolved_timeframe,
            "strategy_version": resolved_strategy_version,
            "active_profile": active_profile,
            "governance_status": governance_status,
            "governance_mode": governance_mode,
            "edge_status": edge_status,
            "paper_closed_trades": paper_closed_trades,
            "quality_score": round(quality_score, 2),
            "required_quality_score": float(ProductionConfig.MIN_LIVE_QUALITY_SCORE),
            "config_enabled": bool(ProductionConfig.ENABLE_LIVE_EXECUTION),
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Buscar estatísticas gerais"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        stats = {}
        
        # Total de sinais
        cursor.execute('SELECT COUNT(*) as total FROM trading_signals')
        stats['total_signals'] = cursor.fetchone()['total']
        
        # Sinais por tipo
        cursor.execute('''
            SELECT signal_type, COUNT(*) as count 
            FROM trading_signals 
            GROUP BY signal_type
        ''')
        signal_types = {row['signal_type']: row['count'] for row in cursor.fetchall()}
        stats['signal_types'] = signal_types
        
        # Sinais por símbolo
        cursor.execute('''
            SELECT symbol, COUNT(*) as count 
            FROM trading_signals 
            GROUP BY symbol 
            ORDER BY count DESC 
            LIMIT 10
        ''')
        stats['top_symbols'] = [dict(row) for row in cursor.fetchall()]
        
        # Sinais recentes (últimas 24h)
        cursor.execute('''
            SELECT COUNT(*) as count 
            FROM trading_signals 
            WHERE created_at >= datetime('now', '-1 day')
        ''')
        stats['signals_24h'] = cursor.fetchone()['count']

        cursor.execute('SELECT COUNT(*) as total FROM backtest_runs')
        stats['total_backtests'] = cursor.fetchone()['total']

        cursor.execute('SELECT COUNT(*) as total FROM paper_trades')
        stats['total_paper_trades'] = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as total FROM paper_trades WHERE status = 'OPEN'")
        stats['open_paper_trades'] = cursor.fetchone()['total']
        
        conn.close()
        return stats
    
    def cleanup_old_data(self, days_to_keep: int = 30):
        """Limpar dados antigos"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Remover sinais antigos
        cursor.execute('''
            DELETE FROM trading_signals 
            WHERE created_at < datetime('now', '-{} days')
        '''.format(days_to_keep))
        
        # Remover análises antigas
        cursor.execute('''
            DELETE FROM analysis_history 
            WHERE created_at < datetime('now', '-{} days')
        '''.format(days_to_keep))

        cursor.execute('''
            DELETE FROM backtest_trades
            WHERE run_id IN (
                SELECT id FROM backtest_runs
                WHERE created_at < datetime('now', '-{} days')
            )
        '''.format(days_to_keep))

        cursor.execute('''
            DELETE FROM backtest_runs
            WHERE created_at < datetime('now', '-{} days')
        '''.format(days_to_keep))

        cursor.execute('''
            DELETE FROM paper_trades
            WHERE created_at < datetime('now', '-{} days')
        '''.format(days_to_keep))
        
        conn.commit()
        conn.close()

    def _normalize_timestamp(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return str(value)

# Instância global do banco
db = TradingDatabase()
