import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import contextlib
import html
import os
import json
import time
from datetime import datetime, timedelta, date
import asyncio
import hmac
import logging
import secrets
import signal
import subprocess
import sys
from pathlib import Path

# Importar funções de fuso horário brasileiro
from utils.timezone_utils import now_brazil, format_brazil_time, get_brazil_datetime_naive, BRAZIL_TZ

# Importar banco de dados
from database.database import build_strategy_version, db
from runtime_process import (
    BOT_EXECUTION_LOG_PATH,
    BOT_RUNNER_STDERR_LOG_PATH,
    BOT_RUNNER_STDOUT_LOG_PATH,
    RUNTIME_PROCESS_STATE_PATH,
    build_account_runtime_key,
    clear_runtime_process_state,
    clear_runtime_stop_request,
    get_runtime_execution_log_path,
    get_runtime_process_state_path,
    get_runtime_stderr_log_path,
    get_runtime_stdout_log_path,
    get_runtime_stop_request_path,
    read_runtime_process_state,
    request_runtime_stop,
    tail_text_file,
    write_runtime_process_state,
)
from trading_bot import TradingBot
from config import AppConfig, ExchangeConfig, ProductionConfig
try:
    from live_go_live_check import build_go_live_report
except Exception:
    build_go_live_report = None
try:
    from futures_trading import FuturesTrading
    _FUTURES_IMPORT_ERROR = None
except Exception as exc:
    FuturesTrading = None
    _FUTURES_IMPORT_ERROR = exc

from services.paper_trade_service import PaperTradeService
from services.risk_management_service import RiskManagementService

logger = logging.getLogger(__name__)
ACTIONABLE_SIGNALS = {"COMPRA", "VENDA"}
MAX_SIGNAL_DATA_AGE_SECONDS = int(os.getenv("MAX_SIGNAL_DATA_AGE_SECONDS", "180").strip() or "180")
_TELEGRAM_SERVICE_CLASS = None
_TELEGRAM_SERVICE_AVAILABLE = None
_BACKTEST_ENGINE_CLASS = None
DASHBOARD_SESSION_QUERY_KEY = "workspace_session"


def inject_dashboard_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --dashboard-bg: #f4efe6;
            --dashboard-surface: rgba(255, 251, 245, 0.88);
            --dashboard-surface-strong: #fff8ee;
            --dashboard-border: #e5d8c4;
            --dashboard-ink: #1f2937;
            --dashboard-muted: #667085;
            --dashboard-accent: #0f766e;
            --dashboard-accent-soft: #d8f1ec;
            --dashboard-warm: #c97a2b;
            --dashboard-warm-soft: #fff1dd;
            --dashboard-danger: #b9382f;
            --dashboard-shadow: 0 18px 46px rgba(31, 41, 55, 0.08);
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(15, 118, 110, 0.08), transparent 30%),
                radial-gradient(circle at top right, rgba(201, 122, 43, 0.10), transparent 28%),
                linear-gradient(180deg, #fbf7f0 0%, var(--dashboard-bg) 100%);
            color: var(--dashboard-ink);
        }

        .main .block-container {
            max-width: 1440px;
            padding-top: 1.2rem;
            padding-bottom: 4rem;
        }

        section[data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(255, 250, 242, 0.96) 0%, rgba(247, 239, 227, 0.96) 100%);
            border-right: 1px solid var(--dashboard-border);
        }

        h1, h2, h3 {
            color: var(--dashboard-ink);
            letter-spacing: -0.02em;
        }

        .dashboard-shell,
        .section-hero,
        .dashboard-strip {
            border: 1px solid var(--dashboard-border);
            background: linear-gradient(145deg, rgba(255,255,255,0.92), rgba(255,248,238,0.88));
            border-radius: 24px;
            box-shadow: var(--dashboard-shadow);
        }

        .dashboard-shell {
            padding: 1.35rem 1.4rem 1.15rem 1.4rem;
            margin: 0 0 1rem 0;
            overflow: hidden;
            position: relative;
        }

        .dashboard-shell::after,
        .section-hero::after {
            content: "";
            position: absolute;
            inset: auto -4rem -4rem auto;
            width: 13rem;
            height: 13rem;
            border-radius: 999px;
            background: radial-gradient(circle, rgba(15, 118, 110, 0.14), transparent 62%);
            pointer-events: none;
        }

        .shell-kicker,
        .hero-kicker {
            display: inline-block;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.14em;
            color: var(--dashboard-accent);
            background: var(--dashboard-accent-soft);
            border-radius: 999px;
            padding: 0.36rem 0.75rem;
            margin-bottom: 0.8rem;
        }

        .dashboard-shell h1,
        .section-hero h2 {
            margin: 0;
            font-size: 2rem;
            line-height: 1.05;
        }

        .dashboard-shell p,
        .section-hero p,
        .dashboard-strip p {
            margin: 0.45rem 0 0 0;
            color: var(--dashboard-muted);
            line-height: 1.5;
        }

        .shell-badges,
        .hero-badges,
        .strip-badges {
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
            margin-top: 1rem;
        }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.44rem 0.8rem;
            border-radius: 999px;
            border: 1px solid var(--dashboard-border);
            background: rgba(255, 255, 255, 0.82);
            color: var(--dashboard-ink);
            font-size: 0.9rem;
            font-weight: 600;
        }

        .status-pill strong {
            font-weight: 800;
        }

        .status-pill.accent {
            background: var(--dashboard-accent-soft);
            border-color: rgba(15, 118, 110, 0.18);
        }

        .status-pill.warm {
            background: var(--dashboard-warm-soft);
            border-color: rgba(201, 122, 43, 0.22);
        }

        .status-pill.danger {
            background: rgba(248, 222, 222, 0.88);
            border-color: rgba(185, 56, 47, 0.18);
        }

        .section-hero {
            padding: 1.15rem 1.25rem 1rem 1.25rem;
            margin-bottom: 1rem;
            position: relative;
            overflow: hidden;
        }

        .dashboard-strip {
            padding: 0.95rem 1rem;
            margin: 0.75rem 0 1rem 0;
        }

        div[data-testid="stMetric"] {
            background: var(--dashboard-surface);
            border: 1px solid var(--dashboard-border);
            border-radius: 18px;
            box-shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
            padding: 0.9rem 1rem;
            min-height: 116px;
        }

        div[data-testid="stMetric"] label {
            color: var(--dashboard-muted) !important;
            font-size: 0.76rem !important;
            font-weight: 700 !important;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        div[data-testid="stMetricValue"] {
            color: var(--dashboard-ink);
        }

        div[role="radiogroup"] {
            gap: 0.55rem;
            flex-wrap: wrap;
        }

        div[role="radiogroup"] label {
            background: rgba(255, 255, 255, 0.72);
            border: 1px solid var(--dashboard-border);
            border-radius: 999px;
            padding: 0.2rem 0.8rem;
            box-shadow: 0 6px 18px rgba(15, 23, 42, 0.04);
        }

        .stButton > button,
        .stDownloadButton > button {
            border-radius: 999px;
            border: 1px solid #dcc7a8;
            background: linear-gradient(145deg, #fffaf2, #f5ead9);
            color: var(--dashboard-ink);
            font-weight: 700;
            padding: 0.58rem 1rem;
            box-shadow: 0 10px 20px rgba(31, 41, 55, 0.06);
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover {
            border-color: #c9a56e;
            color: #0f172a;
        }

        [data-testid="stExpander"] {
            background: rgba(255, 255, 255, 0.72);
            border: 1px solid var(--dashboard-border);
            border-radius: 18px;
            overflow: hidden;
        }

        .stAlert {
            border-radius: 18px;
            border: 1px solid rgba(148, 163, 184, 0.18);
        }

        .stTextInput > div > div,
        .stNumberInput > div > div,
        .stSelectbox > div > div,
        .stTextArea textarea {
            border-radius: 14px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _build_status_pill(label: str, value: str, tone: str = "default") -> str:
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))
    css_class = "status-pill"
    if tone in {"accent", "warm", "danger"}:
        css_class += f" {tone}"
    return f"<span class='{css_class}'><span>{safe_label}</span><strong>{safe_value}</strong></span>"


def render_dashboard_shell(
    *,
    active_section_label: str,
    dashboard_user: dict | None,
    admin_session_active: bool,
    selected_exchange: str,
) -> None:
    if dashboard_user:
        session_label = (
            dashboard_user.get("first_name")
            or dashboard_user.get("username")
            or dashboard_user.get("login_name")
            or str(dashboard_user.get("user_id"))
        )
        session_tone = "accent"
    elif admin_session_active:
        session_label = "Admin"
        session_tone = "warm"
    else:
        session_label = "Visitante"
        session_tone = "danger"

    badges = "".join(
        [
            _build_status_pill("Seção", active_section_label, "accent"),
            _build_status_pill("Sessão", session_label, session_tone),
            _build_status_pill("Exchange", selected_exchange.upper(), "warm"),
            _build_status_pill("Foco", "Runtime + Mercado", "default"),
        ]
    )
    st.markdown(
        f"""
        <section class="dashboard-shell">
            <span class="shell-kicker">Evo Coin Command</span>
            <h1>Controle visual do bot, mercado e validação</h1>
            <p>
                A dashboard agora prioriza leitura rápida, status operacional e navegação mais limpa
                para deixar o acompanhamento diário mais confortável.
            </p>
            <div class="shell-badges">{badges}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_section_hero(
    *,
    kicker: str,
    title: str,
    subtitle: str,
    badges: list[str] | None = None,
) -> None:
    badge_html = "".join(badges or [])
    st.markdown(
        f"""
        <section class="section-hero">
            <span class="hero-kicker">{html.escape(kicker)}</span>
            <h2>{html.escape(title)}</h2>
            <p>{html.escape(subtitle)}</p>
            <div class="hero-badges">{badge_html}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard_strip(message: str, badges: list[str] | None = None) -> None:
    badge_html = "".join(badges or [])
    st.markdown(
        f"""
        <section class="dashboard-strip">
            <p>{html.escape(message)}</p>
            <div class="strip-badges">{badge_html}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


class _UnavailableTelegramService:
    def __init__(self):
        self._configured = False

    def is_configured(self):
        return False

    def get_config_status(self):
        return {'configured': False}

    def configure(self, bot_token: str, chat_id: str):
        return False, "❌ Telegram não disponível"

    def disable(self):
        return None

    async def test_connection(self):
        return False, "❌ Telegram não disponível"

    async def send_signal_alert(self, symbol: str, signal: str, price: float, rsi: float, macd: float, macd_signal: float):
        return False

    async def send_custom_message(self, message: str):
        return False, "❌ Telegram não disponível"


class _UnavailableBacktestEngine:
    def __init__(self):
        self._error = "Modulo backtest nao encontrado"

    def run_backtest(self, *args, **kwargs):
        raise RuntimeError(self._error)

    def run_market_scan(self, *args, **kwargs):
        raise RuntimeError(self._error)

    def run_global_robustness_matrix(self, *args, **kwargs):
        raise RuntimeError(self._error)

    def optimize_rsi_parameters(self, *args, **kwargs):
        raise RuntimeError(self._error)

    def get_trade_summary_df(self):
        return pd.DataFrame()


class _TerminalBacktestEngine:
    """Adapter que usa a lógica validada no terminal (backtest.run_backtest)."""

    def __init__(self):
        self._trade_summary_df = pd.DataFrame(
            columns=["timestamp", "entry_price", "price", "profit_loss_pct", "profit_loss", "signal"]
        )
        self._last_result = None

    @staticmethod
    def _timeframe_to_minutes(timeframe: str) -> int:
        tf = str(timeframe or "").strip().lower()
        if len(tf) < 2:
            return 15
        unit = tf[-1]
        value_text = tf[:-1]
        if not value_text.isdigit():
            return 15
        value = int(value_text)
        if unit == "m":
            return max(1, value)
        if unit == "h":
            return max(1, value) * 60
        if unit == "d":
            return max(1, value) * 1440
        if unit == "w":
            return max(1, value) * 10080
        return 15

    @classmethod
    def _estimate_candles(cls, timeframe: str, start_date, end_date) -> int:
        start_ts = pd.to_datetime(start_date, errors="coerce", utc=True)
        end_ts = pd.to_datetime(end_date, errors="coerce", utc=True)
        if pd.isna(start_ts) or pd.isna(end_ts) or end_ts <= start_ts:
            return 3000
        tf_minutes = max(1, cls._timeframe_to_minutes(timeframe))
        total_minutes = max(1.0, (end_ts - start_ts).total_seconds() / 60.0)
        # margem para aquecimento de indicadores e bordas de execução
        estimated = int(total_minutes / tf_minutes) + 250
        return max(500, min(estimated, 100000))

    @staticmethod
    def _normalize_backtest_candle_df(df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or df.empty:
            return None

        working_df = df.copy()
        if "timestamp" not in working_df.columns:
            return None
        working_df["timestamp"] = pd.to_datetime(working_df["timestamp"], utc=True, errors="coerce")
        for column in ["open", "high", "low", "close", "volume"]:
            if column not in working_df.columns:
                return None
            working_df[column] = pd.to_numeric(working_df[column], errors="coerce")
        if "is_closed" in working_df.columns:
            working_df = working_df[working_df["is_closed"].fillna(False).astype(bool)]
        working_df = working_df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        if working_df.empty:
            return None
        working_df = (
            working_df.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )
        return working_df

    @staticmethod
    def _get_backtest_stream_clients() -> dict:
        if "backtest_stream_clients" not in st.session_state:
            st.session_state.backtest_stream_clients = {}
        return st.session_state.backtest_stream_clients

    @classmethod
    def _get_or_init_backtest_stream_client(
        cls,
        *,
        symbol: str,
        timeframe: str,
        testnet: bool,
        max_candles: int,
    ):
        from trading_bot_websocket import StreamlinedTradingBot

        stream_key = f"{symbol}|{timeframe}|{int(bool(testnet))}"
        clients = cls._get_backtest_stream_clients()
        existing_client = clients.get(stream_key)
        if existing_client is not None:
            current_capacity = int(getattr(existing_client, "max_candles", 0) or 0)
            if current_capacity >= max_candles:
                return existing_client
            with contextlib.suppress(Exception):
                existing_client.stop()

        stream_client = StreamlinedTradingBot(
            symbol=symbol,
            timeframe=timeframe,
            max_candles=max(max_candles, 500),
            testnet=bool(testnet),
            allow_rest_fallback=False,
            bootstrap_df=None,
        )
        clients[stream_key] = stream_client
        return stream_client

    @classmethod
    def _persist_backtest_candle_frame(
        cls,
        *,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame | None,
        source: str,
    ) -> int:
        normalized_df = cls._normalize_backtest_candle_df(df)
        if normalized_df is None or normalized_df.empty:
            return 0
        candle_rows = normalized_df[["timestamp", "open", "high", "low", "close", "volume"]].to_dict("records")
        return db.store_backtest_websocket_candles(
            symbol=symbol,
            timeframe=timeframe,
            candles=candle_rows,
            source=source,
        )

    @staticmethod
    def _load_backtest_from_shared_stream(
        symbol: str,
        timeframe: str,
        candles: int,
        *,
        allow_partial: bool = False,
    ) -> tuple[pd.DataFrame | None, str | None]:
        runtime_bot = st.session_state.get("trading_bot")
        if runtime_bot is None:
            return None, None

        try:
            stream_client = runtime_bot._get_realtime_stream_client(symbol=symbol, timeframe=timeframe)
            if stream_client is None:
                return None, None

            status = stream_client.get_current_status() or {}
            available_candles = int(status.get("candles") or 0)
            client_capacity = int(getattr(stream_client, "max_candles", 0) or 0)
            if available_candles <= 0 or client_capacity <= 0:
                return None, None

            request_limit = min(max(50, int(candles or 0)), available_candles, client_capacity)
            if not allow_partial and request_limit < candles:
                return None, None

            df = stream_client.get_market_data(
                limit=request_limit,
                timeout=2.0,
                include_current_candle=False,
            )
            normalized_df = _TerminalBacktestEngine._normalize_backtest_candle_df(df)
            if normalized_df is None or normalized_df.empty:
                return None, None
            if not allow_partial and len(normalized_df) < candles:
                return None, None
            return normalized_df.reset_index(drop=True), "shared_websocket_buffer"
        except Exception:
            logger.warning(
                "Falha ao reutilizar buffer websocket compartilhado para backtest %s %s.",
                symbol,
                timeframe,
                exc_info=True,
            )
            return None, None

    @classmethod
    def sync_backtest_websocket_feed(
        cls,
        *,
        symbol: str,
        timeframe: str,
        testnet: bool,
        snapshot_limit: int = 1200,
    ) -> dict:
        inserted_total = 0
        shared_snapshot, shared_source = cls._load_backtest_from_shared_stream(
            symbol,
            timeframe,
            snapshot_limit,
            allow_partial=True,
        )
        if shared_snapshot is not None:
            inserted_total += cls._persist_backtest_candle_frame(
                symbol=symbol,
                timeframe=timeframe,
                df=shared_snapshot,
                source=shared_source or "shared_websocket_buffer",
            )

        dedicated_client = cls._get_or_init_backtest_stream_client(
            symbol=symbol,
            timeframe=timeframe,
            testnet=bool(testnet),
            max_candles=max(snapshot_limit, 1500),
        )
        dedicated_status = dedicated_client.get_current_status() or {}
        dedicated_snapshot = None
        with contextlib.suppress(Exception):
            dedicated_snapshot = dedicated_client.get_market_data(
                limit=min(max(snapshot_limit, 300), int(getattr(dedicated_client, "max_candles", snapshot_limit) or snapshot_limit)),
                timeout=1.5,
                include_current_candle=False,
            )
        inserted_total += cls._persist_backtest_candle_frame(
            symbol=symbol,
            timeframe=timeframe,
            df=dedicated_snapshot,
            source="backtest_public_websocket",
        )

        coverage = db.get_backtest_websocket_candle_coverage(symbol=symbol, timeframe=timeframe)
        return {
            "inserted": int(inserted_total),
            "coverage": coverage,
            "shared_available": 0 if shared_snapshot is None else int(len(shared_snapshot)),
            "stream_status": dedicated_status,
        }

    @classmethod
    def _load_backtest_from_persisted_websocket(
        cls,
        *,
        symbol: str,
        timeframe: str,
        candles: int,
        start_date,
        end_date,
    ) -> tuple[pd.DataFrame | None, dict]:
        timeframe_delta = timedelta(minutes=cls._timeframe_to_minutes(timeframe))
        warmup_candles = max(250, min(600, int(candles * 0.2) if candles > 0 else 250))
        start_ts = pd.to_datetime(start_date, errors="coerce", utc=True)
        end_ts = pd.to_datetime(end_date, errors="coerce", utc=True)
        query_start = start_ts - (timeframe_delta * warmup_candles) if not pd.isna(start_ts) else None
        query_end = end_ts + timeframe_delta if not pd.isna(end_ts) else None

        rows = db.get_backtest_websocket_candles(
            symbol=symbol,
            timeframe=timeframe,
            start_timestamp=query_start,
            end_timestamp=query_end,
        )
        coverage = db.get_backtest_websocket_candle_coverage(symbol=symbol, timeframe=timeframe)
        if not rows:
            return None, coverage

        df = pd.DataFrame(rows)
        if "candle_timestamp" in df.columns and "timestamp" not in df.columns:
            df = df.rename(columns={"candle_timestamp": "timestamp"})
        normalized_df = cls._normalize_backtest_candle_df(df)
        if normalized_df is None or normalized_df.empty:
            return None, coverage

        if start_ts is not None and not pd.isna(start_ts):
            required_period_start = start_ts
            first_timestamp = normalized_df["timestamp"].iloc[0]
            if first_timestamp > required_period_start:
                return None, coverage

        if end_ts is not None and not pd.isna(end_ts):
            required_period_end = end_ts
            last_timestamp = normalized_df["timestamp"].iloc[-1]
            if last_timestamp < required_period_end:
                return None, coverage

        if len(normalized_df) < max(300, min(candles, len(normalized_df))):
            return None, coverage

        return normalized_df.reset_index(drop=True), coverage

    @classmethod
    def _load_backtest_market_series(
        cls,
        symbol: str,
        timeframe: str,
        candles: int,
        *,
        testnet: bool,
        start_date,
        end_date,
    ) -> tuple[pd.DataFrame, str]:
        cls.sync_backtest_websocket_feed(
            symbol=symbol,
            timeframe=timeframe,
            testnet=bool(testnet),
            snapshot_limit=min(max(candles, 600), 3000),
        )
        shared_df, shared_source = cls._load_backtest_from_shared_stream(
            symbol,
            timeframe,
            candles,
            allow_partial=False,
        )
        if shared_df is not None and len(shared_df) >= candles:
            cls._persist_backtest_candle_frame(
                symbol=symbol,
                timeframe=timeframe,
                df=shared_df,
                source=shared_source or "shared_websocket_buffer",
            )
            return shared_df.reset_index(drop=True), "shared_websocket_buffer"

        persisted_df, coverage = cls._load_backtest_from_persisted_websocket(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            start_date=start_date,
            end_date=end_date,
        )
        if persisted_df is not None:
            return persisted_df.reset_index(drop=True), "persisted_public_websocket_db"

        raise RuntimeError(
            "Ainda nao ha historico suficiente da WebSocket publica para este backtest. "
            f"Cobertura atual em banco: {int((coverage or {}).get('total') or 0)} candles "
            f"de {(coverage or {}).get('first_timestamp') or '-'} ate {(coverage or {}).get('last_timestamp') or '-'}. "
            "Deixe a dashboard acumular candles desta WebSocket por mais tempo ou reduza o periodo solicitado."
        )

    @staticmethod
    def _build_trade_summary_df(trades, initial_balance: float) -> pd.DataFrame:
        rows = []
        balance = float(initial_balance)

        for trade in trades:
            net_pct = float(trade.get("net_pct", 0.0) or 0.0)
            pnl_amount = balance * (net_pct / 100.0)
            balance += pnl_amount

            timestamp = pd.to_datetime(
                trade.get("exit_timestamp") or trade.get("entry_timestamp"),
                errors="coerce",
                utc=True,
            )
            rows.append(
                {
                    "timestamp": timestamp,
                    "entry_price": float(trade.get("entry_price", 0.0) or 0.0),
                    "price": float(trade.get("exit_price", 0.0) or 0.0),
                    "profit_loss_pct": net_pct,
                    "profit_loss": pnl_amount,
                    "signal": "COMPRA" if str(trade.get("side", "")).lower() == "long" else "VENDA",
                }
            )

        if not rows:
            return pd.DataFrame(columns=["timestamp", "entry_price", "price", "profit_loss_pct", "profit_loss", "signal"])

        df = pd.DataFrame(rows)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        return df

    @staticmethod
    def _build_portfolio_values(trades, initial_balance: float):
        portfolio_values = []
        balance = float(initial_balance)
        if not trades:
            return portfolio_values

        for trade in trades:
            net_pct = float(trade.get("net_pct", 0.0) or 0.0)
            balance *= (1.0 + net_pct / 100.0)
            ts = pd.to_datetime(trade.get("exit_timestamp"), errors="coerce", utc=True)
            if pd.isna(ts):
                ts = pd.to_datetime(trade.get("entry_timestamp"), errors="coerce", utc=True)
            portfolio_values.append(
                {
                    "timestamp": ts.isoformat() if not pd.isna(ts) else None,
                    "portfolio_value": float(balance),
                }
            )
        return portfolio_values

    @staticmethod
    def _compute_stats(trades, summary: dict, initial_balance: float) -> dict:
        import math

        returns_pct = [float(t.get("net_pct", 0.0) or 0.0) for t in trades]
        returns = [r / 100.0 for r in returns_pct]
        wins = [r for r in returns_pct if r > 0]
        losses = [abs(r) for r in returns_pct if r <= 0]

        gross_profit = float(sum(wins))
        gross_loss = float(sum(losses))
        avg_profit = float(sum(wins) / len(wins)) if wins else 0.0
        avg_loss = float(sum(losses) / len(losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
        payoff_ratio = (avg_profit / avg_loss) if avg_loss > 0 else 0.0

        equity = float(initial_balance)
        peak = equity
        max_drawdown = 0.0
        for r in returns:
            equity *= (1.0 + r)
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak * 100.0
                if dd > max_drawdown:
                    max_drawdown = dd

        sharpe_ratio = 0.0
        if len(returns) >= 2:
            mean_r = sum(returns) / len(returns)
            variance = sum((x - mean_r) ** 2 for x in returns) / (len(returns) - 1)
            std_r = math.sqrt(max(variance, 0.0))
            if std_r > 0:
                sharpe_ratio = mean_r / std_r * math.sqrt(len(returns))

        from collections import Counter

        exit_reason_counts = dict(Counter(str(t.get("reason", "unknown")) for t in trades))
        stats = {
            "initial_balance": float(initial_balance),
            "final_balance": float(initial_balance) * (1.0 + float(summary.get("net_pct", 0.0) or 0.0) / 100.0),
            "total_return_pct": float(summary.get("net_pct", 0.0) or 0.0),
            "total_trades": int(summary.get("trades", 0) or 0),
            "winning_trades": int(summary.get("wins", 0) or 0),
            "losing_trades": int(summary.get("losses", 0) or 0),
            "win_rate": float(summary.get("win_rate_pct", 0.0) or 0.0),
            "avg_profit": avg_profit,
            "avg_loss": avg_loss,
            "expectancy_pct": float(summary.get("avg_trade_pct", 0.0) or 0.0),
            "max_drawdown": float(max_drawdown),
            "sharpe_ratio": float(sharpe_ratio),
            "profit_factor": float(profit_factor) if profit_factor != float("inf") else 999.0,
            "payoff_ratio": float(payoff_ratio),
            "exit_reason_counts": exit_reason_counts,
            # Compatibilidade com seções avançadas da UI
            "market_state_breakdown": [],
            "execution_mode_breakdown": [],
            "regime_breakdown": [],
            "setup_type_breakdown": [],
            "market_pattern_breakdown": [],
            "exit_type_breakdown": [],
            "entry_quality_breakdown": [],
            "risk_mode_breakdown": [],
            "avg_mfe_pct": 0.0,
            "avg_mae_pct": 0.0,
            "avg_profit_given_back_pct": 0.0,
        }
        return stats

    def run_backtest(self, *args, **kwargs):
        import contextlib
        import io
        import config as runtime_config
        from backtest import run_backtest as terminal_run_backtest

        symbol = kwargs.get("symbol") or (args[0] if len(args) > 0 else "BTC/USDT")
        timeframe = kwargs.get("timeframe") or (args[1] if len(args) > 1 else "15m")
        start_date = kwargs.get("start_date")
        end_date = kwargs.get("end_date")
        initial_balance = float(kwargs.get("initial_balance", 10000.0) or 10000.0)

        fee_pct = float(kwargs.get("fee_pct", getattr(runtime_config, "FEE_PCT", 0.08)) or 0.08)
        candles = int(kwargs.get("candles") or self._estimate_candles(timeframe, start_date, end_date))
        backtest_use_testnet = bool(getattr(runtime_config, "BACKTEST_USE_TESTNET", False))

        backtest_df, backtest_data_source = self._load_backtest_market_series(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            testnet=backtest_use_testnet,
            start_date=start_date,
            end_date=end_date,
        )

        capture = io.StringIO()
        try:
            with contextlib.redirect_stdout(capture):
                trades, summary = terminal_run_backtest(
                    symbol=symbol,
                    timeframe=timeframe,
                    candles=candles,
                    fee_pct=fee_pct,
                    testnet=backtest_use_testnet,
                    use_local_csv=False,
                    preloaded_df=backtest_df,
                )
        except Exception:
            raise

        self._trade_summary_df = self._build_trade_summary_df(trades, initial_balance)
        stats = self._compute_stats(trades, summary, initial_balance)
        portfolio_values = self._build_portfolio_values(trades, initial_balance)

        strategy_version = build_strategy_version(
            symbol=symbol,
            timeframe=timeframe,
            rsi_period=kwargs.get("rsi_period"),
            rsi_min=kwargs.get("rsi_min"),
            rsi_max=kwargs.get("rsi_max"),
            stop_loss_pct=float(kwargs.get("stop_loss_pct", 0.0) or 0.0),
            take_profit_pct=float(kwargs.get("take_profit_pct", 0.0) or 0.0),
            require_volume=bool(kwargs.get("require_volume", False)),
            require_trend=bool(kwargs.get("require_trend", False)),
            avoid_ranging=bool(kwargs.get("avoid_ranging", False)),
            context_timeframe=kwargs.get("context_timeframe"),
        )

        result = {
            "symbol": symbol,
            "timeframe": timeframe,
            "stats": stats,
            "trades": trades,
            "portfolio_values": portfolio_values,
            "benchmark_values": [],
            "equity_diagnostics": {},
            "saved_run_id": None,
            "meta": {
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy_version": strategy_version,
                "data_source": backtest_data_source,
                "backtest_use_testnet": backtest_use_testnet,
                "rsi_min": kwargs.get("rsi_min"),
                "rsi_max": kwargs.get("rsi_max"),
                "ai_assist_mode": kwargs.get("ai_assist_mode", "disabled"),
                "ai_min_win_probability": float(kwargs.get("ai_min_win_probability", 0.0) or 0.0),
            },
            "ai_summary": {},
            "ai_comparison": {},
            "objective_check": {},
            "market_state_summary": [],
            "execution_mode_summary": [],
            "regime_summary": [],
            "market_pattern_summary": [],
            "risk_engine_summary": {},
            "position_management_summary": {},
            "signal_audit_summary": {},
            "trade_autopsy": [],
            "signal_audit": [],
        }
        self._last_result = result
        return result

    def run_market_scan(self, *args, **kwargs):
        raise RuntimeError("Modo atual usa o backtest do terminal; market scan não está disponível nesta integração.")

    def run_global_robustness_matrix(self, *args, **kwargs):
        raise RuntimeError("Modo atual usa o backtest do terminal; matriz global não está disponível nesta integração.")

    def optimize_rsi_parameters(self, *args, **kwargs):
        raise RuntimeError("Modo atual usa o backtest do terminal; otimização não está disponível nesta integração.")

    def get_trade_summary_df(self):
        return self._trade_summary_df.copy()


@st.cache_resource
def get_paper_trade_service():
    return PaperTradeService()


@st.cache_resource
def get_risk_management_service():
    return RiskManagementService()


def get_telegram_service_class():
    global _TELEGRAM_SERVICE_CLASS, _TELEGRAM_SERVICE_AVAILABLE
    if _TELEGRAM_SERVICE_CLASS is None:
        try:
            from services.telegram_service import SecureTelegramService as telegram_service_class, TELEGRAM_AVAILABLE as telegram_available

            _TELEGRAM_SERVICE_CLASS = telegram_service_class
            _TELEGRAM_SERVICE_AVAILABLE = bool(telegram_available)
        except ImportError:
            _TELEGRAM_SERVICE_CLASS = _UnavailableTelegramService
            _TELEGRAM_SERVICE_AVAILABLE = False
    return _TELEGRAM_SERVICE_CLASS, bool(_TELEGRAM_SERVICE_AVAILABLE)


def is_telegram_service_available():
    _, telegram_available = get_telegram_service_class()
    return telegram_available


def is_websocket_runtime_available():
    try:
        from trading_bot_websocket import WEBSOCKETS_AVAILABLE as market_ws_available
    except Exception:
        market_ws_available = False
    return bool(market_ws_available)


def get_or_init_session_telegram_bot():
    if 'telegram_bot' not in st.session_state or st.session_state.telegram_bot is None:
        telegram_service_class, _ = get_telegram_service_class()
        st.session_state.telegram_bot = telegram_service_class()
    return st.session_state.telegram_bot


def get_or_init_trading_bot():
    if 'trading_bot' not in st.session_state or st.session_state.trading_bot is None:
        st.session_state.trading_bot = TradingBot()
    return st.session_state.trading_bot


def get_backtest_engine_class():
    global _BACKTEST_ENGINE_CLASS
    if _BACKTEST_ENGINE_CLASS is None:
        _BACKTEST_ENGINE_CLASS = _TerminalBacktestEngine
    return _BACKTEST_ENGINE_CLASS


def get_or_init_backtest_engine():
    if 'backtest_engine' not in st.session_state or st.session_state.backtest_engine is None:
        st.session_state.backtest_engine = get_backtest_engine_class()()
    return st.session_state.backtest_engine


def initialize_dashboard_session_state() -> None:
    session_defaults = {
        "trading_bot": None,
        "telegram_bot": None,
        "telegram_trading_bot_started": False,
        "trader_bot_pid": None,
        "trader_bot_testnet": True,
        "signals_history": list,
        "last_update": None,
        "last_market_timestamp": None,
        "auto_refresh": True,
        "current_data": None,
        "telegram_notifications": False,
        "backtest_engine": None,
        "backtest_results": None,
        "backtest_scan_results": None,
        "backtest_optimization_results": None,
        "backtest_robustness_results": None,
        "dashboard_user_auth": None,
        "dashboard_user_session_token": "",
        "dashboard_user_login": "",
        "dashboard_user_password": "",
        "dashboard_user_auth_error": "",
        "multi_symbol_data": dict,
        "futures_trading": None,
    }
    for key, default in session_defaults.items():
        if key in st.session_state:
            continue
        if default is list:
            st.session_state[key] = []
        elif default is dict:
            st.session_state[key] = {}
        else:
            st.session_state[key] = default


def _get_dashboard_query_param_value(key: str) -> str:
    try:
        raw_value = st.query_params.get(key, "")
    except Exception:
        return ""
    if isinstance(raw_value, list):
        raw_value = raw_value[0] if raw_value else ""
    return str(raw_value or "").strip()


def _set_dashboard_query_param_value(key: str, value: str) -> None:
    try:
        if value:
            st.query_params[key] = value
        elif key in st.query_params:
            del st.query_params[key]
    except Exception:
        logger.debug("Falha ao atualizar query param %s.", key, exc_info=True)


def _get_dashboard_request_headers() -> dict:
    try:
        context = getattr(st, "context", None)
        headers = getattr(context, "headers", None)
        if headers:
            return {str(key).lower(): str(value) for key, value in dict(headers).items()}
    except Exception:
        return {}
    return {}


def _get_dashboard_client_ip() -> str:
    headers = _get_dashboard_request_headers()
    for header_name in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "x-client-ip"):
        raw_value = str(headers.get(header_name) or "").strip()
        if raw_value:
            return raw_value.split(",")[0].strip()
    return "local-dashboard"


def _get_or_create_dashboard_device_id() -> str:
    device_id = _get_dashboard_query_param_value("device_id")
    if device_id:
        return device_id
    device_id = secrets.token_urlsafe(18)
    _set_dashboard_query_param_value("device_id", device_id)
    return device_id


def validate_dashboard_license_for_user(auth_payload: dict | None) -> dict:
    if not auth_payload:
        return {"allowed": False, "reason": "not_authenticated"}
    if bool(st.session_state.get("admin_authenticated")):
        return {"allowed": True, "reason": "admin_bypass"}
    if not bool(getattr(ProductionConfig, "REQUIRE_DASHBOARD_DEVICE_LICENSE", True)):
        return {"allowed": True, "reason": "license_not_required"}
    try:
        return db.validate_dashboard_device_license(
            user_id=int(auth_payload["user_id"]),
            ip_address=_get_dashboard_client_ip(),
            device_fingerprint=_get_or_create_dashboard_device_id(),
            scope="workspace",
            auto_bind=bool(getattr(ProductionConfig, "DASHBOARD_LICENSE_AUTO_BIND_FIRST_ACCESS", True)),
            bind_ip=bool(getattr(ProductionConfig, "DASHBOARD_LICENSE_BIND_IP", True)),
            bind_device=bool(getattr(ProductionConfig, "DASHBOARD_LICENSE_BIND_DEVICE", True)),
        )
    except Exception as exc:
        logger.warning("Falha ao validar licenca da dashboard.", exc_info=True)
        return {"allowed": False, "reason": f"license_validation_error: {exc}"}


def _get_persistent_dashboard_session_token() -> str:
    session_token = str(st.session_state.get("dashboard_user_session_token") or "").strip()
    if session_token:
        return session_token
    query_token = _get_dashboard_query_param_value(DASHBOARD_SESSION_QUERY_KEY)
    if query_token:
        st.session_state.dashboard_user_session_token = query_token
    return query_token


def _set_persistent_dashboard_session_token(session_token: str) -> None:
    normalized_token = str(session_token or "").strip()
    st.session_state.dashboard_user_session_token = normalized_token
    _set_dashboard_query_param_value(DASHBOARD_SESSION_QUERY_KEY, normalized_token)


def _clear_persistent_dashboard_session_token() -> None:
    st.session_state.dashboard_user_session_token = ""
    _set_dashboard_query_param_value(DASHBOARD_SESSION_QUERY_KEY, "")


def ensure_trading_runtime(selected_exchange: str):
    trading_bot = get_or_init_trading_bot()
    if st.session_state.get("current_exchange") == selected_exchange:
        return trading_bot

    trading_bot.exchange_name = selected_exchange
    trading_bot.exchange = ExchangeConfig.get_exchange_instance(selected_exchange, testnet=False)
    st.session_state.current_exchange = selected_exchange
    st.session_state.current_data = None
    st.session_state.last_update = None
    st.session_state.last_market_timestamp = None
    st.session_state.multi_symbol_data = {}
    return trading_bot


def get_session_trading_bot_safe(selected_exchange: str, *, force_init: bool = False):
    """
    Garante um TradingBot válido na sessão e evita NoneType em chamadas de mercado.
    Tenta alinhar exchange/timeframe com o runtime selecionado; se falhar, aplica fallback leve.
    """
    trading_bot = st.session_state.get("trading_bot")
    should_bootstrap = (
        force_init
        or trading_bot is None
        or st.session_state.get("current_exchange") != selected_exchange
    )
    if not should_bootstrap:
        return trading_bot

    try:
        return ensure_trading_runtime(selected_exchange)
    except Exception as exc:
        logger.warning("Falha ao inicializar runtime de trading (%s). Aplicando fallback.", selected_exchange, exc_info=True)
        try:
            return get_or_init_trading_bot()
        except Exception:
            logger.warning("Fallback de TradingBot também falhou.", exc_info=True)
            st.session_state.trading_bot = None
            return None


@st.cache_data(ttl=30, show_spinner=False)
def get_cached_ai_model_metadata():
    model_path = Path(ProductionConfig.AI_MODEL_PATH)
    metadata_path = Path(ProductionConfig.AI_MODEL_METADATA_PATH)
    payload = {
        "model_path": str(model_path),
        "metadata_path": str(metadata_path),
        "model_exists": model_path.exists(),
        "metadata_exists": metadata_path.exists(),
        "model_version": None,
        "metrics": {},
        "train_period": {},
        "test_period": {},
        "top_feature_importances": [],
    }
    if not metadata_path.exists():
        return payload

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["metadata_error"] = str(exc)
        return payload

    payload.update(
        {
            "model_version": metadata.get("model_version"),
            "metrics": metadata.get("metrics") or {},
            "train_period": metadata.get("train_period") or {},
            "test_period": metadata.get("test_period") or {},
            "top_feature_importances": list(metadata.get("top_feature_importances") or [])[:5],
            "dataset_rows": int(metadata.get("dataset_rows", 0) or 0),
            "test_rows": int(metadata.get("test_rows", 0) or 0),
        }
    )
    return payload


def get_ai_runtime_status(backtest_engine=None):
    metadata = dict(get_cached_ai_model_metadata())
    runtime_loaded = False
    runtime_version = metadata.get("model_version")

    runtime_model = getattr(getattr(backtest_engine, "ai_model", None), "runtime_model", None)
    if runtime_model is not None:
        runtime_loaded = bool(getattr(runtime_model, "model_loaded", False))
        runtime_version = runtime_model.metadata.get("model_version") or runtime_version

    metadata["runtime_loaded"] = runtime_loaded
    metadata["runtime_version"] = runtime_version
    return metadata


def run_async_task_sync(awaitable):
    """Executa uma coroutine sem vazar event loops no dashboard."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(awaitable)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            logger.debug("Falha ao encerrar async generators do loop temporario.", exc_info=True)
        asyncio.set_event_loop(None)
        loop.close()


def get_history_data_dir() -> Path:
    return Path(getattr(AppConfig, "HISTORY_DATA_DIR", os.path.join("data", "history")))


def list_history_data_files(limit: int = 50) -> list[dict]:
    history_dir = get_history_data_dir()
    if not history_dir.exists():
        return []

    rows = []
    for file_path in sorted(history_dir.iterdir(), key=lambda item: item.name.lower()):
        if not file_path.is_file():
            continue
        stat_result = file_path.stat()
        rows.append(
            {
                "Arquivo": file_path.name,
                "Tamanho (MB)": round(stat_result.st_size / (1024 * 1024), 3),
                "Atualizado em": datetime.fromtimestamp(stat_result.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def save_uploaded_history_file(uploaded_file) -> Path:
    safe_name = Path(str(getattr(uploaded_file, "name", "") or "")).name
    lower_name = safe_name.lower()
    if not safe_name or not (lower_name.endswith(".csv") or lower_name.endswith(".csv.gz")):
        raise ValueError("Envie um arquivo .csv ou .csv.gz.")

    history_dir = get_history_data_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    target_path = history_dir / safe_name
    with open(target_path, "wb") as output_file:
        output_file.write(uploaded_file.getbuffer())
    return target_path


@st.cache_resource
def get_user_manager():
    try:
        from user_manager import UserManager
        return UserManager()
    except ImportError:
        class _FallbackUserManager:
            def get_user_stats(self):
                return {'total_users': 0, 'free_users': 0, 'premium_users': 0, 'active_today': 0}

            def list_users(self, limit):
                return []

            def upgrade_to_premium(self, user_id):
                return False

            def add_admin(self, user_id):
                return False

            def is_admin(self, user_id):
                return False

            def get_user(self, user_id):
                return None

        return _FallbackUserManager()


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_active_strategy_profile(symbol: str, timeframe: str):
    return db.get_active_strategy_profile(symbol=symbol, timeframe=timeframe)


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_edge_monitor_summary(symbol: str, timeframe: str, strategy_version: str | None = None):
    return db.get_edge_monitor_summary(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_governance_evaluation(
    symbol: str,
    timeframe: str,
    strategy_version: str | None = None,
    current_regime: str | None = None,
):
    return db.evaluate_strategy_governance(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
        current_regime=current_regime,
        persist=False,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_backtest_run_promotion_readiness(run_id: int):
    return db.get_backtest_run_promotion_readiness(run_id)


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_strategy_governance_summary(symbol: str, timeframe: str, active_only: bool = False, limit: int = 10):
    return db.get_strategy_governance_summary(
        symbol=symbol,
        timeframe=timeframe,
        active_only=active_only,
        limit=limit,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_setup_regime_baselines(symbol: str, timeframe: str, strategy_version: str | None = None):
    return db.get_setup_regime_baselines(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_alignment_metrics(symbol: str, timeframe: str, strategy_version: str | None = None, limit: int = 5):
    return db.get_alignment_metrics(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
        limit=limit,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_governance_history(symbol: str, timeframe: str, strategy_version: str | None = None, limit: int = 10):
    return db.get_governance_history(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
        limit=limit,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_strategy_evaluations(
    symbol: str,
    timeframe: str,
    strategy_version: str | None = None,
    limit: int = 5,
):
    return db.get_strategy_evaluations(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
        limit=limit,
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_cached_strategy_evaluation_overview(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 10,
):
    return db.get_strategy_evaluation_overview(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )


@st.cache_data(ttl=10, show_spinner=False)
def get_cached_bot_runtime_db_state(runtime_key: str | None = None, limit: int = 1):
    rows = db.get_bot_runtime_state(runtime_key=runtime_key, limit=limit)
    return rows[0] if rows else None


@st.cache_data(ttl=30, show_spinner=False)
def get_cached_live_go_live_report():
    if build_go_live_report is None:
        return None
    return build_go_live_report()


def clear_dashboard_data_caches() -> None:
    get_cached_active_strategy_profile.clear()
    get_cached_edge_monitor_summary.clear()
    get_cached_governance_evaluation.clear()
    get_cached_backtest_run_promotion_readiness.clear()
    get_cached_strategy_governance_summary.clear()
    get_cached_setup_regime_baselines.clear()
    get_cached_alignment_metrics.clear()
    get_cached_governance_history.clear()
    get_cached_strategy_evaluations.clear()
    get_cached_strategy_evaluation_overview.clear()
    get_cached_bot_runtime_db_state.clear()
    get_cached_live_go_live_report.clear()


def clear_dashboard_user_session(*, revoke_persistent: bool = True):
    session_token = _get_persistent_dashboard_session_token()
    if revoke_persistent and session_token:
        try:
            db.revoke_dashboard_user_session(session_token)
        except Exception:
            logger.warning("Falha ao revogar sessao persistente da dashboard.", exc_info=True)
    _clear_persistent_dashboard_session_token()
    st.session_state.dashboard_user_auth = None
    st.session_state.dashboard_user_login = ""
    st.session_state.dashboard_user_password = ""
    st.session_state.dashboard_user_auth_error = ""


def get_authenticated_dashboard_user():
    auth_payload = st.session_state.get("dashboard_user_auth")
    if not auth_payload:
        session_token = _get_persistent_dashboard_session_token()
        if session_token:
            try:
                restored_auth = db.authenticate_dashboard_session(session_token)
            except Exception:
                restored_auth = None
                logger.warning("Falha ao restaurar sessao persistente da dashboard.", exc_info=True)
            if restored_auth:
                st.session_state.dashboard_user_auth = restored_auth
                st.session_state.dashboard_user_auth_error = ""
                auth_payload = restored_auth
            else:
                clear_dashboard_user_session(revoke_persistent=False)
                _clear_persistent_dashboard_session_token()
                return None
    if not auth_payload:
        return None

    expires_at_raw = auth_payload.get("expires_at")
    if not expires_at_raw:
        clear_dashboard_user_session()
        return None

    try:
        expires_at = datetime.fromisoformat(str(expires_at_raw))
    except ValueError:
        clear_dashboard_user_session()
        return None

    current_time = now_brazil()
    if expires_at <= current_time:
        clear_dashboard_user_session()
        return None

    user_id = auth_payload.get("user_id")
    if user_id is not None:
        try:
            subscription_payload = db.get_dashboard_user_subscription(int(user_id))
            auth_payload["subscription"] = subscription_payload
            auth_payload["license"] = validate_dashboard_license_for_user(auth_payload)
            st.session_state.dashboard_user_auth = auth_payload
        except Exception:
            logger.warning("Falha ao carregar assinatura do usuário %s.", user_id, exc_info=True)

    return auth_payload


def is_admin_dashboard_session_active() -> bool:
    return bool(st.session_state.get("admin_authenticated"))


def is_admin_dashboard_entry_requested() -> bool:
    admin_param = (
        _get_dashboard_query_param_value("admin")
        or _get_dashboard_query_param_value("admin_panel")
        or _get_dashboard_query_param_value("modo_admin")
    )
    return str(admin_param or "").strip().lower() in {"1", "true", "yes", "on", "admin", "sim"}


def get_trader_bot_entrypoint() -> Path:
    return Path(__file__).resolve().with_name("bot_runner.py")


def get_or_init_admin_telegram_bot():
    if 'telegram_trading_bot' not in st.session_state:
        try:
            from telegram_bot import TelegramTradingBot
            st.session_state.telegram_trading_bot = TelegramTradingBot(
                auto_configure_from_env=False,
            )
        except Exception as exc:
            logger.warning("Erro ao inicializar telegram_trading_bot do admin: %s", exc)
            st.session_state.telegram_trading_bot = None
    return st.session_state.get("telegram_trading_bot")


def _resolve_primary_runtime_key() -> str:
    runtime_symbol = str(os.getenv("SYMBOL", AppConfig.DEFAULT_SYMBOL)).strip() or AppConfig.DEFAULT_SYMBOL
    runtime_timeframe = str(os.getenv("TIMEFRAME", AppConfig.DEFAULT_TIMEFRAME)).strip() or AppConfig.DEFAULT_TIMEFRAME
    return f"primary:{runtime_symbol}:{runtime_timeframe}"


def _resolve_account_runtime_key(use_testnet: bool | None = None) -> str:
    runtime_symbol = str(os.getenv("SYMBOL", AppConfig.DEFAULT_SYMBOL)).strip() or AppConfig.DEFAULT_SYMBOL
    runtime_timeframe = str(os.getenv("TIMEFRAME", AppConfig.DEFAULT_TIMEFRAME)).strip() or AppConfig.DEFAULT_TIMEFRAME
    if use_testnet is None:
        use_testnet = bool(st.session_state.get("trader_bot_testnet", True))
    return (
        f"account:{_runtime_credential_user_id()}:"
        f"{_runtime_credential_account_id(bool(use_testnet))}:"
        f"{_runtime_credential_exchange_name()}:"
        f"{runtime_symbol}:{runtime_timeframe}"
    )


def _parse_runtime_datetime(raw_value):
    if raw_value in (None, ""):
        return None
    try:
        parsed = pd.to_datetime(raw_value, utc=True, errors="coerce")
    except Exception:
        return None
    if parsed is None or pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _format_age_label(total_seconds: float | None) -> str:
    if total_seconds is None:
        return "-"
    if total_seconds < 60:
        return f"{int(total_seconds)}s"
    if total_seconds < 3600:
        return f"{int(total_seconds // 60)}m"
    return f"{total_seconds / 3600:.1f}h"


def _runtime_heartbeat_age_seconds(runtime_db_state: dict | None) -> float | None:
    if not runtime_db_state:
        return None
    heartbeat_at = _parse_runtime_datetime(runtime_db_state.get("last_heartbeat_at"))
    if heartbeat_at is None:
        return None
    return max((datetime.now(heartbeat_at.tzinfo) - heartbeat_at).total_seconds(), 0.0)


def _runtime_heartbeat_is_recent(runtime_db_state: dict | None) -> bool:
    heartbeat_age = _runtime_heartbeat_age_seconds(runtime_db_state)
    if heartbeat_age is None:
        return False
    freshness_window = max(90.0, float(os.getenv("BOT_RUNTIME_HEARTBEAT_STALE_SEC", "90") or "90"))
    return heartbeat_age <= freshness_window


def _runtime_env_uses_testnet() -> bool:
    testnet_env = os.getenv("TESTNET")
    if testnet_env is not None:
        return str(testnet_env).strip().lower() in {"1", "true", "yes", "on", "y", "sim"}
    return bool(st.session_state.get("trader_bot_testnet", True))


def _get_active_bot_runtime_state() -> tuple[str, dict | None, dict]:
    use_testnet = _runtime_env_uses_testnet()
    runtime_keys = [_resolve_account_runtime_key(use_testnet), _resolve_primary_runtime_key()]
    selected_key = runtime_keys[0]
    runtime_db_state = None
    for runtime_key in dict.fromkeys(runtime_keys):
        runtime_db_state = get_cached_bot_runtime_db_state(runtime_key=runtime_key, limit=1)
        selected_key = runtime_key
        if runtime_db_state:
            break
    return selected_key, runtime_db_state, get_trader_bot_process_state(
        runtime_db_state=runtime_db_state,
        runtime_key=selected_key,
    )


def _is_process_running(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    except Exception:
        logger.debug("Falha ao verificar status do processo %s.", pid, exc_info=True)
        return False
    return True


def _runtime_session_pid_key(runtime_key: str | None) -> str:
    safe_key = "".join(char if char.isalnum() else "_" for char in str(runtime_key or "primary"))
    return f"trader_bot_pid_{safe_key[:120]}"


def get_trader_bot_process_state(runtime_db_state: dict | None = None, runtime_key: str | None = None):
    process_state_path = get_runtime_process_state_path(runtime_key)
    metadata = read_runtime_process_state(path=process_state_path)
    pid_key = _runtime_session_pid_key(runtime_key)
    pid = st.session_state.get(pid_key)
    if not pid and metadata:
        pid = metadata.get("pid")
    use_testnet = bool(st.session_state.get("trader_bot_testnet", True))
    if metadata and metadata.get("use_testnet") is not None:
        use_testnet = bool(metadata.get("use_testnet"))
        st.session_state.trader_bot_testnet = use_testnet
    running = _is_process_running(pid)
    embedded_runtime = str(os.getenv("TRADER_BOT_EMBEDDED", "")).strip().lower() in {"1", "true", "yes", "on"}
    if embedded_runtime:
        testnet_env = str(os.getenv("TESTNET", "true")).strip().lower()
        use_testnet = testnet_env in {"1", "true", "yes", "on", "y", "sim"}
    heartbeat_fresh = _runtime_heartbeat_is_recent(runtime_db_state)
    heartbeat_age_seconds = _runtime_heartbeat_age_seconds(runtime_db_state)
    if not running and metadata and not heartbeat_fresh:
        clear_runtime_process_state()
        metadata = None
    running_via_heartbeat = bool(not running and not embedded_runtime and heartbeat_fresh)
    if running_via_heartbeat:
        running = True
    if not running:
        st.session_state[pid_key] = None
        st.session_state.telegram_trading_bot_started = False
        pid = None
    else:
        st.session_state[pid_key] = pid
    if embedded_runtime and not running:
        # No modo all do Railway, o bot é iniciado fora do controle da sessão Streamlit.
        # Consideramos runtime online para evitar falso "OFF/Crash" na UI.
        running = True
    status_source = "pid"
    if embedded_runtime:
        status_source = "embedded"
    elif running_via_heartbeat:
        status_source = "heartbeat"
    elif not running:
        status_source = "offline"
    return {
        "running": running,
        "pid": pid,
        "use_testnet": use_testnet,
        "mode_label": "Testnet" if use_testnet else "Conta Real",
        "entrypoint": str(get_trader_bot_entrypoint()),
        "managed_externally": embedded_runtime,
        "metadata": metadata or {},
        "process_state_file": str(process_state_path),
        "status_source": status_source,
        "heartbeat_fresh": heartbeat_fresh,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "controllable": bool(pid) and not embedded_runtime,
    }


def _runtime_credential_slot(use_testnet: bool) -> str:
    return "testnet" if bool(use_testnet) else "real"


def _runtime_credential_session_key(use_testnet: bool) -> str:
    return f"runtime_exchange_credentials_{_runtime_credential_slot(use_testnet)}"


def _runtime_credential_env_names(use_testnet: bool) -> tuple[str, str]:
    exchange_name = ExchangeConfig.normalize_exchange_name(_runtime_credential_exchange_name())
    if exchange_name == "bybit":
        if bool(use_testnet):
            return "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_SECRET_KEY"
        return "BYBIT_API_KEY", "BYBIT_SECRET_KEY"
    if bool(use_testnet):
        return "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET_KEY"
    return "BINANCE_API_KEY", "BINANCE_SECRET_KEY"


def _runtime_credential_account_id(use_testnet: bool) -> str:
    base_account_id = str(os.getenv("SINGLE_USER_RUNTIME_ACCOUNT_ID", "env-primary")).strip() or "env-primary"
    return f"{base_account_id}-{_runtime_credential_slot(use_testnet)}"


def _runtime_credential_exchange_name() -> str:
    selected = str(st.session_state.get("runtime_exchange_name") or "").strip()
    configured = str(os.getenv("SINGLE_USER_RUNTIME_EXCHANGE", "binanceusdm")).strip() or "binanceusdm"
    return ExchangeConfig.normalize_exchange_name(selected or configured)


def _runtime_credential_user_id() -> int:
    auth_payload = st.session_state.get("dashboard_user_auth") or {}
    if auth_payload.get("user_id") is not None:
        try:
            return int(auth_payload.get("user_id"))
        except (TypeError, ValueError):
            pass
    raw_value = str(os.getenv("SINGLE_USER_RUNTIME_USER_ID", "0")).strip() or "0"
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return 0


def _mask_runtime_secret(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return "-"
    if len(raw_value) <= 8:
        return f"{raw_value[:2]}***{raw_value[-1:]}"
    return f"{raw_value[:4]}***{raw_value[-4:]}"


def _get_runtime_session_credentials(use_testnet: bool) -> dict | None:
    payload = st.session_state.get(_runtime_credential_session_key(use_testnet)) or {}
    api_key = str(payload.get("api_key") or "").strip()
    api_secret = str(payload.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return None
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "source": str(payload.get("source") or "session"),
        "persisted": bool(payload.get("persisted", False)),
    }


def _store_runtime_session_credentials(
    use_testnet: bool,
    *,
    api_key: str,
    api_secret: str,
    source: str = "session",
    persisted: bool = False,
) -> None:
    st.session_state[_runtime_credential_session_key(use_testnet)] = {
        "api_key": str(api_key or "").strip(),
        "api_secret": str(api_secret or "").strip(),
        "source": str(source or "session"),
        "persisted": bool(persisted),
    }


def _clear_runtime_session_credentials(use_testnet: bool) -> None:
    st.session_state.pop(_runtime_credential_session_key(use_testnet), None)


def _load_runtime_vault_credentials(use_testnet: bool) -> dict | None:
    try:
        from services.credential_vault import CredentialVault

        vault = CredentialVault(strict=False)
    except Exception:
        return None

    if not vault.is_configured():
        return None

    try:
        credentials = vault.load_exchange_credentials(
            db,
            user_id=_runtime_credential_user_id(),
            account_id=_runtime_credential_account_id(use_testnet),
            exchange=_runtime_credential_exchange_name(),
        )
    except Exception:
        return None

    api_key = str(credentials.get("api_key") or "").strip()
    api_secret = str(credentials.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return None

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "source": "vault",
        "persisted": True,
    }


def _resolve_runtime_credentials(use_testnet: bool) -> dict | None:
    session_credentials = _get_runtime_session_credentials(use_testnet)
    if session_credentials:
        return {
            **session_credentials,
            "source_label": "sessão",
        }

    vault_credentials = _load_runtime_vault_credentials(use_testnet)
    if vault_credentials:
        return {
            **vault_credentials,
            "source_label": "vault",
        }

    api_env_name, secret_env_name = _runtime_credential_env_names(use_testnet)
    api_key = str(os.getenv(api_env_name, "")).strip()
    api_secret = str(os.getenv(secret_env_name, "")).strip()
    if api_key and api_secret:
        return {
            "api_key": api_key,
            "api_secret": api_secret,
            "source": "env",
            "source_label": "env",
            "persisted": True,
        }

    return None


def _apply_runtime_credentials_to_process_env(process_env: dict, *, use_testnet: bool) -> dict:
    credentials = _resolve_runtime_credentials(use_testnet)
    target_api_env, target_secret_env = _runtime_credential_env_names(use_testnet)

    for env_name in (
        "BINANCE_API_KEY",
        "BINANCE_SECRET_KEY",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_SECRET_KEY",
        "BYBIT_API_KEY",
        "BYBIT_SECRET_KEY",
        "BYBIT_TESTNET_API_KEY",
        "BYBIT_TESTNET_SECRET_KEY",
    ):
        process_env.pop(env_name, None)

    if not credentials:
        return {
            "configured": False,
            "source": "none",
        }

    process_env[target_api_env] = str(credentials.get("api_key") or "").strip()
    process_env[target_secret_env] = str(credentials.get("api_secret") or "").strip()
    process_env["RUNTIME_CREDENTIAL_SOURCE"] = str(credentials.get("source") or "unknown")
    process_env["RUNTIME_CREDENTIAL_MODE"] = _runtime_credential_slot(use_testnet)
    return {
        "configured": True,
        "source": str(credentials.get("source") or "unknown"),
    }


def _build_runtime_entry_engine_lines(snapshot: dict | None) -> list[str]:
    snapshot = snapshot or {}
    long_setups: list[str] = []
    short_setups: list[str] = []

    if bool(snapshot.get("allow_long", True)):
        if bool(snapshot.get("enable_long_pullback", True)):
            long_setups.append("pullback_long")
        long_setups.append("trend_resume_long")

    if bool(snapshot.get("allow_short", True)):
        if bool(snapshot.get("enable_short_pullback", True)):
            short_setups.append("pullback_short")
        if bool(snapshot.get("enable_short_relief_rally", False)):
            short_setups.append("relief_rally_short")
        if bool(snapshot.get("enable_short_resume", False)):
            short_setups.append("trend_resume_short")

    return [
        f"Long ativos: {', '.join(long_setups) if long_setups else '-'}",
        f"Short ativos: {', '.join(short_setups) if short_setups else '-'}",
        (
            "Filtros-base: "
            f"RSI {snapshot.get('buy_rsi_signal', '-')}/{snapshot.get('sell_rsi_signal', '-')} | "
            f"ADX {snapshot.get('long_adx_threshold', '-')}/{snapshot.get('short_adx_threshold', '-')} | "
            f"vol {snapshot.get('long_volume_ratio_required', '-')}/{snapshot.get('short_volume_ratio_required', '-')}"
        ),
        (
            "Stops/Alvos: "
            f"long {snapshot.get('long_stop_loss_pct', '-')}/{snapshot.get('long_take_profit_pct', '-')} | "
            f"short {snapshot.get('short_stop_loss_pct', '-')}/{snapshot.get('short_take_profit_pct', '-')}"
        ),
        (
            "Horas bloqueadas UTC: "
            f"long {snapshot.get('blocked_long_entry_hours_utc', [])} | "
            f"short {snapshot.get('blocked_short_entry_hours_utc', [])}"
        ),
    ]


def _validate_live_runtime_preflight(use_testnet: bool):
    if bool(use_testnet):
        return True, ""

    if not bool(ProductionConfig.ENABLE_LIVE_EXECUTION):
        return False, "Modo real bloqueado: ENABLE_LIVE_EXECUTION=false."

    confirmation = str(ProductionConfig.LIVE_TRADING_CONFIRMATION or "").strip().upper()
    if confirmation != "EU_ASSUMO_RISCO":
        return False, "Modo real exige LIVE_TRADING_CONFIRMATION=EU_ASSUMO_RISCO."

    credentials = _resolve_runtime_credentials(use_testnet=False)
    if not credentials:
        return False, "Modo real exige credenciais reais configuradas na aba de runtime, no vault ou no ambiente."

    try:
        import config as runtime_config

        risk_per_trade = float(getattr(runtime_config, "RISK_PER_TRADE_PCT", 0.0) or 0.0)
    except Exception:
        risk_per_trade = 0.0

    max_risk_start = float(ProductionConfig.MAX_REAL_RISK_PER_TRADE_PCT_START or 0.25)
    if risk_per_trade > max_risk_start:
        return (
            False,
            f"Risco por trade alto para go-live ({risk_per_trade:.2f}% > {max_risk_start:.2f}%). "
            "Ajuste RISK_PER_TRADE_PCT antes de ligar em conta real.",
        )

    return True, ""


def start_trader_bot_process(use_testnet: bool = True):
    runtime_key = _resolve_account_runtime_key(use_testnet)
    current_state = get_trader_bot_process_state(
        runtime_db_state=get_cached_bot_runtime_db_state(runtime_key=runtime_key, limit=1),
        runtime_key=runtime_key,
    )
    if current_state["running"]:
        return True, f"Bot já está ativo (PID {current_state['pid']}) em {current_state.get('mode_label', 'modo desconhecido')}."

    entrypoint = get_trader_bot_entrypoint()
    if not entrypoint.exists():
        return False, f"Entrypoint não encontrado: {entrypoint}"

    preflight_ok, preflight_message = _validate_live_runtime_preflight(use_testnet=use_testnet)
    if not preflight_ok:
        return False, preflight_message

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process_env = os.environ.copy()
    process_env["TESTNET"] = "true" if bool(use_testnet) else "false"
    process_env["TRADER_BOT_LAUNCH_SOURCE"] = "dashboard"
    process_env["PYTHONUNBUFFERED"] = "1"
    process_env["RUNTIME_USE_ENV_CREDENTIALS"] = "1"
    process_env["SINGLE_USER_RUNTIME_USER_ID"] = str(_runtime_credential_user_id())
    process_env["SINGLE_USER_RUNTIME_ACCOUNT_ID"] = _runtime_credential_account_id(use_testnet)
    process_env["SINGLE_USER_RUNTIME_EXCHANGE"] = _runtime_credential_exchange_name()
    process_env["TRADER_BOT_RUNTIME_KEY"] = runtime_key
    process_env["BOT_EXECUTION_LOG_PATH"] = str(get_runtime_execution_log_path(runtime_key))
    credential_runtime = _apply_runtime_credentials_to_process_env(process_env, use_testnet=bool(use_testnet))
    process_state_path = get_runtime_process_state_path(runtime_key)
    stop_request_path = get_runtime_stop_request_path(runtime_key)
    stdout_log_path = get_runtime_stdout_log_path(runtime_key)
    stderr_log_path = get_runtime_stderr_log_path(runtime_key)
    BOT_EXECUTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    clear_runtime_stop_request(path=stop_request_path)

    stdout_handle = None
    stderr_handle = None
    try:
        stdout_handle = open(stdout_log_path, "a", encoding="utf-8")
        stderr_handle = open(stderr_log_path, "a", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(entrypoint)],
            cwd=str(entrypoint.parent),
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
            env=process_env,
        )
    except Exception as exc:
        logger.warning("Falha ao iniciar processo do bot trader.", exc_info=True)
        return False, str(exc)
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()

    time.sleep(1.0)
    if process.poll() is not None:
        clear_runtime_process_state(path=process_state_path)
        stderr_tail = tail_text_file(stderr_log_path, max_lines=40, max_chars=4000)
        error_hint = stderr_tail or "Processo encerrou logo após o start. Verifique os logs do runner."
        return False, error_hint

    write_runtime_process_state(
        pid=process.pid,
        use_testnet=bool(use_testnet),
        entrypoint=str(entrypoint),
        source="dashboard",
        command=f"{sys.executable} {entrypoint.name}",
        path=process_state_path,
        extra={
            "runtime_key": runtime_key,
            "user_id": _runtime_credential_user_id(),
            "account_id": _runtime_credential_account_id(use_testnet),
            "stdout_log": str(stdout_log_path),
            "stderr_log": str(stderr_log_path),
            "execution_log": str(get_runtime_execution_log_path(runtime_key)),
        },
    )

    st.session_state.trader_bot_testnet = bool(use_testnet)
    st.session_state[_runtime_session_pid_key(runtime_key)] = process.pid
    st.session_state.telegram_trading_bot_started = True
    clear_dashboard_data_caches()
    mode_label = "Testnet" if bool(use_testnet) else "Conta Real"
    credential_hint = ""
    if credential_runtime.get("configured"):
        credential_hint = f" | credencial: {credential_runtime.get('source')}"
    return True, f"Bot trader iniciado em background (PID {process.pid}) | modo: {mode_label}{credential_hint}."


def stop_trader_bot_process():
    use_testnet = bool(st.session_state.get("trader_bot_testnet", True))
    runtime_key = _resolve_account_runtime_key(use_testnet)
    process_state_path = get_runtime_process_state_path(runtime_key)
    stop_request_path = get_runtime_stop_request_path(runtime_key)
    current_state = get_trader_bot_process_state(
        runtime_db_state=get_cached_bot_runtime_db_state(runtime_key=runtime_key, limit=1),
        runtime_key=runtime_key,
    )
    pid = current_state.get("pid")
    if not pid and not current_state.get("heartbeat_fresh"):
        return False, "Nenhum bot trader ativo nesta sessão."

    request_runtime_stop(path=stop_request_path)
    try:
        if pid:
            deadline = time.time() + 8.0
            while _is_process_running(pid) and time.time() < deadline:
                time.sleep(0.5)
            if _is_process_running(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    except Exception as exc:
        logger.warning("Falha ao encerrar processo do bot trader.", exc_info=True)
        return False, str(exc)

    st.session_state[_runtime_session_pid_key(runtime_key)] = None
    st.session_state.telegram_trading_bot_started = False
    clear_runtime_process_state(path=process_state_path)
    clear_runtime_stop_request(path=stop_request_path)
    clear_dashboard_data_caches()
    if pid:
        return True, f"Solicitação de parada enviada para o PID {pid}."
    return True, "Solicitação de parada enviada para o runtime monitorado pelo heartbeat."


def render_runtime_credentials_panel(
    section_key: str = "runtime_credentials",
    default_use_testnet: bool = True,
):
    st.markdown("### 🔐 Credenciais do Runtime")
    st.caption(
        "Aqui a gente separa `testnet` e `conta real`. "
        "Se o vault estiver configurado, as chaves podem ficar persistidas com criptografia; "
        "senão elas ficam só na sessão atual da dashboard."
    )

    try:
        from services.credential_vault import CredentialVault

        vault = CredentialVault(strict=False)
    except Exception:
        vault = None

    vault_ready = bool(vault and vault.is_configured())
    exchange_name = _runtime_credential_exchange_name()
    slot_order = (
        [(True, "Testnet"), (False, "Conta Real")]
        if bool(default_use_testnet)
        else [(False, "Conta Real"), (True, "Testnet")]
    )
    tabs = st.tabs([label for _, label in slot_order])

    for (use_testnet, _label), tab in zip(slot_order, tabs):
        slot = _runtime_credential_slot(use_testnet)
        resolved = _resolve_runtime_credentials(use_testnet)
        env_api_name, env_secret_name = _runtime_credential_env_names(use_testnet)

        with tab:
            status_col1, status_col2, status_col3 = st.columns(3)
            with status_col1:
                st.metric("Configurada", "SIM" if resolved else "NÃO")
            with status_col2:
                st.metric("Origem Ativa", (resolved or {}).get("source_label", "-").upper())
            with status_col3:
                st.metric("Exchange", exchange_name)

            if resolved:
                st.caption(
                    f"API Key: {_mask_runtime_secret(resolved.get('api_key', ''))} | "
                    f"API Secret: {_mask_runtime_secret(resolved.get('api_secret', ''))}"
                )
            else:
                st.caption("Nenhuma credencial ativa neste slot ainda.")

            if use_testnet:
                st.info(
                    "No fluxo atual, `testnet` costuma rodar em paper. "
                    "Mesmo assim, este slot já fica separado e pronto para futuras validações."
                )
            else:
                st.warning(
                    "As credenciais reais só entram no processo quando você seleciona `Conta Real` e liga o runtime."
                )

            st.caption(
                f"Fallback por ambiente: `{env_api_name}` / `{env_secret_name}`. "
                f"Conta salva no vault: `{_runtime_credential_account_id(use_testnet)}`."
            )

            with st.form(f"{section_key}_{slot}_form"):
                api_key = st.text_input(
                    "API Key",
                    type="password",
                    key=f"{section_key}_{slot}_api_key",
                    help="Cole aqui a chave da exchange correspondente a este ambiente.",
                )
                api_secret = st.text_input(
                    "API Secret",
                    type="password",
                    key=f"{section_key}_{slot}_api_secret",
                    help="Cole aqui o segredo correspondente à mesma chave.",
                )
                action_col1, action_col2, action_col3 = st.columns(3)
                with action_col1:
                    save_session = st.form_submit_button("Salvar nesta sessão")
                with action_col2:
                    save_vault = st.form_submit_button("Salvar no Vault", disabled=not vault_ready)
                with action_col3:
                    clear_session = st.form_submit_button("Limpar sessão")

                if save_session:
                    if api_key and api_secret:
                        _store_runtime_session_credentials(
                            use_testnet,
                            api_key=api_key,
                            api_secret=api_secret,
                            source="session",
                            persisted=False,
                        )
                        st.success("Credenciais carregadas nesta sessão da dashboard.")
                        st.rerun()
                    else:
                        st.warning("Preencha API Key e API Secret para salvar nesta sessão.")

                if save_vault:
                    if not vault_ready:
                        st.error("Vault não configurado. Defina `CREDENTIAL_ENCRYPTION_KEY` para persistir com criptografia.")
                    elif api_key and api_secret:
                        vault.store_exchange_credentials(
                            db,
                            user_id=_runtime_credential_user_id(),
                            account_id=_runtime_credential_account_id(use_testnet),
                            exchange=exchange_name,
                            api_key=api_key,
                            api_secret=api_secret,
                            credential_alias=f"runtime-{slot}",
                            permissions_read=True,
                            permissions_trade=True,
                            permissions_withdraw=False,
                            permission_status="unknown",
                            token_status="unknown",
                            reconciliation_status="unknown",
                            notes=f"Credencial local do runtime ({slot}).",
                        )
                        _store_runtime_session_credentials(
                            use_testnet,
                            api_key=api_key,
                            api_secret=api_secret,
                            source="vault",
                            persisted=True,
                        )
                        st.success("Credenciais persistidas com criptografia e carregadas na sessão.")
                        st.rerun()
                    else:
                        st.warning("Preencha API Key e API Secret antes de salvar no vault.")

                if clear_session:
                    _clear_runtime_session_credentials(use_testnet)
                    st.success("Credenciais removidas da sessão atual.")
                    st.rerun()

            if not vault_ready:
                st.caption("Vault indisponível neste ambiente. As chaves podem ser usadas nesta sessão, mas não ficam persistidas com criptografia.")


def render_bot_telegram_notifications_panel(section_key: str = "bot_runtime_telegram"):
    st.markdown("### 📱 Notificações Telegram")
    st.caption("Opcional. O bot trader pode ser ligado sem Telegram; este bloco serve apenas para alertas e mensagens.")

    telegram_service_available = is_telegram_service_available()
    if not telegram_service_available:
        st.warning("Biblioteca Telegram indisponível neste ambiente. As notificações ficam desativadas, mas o bot pode operar.")
        st.session_state.telegram_notifications = False
        return

    telegram_bot = get_or_init_session_telegram_bot()
    has_env_secrets = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    is_configured = bool(has_env_secrets or (telegram_bot and telegram_bot.is_configured()))

    status_col1, status_col2, status_col3 = st.columns(3)
    with status_col1:
        st.metric("Telegram", "CONFIGURADO" if is_configured else "OPCIONAL")
    with status_col2:
        current_enabled = bool(st.session_state.get("telegram_notifications", False))
        st.metric("Alertas", "ON" if current_enabled else "OFF")
    with status_col3:
        st.metric("Origem", "ENV" if has_env_secrets else ("SESSÃO" if is_configured else "NÃO CONFIGURADO"))

    if is_configured:
        toggle_label = "Ativar alertas Telegram"
        st.session_state.telegram_notifications = st.checkbox(
            toggle_label,
            value=bool(st.session_state.get("telegram_notifications", False)),
            key=f"{section_key}_enabled",
            help="Liga/desliga envio de alertas operacionais pelo Telegram nesta sessão.",
        )

        action_col1, action_col2 = st.columns(2)
        with action_col1:
            if st.button("📤 Testar Telegram", key=f"{section_key}_test"):
                try:
                    success, message = run_async_task_sync(
                        telegram_bot.send_custom_message("🧪 Teste das notificações do bot trader.")
                    )
                    if success:
                        st.success("Mensagem de teste enviada com sucesso.")
                    else:
                        st.error(message)
                except Exception as exc:
                    st.error(f"Falha ao testar Telegram: {exc}")
        with action_col2:
            if not has_env_secrets and st.button("🧹 Limpar Configuração da Sessão", key=f"{section_key}_clear"):
                telegram_bot.disable()
                st.session_state.telegram_notifications = False
                st.success("Configuração temporária do Telegram removida desta sessão.")
                st.rerun()

        if has_env_secrets:
            st.caption("Telegram configurado por variáveis de ambiente. Para trocar os dados, ajuste o ambiente do deploy.")
        else:
            st.caption("Telegram configurado apenas nesta sessão da dashboard.")
        return

    with st.expander("Configurar Telegram nesta aba", expanded=False):
        st.markdown(
            """
            1. Crie o bot no `@BotFather`
            2. Obtenha o `Chat ID`
            3. Salve aqui para receber alertas nesta sessão
            """
        )
        tg_col1, tg_col2 = st.columns(2)
        with tg_col1:
            telegram_token = st.text_input(
                "Token do Bot",
                type="password",
                key=f"{section_key}_token",
                help="Token gerado pelo @BotFather.",
            )
        with tg_col2:
            telegram_chat_id = st.text_input(
                "Chat ID",
                key=f"{section_key}_chat_id",
                help="ID do chat ou canal que vai receber os alertas.",
            )

        action_col1, action_col2 = st.columns(2)
        with action_col1:
            if st.button("✅ Salvar Telegram", key=f"{section_key}_save"):
                if telegram_token and telegram_chat_id:
                    success, message = telegram_bot.configure(telegram_token, telegram_chat_id)
                    if success:
                        st.session_state.telegram_notifications = True
                        st.success("Telegram configurado com sucesso para esta sessão.")
                        st.rerun()
                    else:
                        st.error(message)
                else:
                    st.warning("Preencha Token e Chat ID.")
        with action_col2:
            if telegram_token and telegram_chat_id and st.button("📤 Testar Agora", key=f"{section_key}_test_temp"):
                success, message = telegram_bot.configure(telegram_token, telegram_chat_id)
                if success:
                    try:
                        sent, sent_message = run_async_task_sync(
                            telegram_bot.send_custom_message("🧪 Teste das notificações do bot trader.")
                        )
                        if sent:
                            st.success("Mensagem de teste enviada.")
                        else:
                            st.error(sent_message)
                    except Exception as exc:
                        st.error(f"Falha ao testar Telegram: {exc}")
                else:
                    st.error(message)


@st.fragment(run_every=5)
def render_trader_bot_runtime_monitor(runtime_key: str, section_key: str = "bot_runtime_monitor"):
    get_cached_bot_runtime_db_state.clear()
    runtime_db_state = get_cached_bot_runtime_db_state(runtime_key=runtime_key, limit=1)
    trader_bot_state = get_trader_bot_process_state(runtime_db_state=runtime_db_state, runtime_key=runtime_key)
    process_metadata = trader_bot_state.get("metadata") or {}
    payload = (runtime_db_state or {}).get("state_payload") or {}
    position = payload.get("position") or {}
    risk_state = payload.get("risk_state") or {}
    snapshot = payload.get("snapshot") or {}
    entry_runtime = payload.get("entry_runtime") or {}
    last_signal_details = payload.get("last_signal_details") or {}

    monitor_col1, monitor_col2, monitor_col3, monitor_col4 = st.columns(4)
    with monitor_col1:
        st.metric("Fonte Status", str(trader_bot_state.get("status_source") or "-").upper())
    with monitor_col2:
        st.metric("Heartbeat", _format_age_label(trader_bot_state.get("heartbeat_age_seconds")))
    with monitor_col3:
        st.metric("Controle", "SIM" if trader_bot_state.get("controllable") else "MONITOR")
    with monitor_col4:
        st.metric("Process File", "OK" if process_metadata else "AUSENTE")

    if process_metadata:
        st.caption(
            f"Process state: {trader_bot_state.get('process_state_file')} | "
            f"source={process_metadata.get('source') or '-'} | "
            f"started_at={process_metadata.get('started_at') or '-'}"
        )

    runtime_tab1, runtime_tab2, runtime_tab3, runtime_tab4 = st.tabs(["Operação", "Entradas", "Posição", "Logs"])

    with runtime_tab1:
        if runtime_db_state:
            op_col1, op_col2, op_col3, op_col4 = st.columns(4)
            with op_col1:
                st.metric("DB Status", runtime_db_state.get("status") or "-")
            with op_col2:
                st.metric("Último Candle", str(runtime_db_state.get("last_candle_timestamp") or "-"))
            with op_col3:
                st.metric("Último Sinal", str(runtime_db_state.get("last_signal") or "-"))
            with op_col4:
                st.metric("Preço Sinal", f"{float(runtime_db_state.get('last_signal_price') or 0.0):.2f}" if runtime_db_state.get("last_signal_price") is not None else "-")

            st.caption(
                f"Motivo do último sinal: {runtime_db_state.get('last_signal_reason') or '-'} | "
                f"Strategy version: {runtime_db_state.get('strategy_version') or '-'}"
            )
            if runtime_db_state.get("blocked"):
                st.warning(f"Runtime bloqueado: {runtime_db_state.get('block_reason') or 'sem motivo informado'}")
            if runtime_db_state.get("last_error"):
                st.error(f"Último erro persistido: {runtime_db_state.get('last_error')}")
        else:
            st.info("Sem snapshot persistido do runtime ainda.")

    with runtime_tab2:
        started_at_raw = entry_runtime.get("started_at_utc") or process_metadata.get("started_at")
        started_at = _parse_runtime_datetime(started_at_raw)
        first_entry_at = _parse_runtime_datetime(entry_runtime.get("first_entry_at_utc"))
        current_wait_seconds = None
        if started_at is not None and first_entry_at is None:
            current_wait_seconds = max((datetime.now(started_at.tzinfo) - started_at).total_seconds(), 0.0)

        entry_col1, entry_col2, entry_col3, entry_col4 = st.columns(4)
        with entry_col1:
            first_order_label = "OK" if entry_runtime.get("first_entry_at_utc") else "AGUARDANDO"
            st.metric("Primeira Ordem", first_order_label)
        with entry_col2:
            if entry_runtime.get("first_entry_delay_sec") is not None:
                delay_label = f"{float(entry_runtime.get('first_entry_delay_sec') or 0.0):.1f}s"
            else:
                delay_label = _format_age_label(current_wait_seconds)
            st.metric("Tempo até 1ª Ordem", delay_label)
        with entry_col3:
            st.metric("Sinais Ação", int(entry_runtime.get("actionable_signal_count", 0) or 0))
        with entry_col4:
            st.metric("Entradas Sessão", int(entry_runtime.get("entry_count", 0) or 0))

        if started_at_raw:
            st.caption(
                f"Runtime iniciado em: {started_at_raw} | "
                f"Candles processados: {int(entry_runtime.get('processed_candles', 0) or 0)}"
            )

        st.markdown("#### Motor de Entrada")
        for line in _build_runtime_entry_engine_lines(snapshot):
            st.write(f"- {line}")

        if last_signal_details:
            st.markdown("#### Último Sinal Avaliado")
            signal_col1, signal_col2, signal_col3, signal_col4 = st.columns(4)
            with signal_col1:
                st.metric("Sinal", str(last_signal_details.get("signal") or "-").upper())
            with signal_col2:
                st.metric("Setup", str(last_signal_details.get("setup_name") or "-"))
            with signal_col3:
                score_value = last_signal_details.get("score")
                st.metric("Score", "-" if score_value is None else f"{float(score_value):.1f}")
            with signal_col4:
                atr_value = last_signal_details.get("atr")
                st.metric("ATR", "-" if atr_value is None else f"{float(atr_value):.4f}")
            st.caption(f"Motivo: {last_signal_details.get('reason') or '-'}")

        last_entry = entry_runtime.get("last_entry") or {}
        if last_entry:
            st.markdown("#### Última Entrada")
            entry_info_col1, entry_info_col2, entry_info_col3, entry_info_col4 = st.columns(4)
            with entry_info_col1:
                st.metric("Side", str(last_entry.get("side") or "-").upper())
            with entry_info_col2:
                st.metric("Setup", str(last_entry.get("setup_name") or "-"))
            with entry_info_col3:
                last_entry_score = last_entry.get("score")
                st.metric("Score", "-" if last_entry_score is None else f"{float(last_entry_score):.1f}")
            with entry_info_col4:
                st.metric("Modo Execução", str(last_entry.get("execution_mode") or "-"))
            st.caption(
                f"Preço: {float(last_entry.get('entry_price') or 0.0):.2f} | "
                f"Qty: {float(last_entry.get('quantity') or 0.0):.6f} | "
                f"Candle: {last_entry.get('candle_timestamp') or '-'} | "
                f"Motivo: {last_entry.get('reason') or '-'}"
            )

        last_blocked_entry = entry_runtime.get("last_blocked_entry") or {}
        if last_blocked_entry:
            st.markdown("#### Última Entrada Bloqueada")
            st.warning(
                f"{str(last_blocked_entry.get('signal') or '-').upper()} | "
                f"setup={last_blocked_entry.get('setup_name') or '-'} | "
                f"score={last_blocked_entry.get('score') if last_blocked_entry.get('score') is not None else '-'} | "
                f"etapa={last_blocked_entry.get('stage') or '-'}"
            )
            st.caption(
                f"Candle: {last_blocked_entry.get('candle_timestamp') or '-'} | "
                f"Motivo: {last_blocked_entry.get('reason') or '-'}"
            )

    with runtime_tab3:
        risk_col1, risk_col2, risk_col3 = st.columns(3)
        with risk_col1:
            st.metric("Daily PnL %", f"{float(risk_state.get('daily_realized_pct', 0.0) or 0.0):.2f}%")
        with risk_col2:
            st.metric("Losses Seguidas", int(risk_state.get("consecutive_losses", 0) or 0))
        with risk_col3:
            st.metric("Bloqueado", "SIM" if risk_state.get("blocked") else "NÃO")

        if position:
            pos_col1, pos_col2, pos_col3, pos_col4 = st.columns(4)
            with pos_col1:
                st.metric("Side", str(position.get("side") or "-").upper())
            with pos_col2:
                st.metric("Entry", f"{float(position.get('entry_price') or 0.0):.2f}")
            with pos_col3:
                st.metric("Stop Atual", f"{float(position.get('current_stop') or 0.0):.2f}")
            with pos_col4:
                st.metric("Alvo Parcial", f"{float(position.get('partial_target') or 0.0):.2f}")
            st.caption(
                f"Best price: {float(position.get('best_price') or 0.0):.2f} | "
                f"Execution profile: {position.get('execution_profile') or '-'} | "
                f"Entry timestamp: {position.get('entry_timestamp') or '-'}"
            )
        else:
            st.info("Sem posição aberta no snapshot atual.")

        with st.expander("Snapshot bruto do runtime", expanded=False):
            st.json(
                {
                    "risk_state": risk_state,
                    "position": position,
                    "snapshot": snapshot,
                    "entry_runtime": entry_runtime,
                    "last_signal_details": last_signal_details,
                }
            )

    with runtime_tab4:
        log_options = {
            "Execução": BOT_EXECUTION_LOG_PATH,
            "Runner stdout": BOT_RUNNER_STDOUT_LOG_PATH,
            "Runner stderr": BOT_RUNNER_STDERR_LOG_PATH,
        }
        selected_log = st.selectbox(
            "Arquivo de log",
            options=list(log_options.keys()),
            key=f"{section_key}_log_file",
        )
        log_lines = st.slider(
            "Linhas finais",
            min_value=40,
            max_value=400,
            value=120,
            step=20,
            key=f"{section_key}_log_lines",
        )
        log_text = tail_text_file(log_options[selected_log], max_lines=log_lines)
        if log_text:
            st.code(log_text, language="text")
        else:
            st.info("Sem conteúdo de log disponível neste arquivo.")


def render_live_go_live_status_panel(section_key: str = "bot_go_live_status"):
    report = get_cached_live_go_live_report()
    if not report:
        st.info("Checklist de go-live indisponível no momento.")
        return None

    checks = report.get("checks") or []
    fail_count = sum(1 for item in checks if item.get("status") == "FAIL")
    warn_count = sum(1 for item in checks if item.get("status") == "WARN")
    info_count = sum(1 for item in checks if item.get("status") == "INFO")

    st.markdown("### 🛡️ Prontidão Conta Real")
    status_col1, status_col2, status_col3, status_col4 = st.columns(4)
    with status_col1:
        st.metric(
            "Estrutura Alinhada",
            "SIM" if bool(report.get("structure_aligned_for_conservative_live")) else "NÃO",
        )
    with status_col2:
        st.metric(
            "Live Armado",
            "SIM" if bool(report.get("live_mode_armed")) else "NÃO",
        )
    with status_col3:
        st.metric("Checks FAIL", fail_count)
    with status_col4:
        st.metric("Checks WARN", warn_count)

    st.caption(
        f"Relatório: {report.get('latest_backtest_report') or '-'} | "
        f"INFO={info_count} | gerado em {report.get('generated_at_utc') or '-'}"
    )

    with st.expander("Ver checklist automático de go-live", expanded=False):
        for item in checks:
            status = str(item.get("status") or "-").upper()
            detail = str(item.get("detail") or "-")
            action = str(item.get("action") or "").strip()
            if status == "FAIL":
                st.error(f"[{status}] {item.get('name')}: {detail}")
            elif status == "WARN":
                st.warning(f"[{status}] {item.get('name')}: {detail}")
            elif status == "PASS":
                st.success(f"[{status}] {item.get('name')}: {detail}")
            else:
                st.info(f"[{status}] {item.get('name')}: {detail}")
            if action:
                st.caption(f"Ação: {action}")

    return report


def render_trader_bot_runtime_controls(
    section_key: str = "bot_trader_runtime",
    allow_start: bool = True,
    block_reason: str = "",
):
    selected_runtime_mode_for_key = str(
        st.session_state.get(f"{section_key}_runtime_mode")
        or ("testnet" if bool(st.session_state.get("trader_bot_testnet", True)) else "real")
    )
    runtime_key = _resolve_account_runtime_key(selected_runtime_mode_for_key == "testnet")
    runtime_db_state = get_cached_bot_runtime_db_state(runtime_key=runtime_key, limit=1)
    trader_bot_state = get_trader_bot_process_state(runtime_db_state=runtime_db_state, runtime_key=runtime_key)
    managed_externally = bool(trader_bot_state.get("managed_externally"))
    runtime_symbol = str(os.getenv("SYMBOL", AppConfig.DEFAULT_SYMBOL)).strip() or AppConfig.DEFAULT_SYMBOL
    runtime_timeframe = str(os.getenv("TIMEFRAME", AppConfig.DEFAULT_TIMEFRAME)).strip() or AppConfig.DEFAULT_TIMEFRAME

    render_dashboard_strip(
        "Use esta área para controlar o processo local do bot sem sair da dashboard.",
        badges=[
            _build_status_pill("Runtime", "ON" if trader_bot_state.get("running") else "OFF", "accent" if trader_bot_state.get("running") else "danger"),
            _build_status_pill("Modo", trader_bot_state.get("mode_label", "Testnet"), "warm"),
            _build_status_pill("Mercado", f"{runtime_symbol} · {runtime_timeframe}", "default"),
            _build_status_pill("Entrada", Path(trader_bot_state.get("entrypoint", "bot_runner.py")).name, "default"),
        ],
    )

    bot_runtime_col1, bot_runtime_col2, bot_runtime_col3, bot_runtime_col4 = st.columns(4)
    with bot_runtime_col1:
        st.metric("Status Runtime", "ON" if trader_bot_state.get("running") else "OFF")
    with bot_runtime_col2:
        st.metric("PID", trader_bot_state.get("pid") or ("EMBEDDED" if managed_externally else "-"))
    with bot_runtime_col3:
        st.metric("Modo", trader_bot_state.get("mode_label", "Testnet"))
    with bot_runtime_col4:
        st.metric(
            "Entrypoint",
            Path(trader_bot_state.get("entrypoint", "bot_runner.py")).name,
        )

    if runtime_db_state:
        heartbeat_value = str(runtime_db_state.get("last_heartbeat_at") or "-")
        last_signal_label = str(runtime_db_state.get("last_signal") or "-")
        last_signal_reason = str(runtime_db_state.get("last_signal_reason") or "-")
        current_position = str(runtime_db_state.get("position_side") or "flat")
        runtime_db_col1, runtime_db_col2, runtime_db_col3, runtime_db_col4 = st.columns(4)
        with runtime_db_col1:
            st.metric("DB Status", runtime_db_state.get("status") or "-")
        with runtime_db_col2:
            st.metric("Ultimo Candle", str(runtime_db_state.get("last_candle_timestamp") or "-"))
        with runtime_db_col3:
            st.metric("Ultimo Sinal", last_signal_label)
        with runtime_db_col4:
            st.metric("Posicao", current_position)

        st.caption(
            f"Heartbeat: {heartbeat_value} | "
            f"Runtime DB: {runtime_key} | "
            f"Motivo do ultimo sinal: {last_signal_reason}"
        )
        if runtime_db_state.get("blocked"):
            st.warning(f"Runtime bloqueado no DB: {runtime_db_state.get('block_reason') or 'sem motivo informado'}")
        if runtime_db_state.get("last_error"):
            st.error(f"Último erro persistido: {runtime_db_state['last_error']}")
    else:
        st.caption(
            f"Sem snapshot persistido do runtime ainda para {runtime_symbol} {runtime_timeframe}. "
            "Ligue o bot e aguarde o primeiro heartbeat."
        )

    selected_runtime_mode = st.radio(
        "Ambiente do Bot Trader",
        options=["testnet", "real"],
        index=0 if bool(st.session_state.get("trader_bot_testnet", True)) else 1,
        horizontal=True,
        key=f"{section_key}_runtime_mode",
        format_func=lambda value: "Testnet (seguro)" if value == "testnet" else "Conta Real (cuidado)",
        disabled=bool(trader_bot_state.get("running")) or managed_externally,
    )
    selected_use_testnet = selected_runtime_mode == "testnet"
    if not bool(trader_bot_state.get("running")):
        st.session_state.trader_bot_testnet = bool(selected_use_testnet)

    render_runtime_credentials_panel(
        section_key=f"{section_key}_credentials",
        default_use_testnet=selected_use_testnet,
    )
    selected_credentials = _resolve_runtime_credentials(selected_use_testnet)
    if not selected_use_testnet and selected_credentials and str(selected_credentials.get("source") or "") == "session":
        st.info(
            "Credencial real carregada só nesta sessão. "
            "Ela já serve para ligar o runtime, mas o checklist automático de go-live continua mais rígido "
            "e só considera `env` ou `vault` como persistência."
        )

    real_preflight_ok = True
    real_preflight_message = ""
    if not selected_use_testnet:
        real_preflight_ok, real_preflight_message = _validate_live_runtime_preflight(use_testnet=False)
        if real_preflight_ok:
            st.warning("Conta Real selecionada. Faça a virada apenas quando o piloto estiver realmente decidido.")
        else:
            st.error(f"Conta Real bloqueada agora: {real_preflight_message}")

    live_go_live_report = render_live_go_live_status_panel(section_key=f"{section_key}_go_live")
    if not selected_use_testnet and live_go_live_report and not bool(live_go_live_report.get("structure_aligned_for_conservative_live")):
        st.warning("A estrutura do projeto ainda não está suficientemente alinhada para um piloto real conservador.")

    credential_source = str((selected_credentials or {}).get("source") or "não configurada")
    render_dashboard_strip(
        "A seleção de ambiente fica travada enquanto o processo estiver em execução, para evitar mudanças acidentais no meio da operação.",
        badges=[
            _build_status_pill("Ambiente", "Testnet" if selected_use_testnet else "Conta Real", "accent" if selected_use_testnet else "danger"),
            _build_status_pill("Credencial", credential_source, "warm" if credential_source not in {"não configurada", "none"} else "danger"),
        ],
    )

    bot_control_col1, bot_control_col2, bot_control_col3 = st.columns([1, 1, 0.8])
    with bot_control_col1:
        if st.button(
            "▶️ Ligar Bot Trader",
            key=f"{section_key}_start",
            disabled=managed_externally or bool(trader_bot_state.get("running")) or not bool(allow_start) or (not selected_use_testnet and not real_preflight_ok),
        ):
            success, message = start_trader_bot_process(use_testnet=selected_use_testnet)
            if success:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
    with bot_control_col2:
        if st.button(
            "⏹️ Parar Bot Trader",
            key=f"{section_key}_stop",
            disabled=managed_externally or not trader_bot_state.get("running"),
        ):
            success, message = stop_trader_bot_process()
            if success:
                st.warning(message)
                st.rerun()
            else:
                st.error(message)
    with bot_control_col3:
        if st.button(
            "🔄 Atualizar Painel",
            key=f"{section_key}_refresh",
        ):
            st.rerun()

    if managed_externally:
        st.info("Runtime gerenciado externamente (RAILWAY_SERVICE_MODE=all). Status ON sincronizado pelo ambiente.")
    if not bool(allow_start):
        st.warning(block_reason or "Runtime bloqueado para esta conta.")

    st.caption(
        "Controle manual do processo local via bot_runner.py. "
        "Use para subir ou derrubar o bot trader sem sair da dashboard. "
        "O botão de ligar injeta TESTNET=true/false conforme o modo escolhido."
    )
    st.markdown("### 👀 Monitor Operacional")
    st.caption("Este bloco atualiza sozinho a cada 5 segundos enquanto a aba estiver aberta.")
    render_trader_bot_runtime_monitor(runtime_key=runtime_key, section_key=f"{section_key}_monitor")
    return trader_bot_state


def render_export_data_panel(symbol: str, timeframe: str, key_prefix: str = "dashboard_export"):
    st.markdown("### 💾 Exportações")
    st.caption("Baixe dados atuais, histórico de sinais e artefatos do backtest sem sair da análise.")

    current_data = st.session_state.get("current_data")
    signals_history = st.session_state.get("signals_history") or []
    backtest_results = st.session_state.get("backtest_results")
    backtest_engine = get_or_init_backtest_engine() if backtest_results else None

    export_col1, export_col2 = st.columns(2)
    with export_col1:
        st.markdown("#### 📊 Dados de Mercado")
        if current_data is not None:
            csv_data = current_data.to_csv()
            st.download_button(
                label="⬇️ Baixar OHLCV CSV",
                data=csv_data,
                file_name=f"{symbol}_{timeframe}_{format_brazil_time(fmt='%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key=f"{key_prefix}_ohlcv",
            )
        else:
            st.info("Nenhum dado atual disponível para exportar.")

    with export_col2:
        st.markdown("#### 🚨 Histórico de Sinais")
        if signals_history:
            signals_df = pd.DataFrame(signals_history)
            csv_data = signals_df.to_csv(index=False)
            st.download_button(
                label="⬇️ Baixar Sinais CSV",
                data=csv_data,
                file_name=f"sinais_{format_brazil_time(fmt='%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key=f"{key_prefix}_signals",
            )
        else:
            st.info("Nenhum sinal disponível para exportar.")

    if backtest_results:
        result_symbol = backtest_results.get("symbol") or symbol
        result_timeframe = backtest_results.get("timeframe") or timeframe
        st.markdown("#### 🔬 Resultados de Backtest")
        bt_export_col1, bt_export_col2 = st.columns(2)

        with bt_export_col1:
            trade_df = backtest_engine.get_trade_summary_df() if backtest_engine else pd.DataFrame()
            if not trade_df.empty:
                csv_data = trade_df.to_csv(index=False)
                st.download_button(
                    label="⬇️ Baixar Trades CSV",
                    data=csv_data,
                    file_name=f"backtest_trades_{result_symbol}_{result_timeframe}_{format_brazil_time(fmt='%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    key=f"{key_prefix}_trades",
                )
            else:
                st.info("Nenhum trade de backtest disponível para exportar.")

        with bt_export_col2:
            portfolio_values = backtest_results.get("portfolio_values")
            if portfolio_values is not None and len(portfolio_values) > 0:
                portfolio_df = pd.DataFrame(portfolio_values)
                csv_data = portfolio_df.to_csv(index=False)
                st.download_button(
                    label="⬇️ Baixar Portfolio CSV",
                    data=csv_data,
                    file_name=f"backtest_portfolio_{result_symbol}_{result_timeframe}_{format_brazil_time(fmt='%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    key=f"{key_prefix}_portfolio",
                )
            else:
                st.info("Nenhum portfolio de backtest disponível para exportar.")


def apply_edge_guardrail(signal: str, symbol: str, timeframe: str, strategy_version: str = None):
    """Downgrade actionable signals when live paper performance is degraded."""
    if signal not in ACTIONABLE_SIGNALS or not ProductionConfig.ENABLE_EDGE_GUARDRAIL:
        return signal, None

    try:
        edge_summary = get_cached_edge_monitor_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
    except Exception as exc:
        logger.warning("Falha ao consultar edge monitor: %s", exc)
        return signal, None

    if (
        edge_summary.get("status") == "degraded"
        and edge_summary.get("paper_closed_trades", 0) >= ProductionConfig.MIN_PAPER_TRADES_FOR_EDGE_GUARDRAIL
    ):
        return "NEUTRO", edge_summary

    return signal, edge_summary


def apply_risk_guardrail(
    signal: str,
    entry_price: float,
    strategy_settings: dict,
    runtime_allowed: bool = True,
    runtime_block_reason: str = None,
    system_health_ok: bool = True,
    system_health_reason: str = None,
):
    if signal not in ACTIONABLE_SIGNALS:
        return signal, None

    risk_plan = get_risk_management_service().evaluate_risk_engine(
        entry_price=float(entry_price),
        stop_loss_pct=strategy_settings.get("stop_loss_pct", 0.0) or 0.0,
        symbol=strategy_settings.get("symbol"),
        timeframe=strategy_settings.get("timeframe"),
        strategy_version=strategy_settings.get("strategy_version"),
        runtime_allowed=runtime_allowed,
        runtime_block_reason=runtime_block_reason,
        system_health_ok=system_health_ok,
        system_health_reason=system_health_reason,
    )
    if not risk_plan.get("allowed"):
        return "NEUTRO", risk_plan
    return signal, risk_plan


def build_operational_signal_state(
    analytical_signal: str,
    entry_price: float,
    strategy_settings: dict,
    regime_evaluation: dict | None = None,
):
    final_signal = analytical_signal
    edge_summary = None
    risk_plan = None
    governance_summary = None
    operational_runtime_allowed = bool(strategy_settings.get("runtime_allowed", True))
    operational_block_reason = None
    operational_block_source = None
    edge_allowed = True
    edge_block_reason = None
    current_regime = (regime_evaluation or {}).get("regime")
    if (regime_evaluation or {}).get("parabolic"):
        current_regime = "parabolic"

    if operational_runtime_allowed:
        edge_signal, edge_summary = apply_edge_guardrail(
            analytical_signal,
            strategy_settings.get("symbol"),
            strategy_settings.get("timeframe"),
            strategy_version=strategy_settings.get("strategy_version"),
        )
        edge_allowed = edge_signal in ACTIONABLE_SIGNALS or analytical_signal not in ACTIONABLE_SIGNALS
        if edge_summary and edge_signal == "NEUTRO":
            edge_block_reason = edge_summary.get("status_message") or "Edge monitor bloqueou a leitura."
        final_signal, risk_plan = apply_risk_guardrail(
            analytical_signal,
            float(entry_price),
            strategy_settings,
            runtime_allowed=True,
            system_health_ok=edge_allowed,
            system_health_reason=edge_block_reason,
        )
        if edge_summary and not edge_allowed:
            operational_block_reason = edge_block_reason
            operational_block_source = "edge_guardrail"
            operational_runtime_allowed = False
        elif risk_plan and not risk_plan.get("allowed"):
            operational_block_reason = (
                risk_plan.get("risk_reason") or risk_plan.get("reason") or "Risco operacional bloqueou a entrada."
            )
            operational_block_source = "risk_guardrail"
            operational_runtime_allowed = False
    else:
        final_signal = "NEUTRO"
        _, risk_plan = apply_risk_guardrail(
            analytical_signal,
            float(entry_price),
            strategy_settings,
            runtime_allowed=False,
            runtime_block_reason=strategy_settings.get("runtime_block_reason", "Runtime bloqueado"),
        )
        operational_block_reason = (
            (risk_plan or {}).get("risk_reason")
            or (risk_plan or {}).get("reason")
            or strategy_settings.get("runtime_block_reason", "Runtime bloqueado")
        )
        operational_block_source = "runtime_governance"

    try:
        governance_summary = get_cached_governance_evaluation(
            symbol=strategy_settings.get("symbol"),
            timeframe=strategy_settings.get("timeframe"),
            strategy_version=strategy_settings.get("strategy_version"),
            current_regime=current_regime,
        )
    except Exception as exc:
        logger.warning("Falha ao avaliar governanca adaptativa: %s", exc)
        governance_summary = None

    if (
        governance_summary
        and analytical_signal in ACTIONABLE_SIGNALS
        and not operational_block_reason
        and governance_summary.get("governance_mode") == "blocked"
    ):
        final_signal = "NEUTRO"
        operational_runtime_allowed = False
        operational_block_reason = governance_summary.get("action_reason") or "Governanca adaptativa bloqueou a leitura."
        operational_block_source = "adaptive_governance"
    elif governance_summary and risk_plan and risk_plan.get("allowed") and governance_summary.get("governance_mode") == "reduced":
        risk_plan["governance_mode"] = "reduced"
        risk_plan["governance_reduction_multiplier"] = governance_summary.get("governance_reduction_multiplier", 1.0)
        risk_plan["risk_reason"] = risk_plan.get("risk_reason") or governance_summary.get("action_reason")

    return {
        "final_signal": final_signal,
        "edge_summary": edge_summary,
        "risk_plan": risk_plan,
        "governance_summary": governance_summary,
        "runtime_allowed": operational_runtime_allowed,
        "block_reason": operational_block_reason,
        "block_source": operational_block_source,
    }


def get_effective_strategy_settings(
    symbol: str,
    timeframe: str,
    require_volume: bool = False,
    require_trend: bool = False,
    avoid_ranging: bool = False,
) -> dict:
    active_profile = get_cached_active_strategy_profile(symbol=symbol, timeframe=timeframe)
    trading_bot = st.session_state.get("trading_bot")
    default_context_timeframe = None

    if active_profile:
        runtime_allowed_signal_directions = AppConfig.get_runtime_allowed_signal_directions(
            timeframe,
            active_profile.get("market_state"),
            active_profile.get("allowed_market_states"),
            active_profile.get("allowed_setup_types"),
        )
        settings = {
            "symbol": symbol,
            "timeframe": timeframe,
            "context_timeframe": active_profile.get("context_timeframe"),
            "rsi_period": active_profile.get("rsi_period"),
            "rsi_min": active_profile.get("rsi_min"),
            "rsi_max": active_profile.get("rsi_max"),
            "stop_loss_pct": active_profile.get("stop_loss_pct") or ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT,
            "take_profit_pct": active_profile.get("take_profit_pct") or ProductionConfig.DEFAULT_LIVE_TAKE_PROFIT_PCT,
            "require_volume": bool(active_profile.get("require_volume", False)),
            "require_trend": bool(active_profile.get("require_trend", False)),
            "avoid_ranging": bool(active_profile.get("avoid_ranging", False)),
            "market_state": active_profile.get("market_state"),
            "allowed_market_states": active_profile.get("allowed_market_states") or [],
            "signal_profile": active_profile.get("market_pattern") or active_profile.get("setup_type"),
            "allowed_signal_profiles": active_profile.get("allowed_market_patterns") or active_profile.get("allowed_setup_types") or [],
            "active_profile": active_profile,
            "source": "active_profile",
            "runtime_allowed": True,
            "runtime_block_reason": "",
            "allowed_signal_directions": runtime_allowed_signal_directions,
            "allowed_execution_setups": runtime_allowed_signal_directions,
        }
    else:
        runtime_allowed_signal_directions = AppConfig.get_runtime_allowed_signal_directions(timeframe)
        runtime_block_reason = ""
        runtime_allowed = True
        runtime_source = "session"
        if ProductionConfig.REQUIRE_ACTIVE_PROFILE_FOR_RUNTIME:
            runtime_allowed = False
            runtime_source = "blocked_no_active_profile"
            runtime_block_reason = (
                "Nenhum perfil operacional ativo para este mercado/timeframe. "
                "Runtime bloqueado ate existir perfil ativo."
            )
        settings = {
            "symbol": symbol,
            "timeframe": timeframe,
            "context_timeframe": default_context_timeframe,
            "rsi_period": getattr(trading_bot, "rsi_period", 14) if trading_bot else 14,
            "rsi_min": getattr(trading_bot, "rsi_min", 20) if trading_bot else 20,
            "rsi_max": getattr(trading_bot, "rsi_max", 80) if trading_bot else 80,
            "stop_loss_pct": ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT,
            "take_profit_pct": ProductionConfig.DEFAULT_LIVE_TAKE_PROFIT_PCT,
            "require_volume": require_volume,
            "require_trend": require_trend,
            "avoid_ranging": avoid_ranging,
            "market_state": None,
            "allowed_market_states": [],
            "signal_profile": None,
            "allowed_signal_profiles": [],
            "active_profile": None,
            "source": runtime_source,
            "runtime_allowed": runtime_allowed,
            "runtime_block_reason": runtime_block_reason,
            "allowed_signal_directions": runtime_allowed_signal_directions,
            "allowed_execution_setups": runtime_allowed_signal_directions,
        }

    settings["strategy_version"] = build_strategy_version(
        symbol=symbol,
        timeframe=timeframe,
        context_timeframe=settings.get("context_timeframe"),
        rsi_period=settings["rsi_period"],
        rsi_min=settings["rsi_min"],
        rsi_max=settings["rsi_max"],
        stop_loss_pct=settings.get("stop_loss_pct", 0.0) or 0.0,
        take_profit_pct=settings.get("take_profit_pct", 0.0) or 0.0,
        require_volume=settings["require_volume"],
        require_trend=settings["require_trend"],
        avoid_ranging=settings.get("avoid_ranging", False),
    )
    return settings

# Helper function for timestamp comparison
def _compare_timestamps(ts1, ts2):
    """
    Safely compare timestamps, handling timezone-aware/naive differences
    Returns True if ts1 < ts2
    """
    try:
        # Convert both to naive datetime for comparison
        if hasattr(ts1, 'tzinfo') and ts1.tzinfo is not None:
            # If ts1 is timezone-aware, convert to Brazil timezone then make naive
            ts1_naive = ts1.astimezone(BRAZIL_TZ).replace(tzinfo=None) if hasattr(ts1, 'astimezone') else ts1.replace(tzinfo=None)
        else:
            # If ts1 is already naive, use as is
            ts1_naive = ts1

        if hasattr(ts2, 'tzinfo') and ts2.tzinfo is not None:
            # If ts2 is timezone-aware, convert to Brazil timezone then make naive
            ts2_naive = ts2.astimezone(BRAZIL_TZ).replace(tzinfo=None) if hasattr(ts2, 'astimezone') else ts2.replace(tzinfo=None)
        else:
            # If ts2 is already naive, use as is
            ts2_naive = ts2

        return ts1_naive < ts2_naive
    except Exception:
        # If comparison fails, assume it's a new signal
        return True


def _coerce_brazil_naive_datetime(value):
    if value is None:
        return None
    try:
        if isinstance(value, pd.Timestamp):
            if value.tzinfo is not None:
                return value.tz_convert(BRAZIL_TZ).tz_localize(None).to_pydatetime()
            return value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(BRAZIL_TZ).replace(tzinfo=None)
            return value
        parsed = pd.to_datetime(value, errors="coerce")
        if parsed is None or pd.isna(parsed):
            return None
        if getattr(parsed, "tzinfo", None) is not None:
            return parsed.tz_convert(BRAZIL_TZ).tz_localize(None).to_pydatetime()
        return parsed.to_pydatetime()
    except Exception:
        return None


def _coerce_market_timestamp_to_brazil_naive(value):
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if parsed is None or pd.isna(parsed):
            return None
        return parsed.tz_convert(BRAZIL_TZ).tz_localize(None).to_pydatetime()
    except Exception:
        return None


def _extract_market_timestamp_reference(df):
    if df is None or getattr(df, "empty", True):
        return None
    try:
        if "timestamp" in df.columns:
            raw_value = df["timestamp"].iloc[-1]
        else:
            raw_value = df.index[-1]
    except Exception:
        return None
    return _coerce_market_timestamp_to_brazil_naive(raw_value)


def _compute_data_age_seconds(last_update, now_reference=None):
    if last_update is None:
        return None
    try:
        now_value = _coerce_brazil_naive_datetime(now_reference) if now_reference is not None else get_brazil_datetime_naive()
        last_naive = _coerce_brazil_naive_datetime(last_update)
        if now_value is None or last_naive is None:
            return None
        return max((now_value - last_naive).total_seconds(), 0.0)
    except Exception:
        return None


def _is_data_fresh(last_update, max_age_seconds=MAX_SIGNAL_DATA_AGE_SECONDS, now_reference=None):
    age_seconds = _compute_data_age_seconds(last_update, now_reference=now_reference)
    if age_seconds is None:
        return False, None
    return age_seconds <= float(max_age_seconds), age_seconds


def _build_stale_data_operational_state(age_seconds, max_age_seconds=MAX_SIGNAL_DATA_AGE_SECONDS):
    age_label = f"{age_seconds:.0f}s" if age_seconds is not None else "desconhecida"
    reason = (
        f"Dados de mercado desatualizados ({age_label} > {max_age_seconds}s). "
        "Operacao bloqueada ate receber dado recente."
    )
    return {
        "final_signal": "NEUTRO",
        "edge_summary": None,
        "risk_plan": {
            "allowed": False,
            "risk_mode": "blocked",
            "reason": reason,
            "risk_reason": reason,
        },
        "governance_summary": None,
        "runtime_allowed": False,
        "block_reason": reason,
        "block_source": "stale_data",
    }


def build_strategy_evaluation_display_df(evaluations):
    if not evaluations:
        return pd.DataFrame()

    rows = []
    for evaluation in evaluations:
        rows.append(
            {
                "Criado em": evaluation.get("created_at_br"),
                "Simbolo": evaluation.get("symbol"),
                "Timeframe": evaluation.get("timeframe"),
                "Versao": evaluation.get("strategy_version"),
                "Origem": evaluation.get("evaluation_type"),
                "Score": round(float(evaluation.get("quality_score", 0.0) or 0.0), 2),
                "PF Backtest": round(float(evaluation.get("avg_profit_factor", 0.0) or 0.0), 2),
                "PF OOS": round(float(evaluation.get("avg_out_of_sample_profit_factor", 0.0) or 0.0), 2),
                "PF Paper": round(float(evaluation.get("paper_profit_factor", 0.0) or 0.0), 2),
                "Paper Fechados": int(evaluation.get("paper_closed_trades", 0) or 0),
                "Edge": evaluation.get("edge_status"),
                "Governanca": evaluation.get("governance_status"),
            }
        )

    return pd.DataFrame(rows)


def build_backtest_robustness_matrix_display_df(rows):
    if not rows:
        return pd.DataFrame()

    display_rows = []
    for row in rows:
        display_rows.append(
            {
                "Símbolo": row.get("symbol"),
                "Família": row.get("symbol_family_label"),
                "Horizonte (d)": int(row.get("horizon_days", 0) or 0),
                "Score": round(float(row.get("quality_score", 0.0) or 0.0), 2),
                "Retorno %": round(float(row.get("total_return_pct", 0.0) or 0.0), 2),
                "PF": round(float(row.get("profit_factor", 0.0) or 0.0), 2),
                "OOS %": round(float(row.get("oos_return_pct", 0.0) or 0.0), 2),
                "OOS PF": round(float(row.get("oos_profit_factor", 0.0) or 0.0), 2),
                "WF Pass %": round(float(row.get("walk_forward_pass_rate_pct", 0.0) or 0.0), 2),
                "Drawdown %": round(float(row.get("max_drawdown", 0.0) or 0.0), 2),
                "Trades": int(row.get("total_trades", 0) or 0),
                "Robusto": "Sim" if bool(row.get("robust_candidate", False)) else "Não",
                "Run ID": row.get("saved_run_id"),
            }
        )

    return pd.DataFrame(display_rows)


def build_backtest_robustness_breakdown_display_df(rows, group_label):
    if not rows:
        return pd.DataFrame()

    display_rows = []
    for row in rows:
        display_rows.append(
            {
                group_label: row.get("label"),
                "Cenários": int(row.get("runs", 0) or 0),
                "Positivos": int(row.get("profitable_runs", 0) or 0),
                "Positivos %": round(float(row.get("profitable_rate_pct", 0.0) or 0.0), 2),
                "OOS Aprovados": int(row.get("oos_passed_runs", 0) or 0),
                "WF Aprovados": int(row.get("walk_forward_passed_runs", 0) or 0),
                "Robustos": int(row.get("robust_runs", 0) or 0),
                "Score Global": round(float(row.get("robustness_score", 0.0) or 0.0), 2),
                "Score Médio": round(float(row.get("avg_quality_score", 0.0) or 0.0), 2),
                "Retorno Med. %": round(float(row.get("median_return_pct", 0.0) or 0.0), 2),
                "PF Med.": round(float(row.get("median_profit_factor", 0.0) or 0.0), 2),
                "Drawdown Pior %": round(float(row.get("worst_drawdown", 0.0) or 0.0), 2),
            }
        )

    return pd.DataFrame(display_rows)


def calculate_backtest_score_pct(stats):
    score = 0.0
    max_score = 100.0
    total_return_pct = float(stats.get('total_return_pct', 0.0) or 0.0)
    win_rate = float(stats.get('win_rate', 0.0) or 0.0)
    max_drawdown = float(stats.get('max_drawdown', 0.0) or 0.0)
    sharpe_ratio = float(stats.get('sharpe_ratio', 0.0) or 0.0)

    if total_return_pct > 50:
        score += 40
    elif total_return_pct > 20:
        score += 30
    elif total_return_pct > 10:
        score += 20
    elif total_return_pct > 0:
        score += 10

    if win_rate > 70:
        score += 25
    elif win_rate > 60:
        score += 20
    elif win_rate > 50:
        score += 15
    elif win_rate > 40:
        score += 10

    if max_drawdown < 5:
        score += 20
    elif max_drawdown < 10:
        score += 15
    elif max_drawdown < 15:
        score += 10
    elif max_drawdown < 25:
        score += 5

    if sharpe_ratio > 2:
        score += 15
    elif sharpe_ratio > 1:
        score += 10
    elif sharpe_ratio > 0.5:
        score += 5

    return (score / max_score) * 100.0


def render_backtest_portfolio_section(results, stats, result_symbol, result_timeframe):
    portfolio_values = results.get("portfolio_values") or []
    if not portfolio_values:
        return

    portfolio_df = pd.DataFrame(portfolio_values)
    if portfolio_df.empty or not {"timestamp", "portfolio_value"}.issubset(portfolio_df.columns):
        return

    portfolio_df["timestamp"] = pd.to_datetime(portfolio_df["timestamp"])
    portfolio_df["portfolio_value"] = pd.to_numeric(portfolio_df["portfolio_value"], errors="coerce")
    portfolio_df = (
        portfolio_df
        .dropna(subset=["timestamp", "portfolio_value"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    if portfolio_df.empty:
        return

    running_max = portfolio_df["portfolio_value"].cummax()
    portfolio_df["drawdown_pct"] = (
        (running_max - portfolio_df["portfolio_value"]) / running_max.replace(0, np.nan)
    ) * 100
    equity_diagnostics = results.get("equity_diagnostics") or {}

    st.markdown("---")
    st.subheader("📈 Evolução do Portfólio")
    st.caption(
        "Curva do capital ao longo do backtest para visualizar aceleração, devolução de lucro e pontos de estresse."
    )

    fig_portfolio = go.Figure()
    fig_portfolio.add_trace(
        go.Scatter(
            x=portfolio_df["timestamp"],
            y=portfolio_df["portfolio_value"],
            mode="lines",
            name="Portfólio",
            line=dict(color="#0f766e", width=2.5),
        )
    )

    benchmark_values = results.get("benchmark_values") or []
    if benchmark_values:
        benchmark_df = pd.DataFrame(benchmark_values)
        if not benchmark_df.empty and {"timestamp", "benchmark_value"}.issubset(benchmark_df.columns):
            benchmark_df["timestamp"] = pd.to_datetime(benchmark_df["timestamp"])
            benchmark_df["benchmark_value"] = pd.to_numeric(benchmark_df["benchmark_value"], errors="coerce")
            benchmark_df = benchmark_df.dropna(subset=["timestamp", "benchmark_value"]).sort_values("timestamp")
            if not benchmark_df.empty:
                fig_portfolio.add_trace(
                    go.Scatter(
                        x=benchmark_df["timestamp"],
                        y=benchmark_df["benchmark_value"],
                        mode="lines",
                        name="Buy & Hold",
                        line=dict(color="#f59e0b", width=2, dash="dot"),
                    )
                )

    fig_portfolio.add_hline(
        y=stats["initial_balance"],
        line_dash="dash",
        line_color="#6b7280",
        annotation_text="Capital inicial",
    )
    fig_portfolio.update_layout(
        title=f"Evolução do Portfólio - {result_symbol} {result_timeframe}",
        xaxis_title="Data",
        yaxis_title="Valor do portfólio ($)",
        height=430,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=60, b=10),
    )
    st.plotly_chart(fig_portfolio, width="stretch")

    drawdown_col1, drawdown_col2, drawdown_col3, drawdown_col4 = st.columns(4)
    with drawdown_col1:
        st.metric("Drawdown Médio", f"{float(equity_diagnostics.get('average_drawdown_pct', 0.0) or 0.0):.2f}%")
    with drawdown_col2:
        st.metric("Recuperação Máx.", int(equity_diagnostics.get("max_recovery_periods", 0) or 0))
    with drawdown_col3:
        st.metric("Payoff Ratio", f"{float(stats.get('payoff_ratio', 0.0) or 0.0):.2f}")
    with drawdown_col4:
        st.metric("Giveback no Topo", f"{float(equity_diagnostics.get('profit_giveback_pct', 0.0) or 0.0):.2f}%")

    fig_drawdown = go.Figure()
    fig_drawdown.add_trace(
        go.Scatter(
            x=portfolio_df["timestamp"],
            y=portfolio_df["drawdown_pct"].fillna(0.0),
            mode="lines",
            name="Drawdown %",
            line=dict(color="#dc2626", width=2),
            fill="tozeroy",
            fillcolor="rgba(220, 38, 38, 0.12)",
        )
    )
    fig_drawdown.update_layout(
        title=f"Curva de Drawdown - {result_symbol} {result_timeframe}",
        xaxis_title="Data",
        yaxis_title="Drawdown %",
        height=280,
        margin=dict(l=10, r=10, t=50, b=10),
    )
    st.plotly_chart(fig_drawdown, width="stretch")


def _get_realtime_chart_snapshot(symbol, timeframe, fallback_data, limit=200):
    trading_bot = st.session_state.get("trading_bot")
    if trading_bot is None:
        return fallback_data, None

    try:
        stream_client = trading_bot._get_realtime_stream_client(symbol=symbol, timeframe=timeframe)
        if stream_client is None:
            logger.warning(
                "Stream client indisponivel para snapshot realtime (%s %s). Usando fallback.",
                symbol,
                timeframe,
            )
            return fallback_data, None
        chart_data = stream_client.get_market_data(
            limit=limit,
            timeout=2.0,
            include_current_candle=True,
        )
        return chart_data, stream_client.get_current_status()
    except Exception as exc:
        logger.warning(
            "Falha ao obter snapshot realtime do grafico %s %s: %s",
            symbol,
            timeframe,
            exc,
        )
        return fallback_data, None


@st.fragment(run_every=2)
def render_live_market_chart(symbol, timeframe, fallback_data):
    chart_limit = 200
    if fallback_data is not None and not fallback_data.empty:
        chart_limit = max(len(fallback_data.index), 200)

    chart_data, stream_status = _get_realtime_chart_snapshot(
        symbol=symbol,
        timeframe=timeframe,
        fallback_data=fallback_data,
        limit=chart_limit,
    )

    if chart_data is None or chart_data.empty:
        st.warning("Grafico realtime indisponivel no momento.")
        return

    fig = make_subplots(
        rows=1, cols=1,
        shared_xaxes=True,
        subplot_titles=("Preço",),
    )

    fig.add_trace(
        go.Candlestick(
            x=chart_data.index,
            open=chart_data["open"],
            high=chart_data["high"],
            low=chart_data["low"],
            close=chart_data["close"],
            name="Preço",
        ),
        row=1, col=1
    )

    chart_signals = pd.DataFrame(st.session_state.signals_history) if st.session_state.signals_history else pd.DataFrame()
    if not chart_signals.empty:
        chart_signals = chart_signals.copy()
        chart_signals["timestamp"] = pd.to_datetime(chart_signals["timestamp"], errors="coerce")
        chart_signals = chart_signals.dropna(subset=["timestamp"])
        if "timeframe" not in chart_signals.columns:
            chart_signals["timeframe"] = timeframe
        for signal_column in ["candidate_signal", "approved_signal", "blocked_signal", "block_reason"]:
            if signal_column not in chart_signals.columns:
                chart_signals[signal_column] = None
        chart_signals = chart_signals[
            (chart_signals["symbol"] == symbol)
            & (chart_signals["timeframe"] == timeframe)
        ]

        def _add_signal_trace(signal_df, expected_signal, marker_symbol, marker_color, marker_size, name, blocked=False):
            filtered = signal_df[signal_df["signal_value"] == expected_signal]
            if filtered.empty:
                return
            hover_text = (
                filtered.get("block_reason", pd.Series([""] * len(filtered))).fillna("-")
                if blocked else
                filtered.get("candidate_signal", pd.Series([""] * len(filtered))).fillna("-")
            )
            fig.add_trace(
                go.Scatter(
                    x=filtered["timestamp"],
                    y=filtered["price"],
                    mode="markers",
                    marker=dict(symbol=marker_symbol, size=marker_size, color=marker_color),
                    name=name,
                    text=hover_text,
                    hovertemplate="%{x}<br>Preco %{y:.6f}<br>%{text}<extra></extra>",
                    showlegend=True,
                ),
                row=1, col=1
            )

        candidate_markers = chart_signals[
            chart_signals["candidate_signal"].isin(list(ACTIONABLE_SIGNALS))
        ].copy()
        if not candidate_markers.empty:
            candidate_markers["signal_value"] = candidate_markers["candidate_signal"]
            _add_signal_trace(candidate_markers, "COMPRA", "triangle-up-open", "rgba(0, 160, 0, 0.7)", 15, "Candidato Compra")
            _add_signal_trace(candidate_markers, "VENDA", "triangle-down-open", "rgba(190, 0, 0, 0.7)", 15, "Candidato Venda")

        approved_markers = chart_signals[
            chart_signals["approved_signal"].isin(list(ACTIONABLE_SIGNALS))
        ].copy()
        if not approved_markers.empty:
            approved_markers["signal_value"] = approved_markers["approved_signal"]
            _add_signal_trace(approved_markers, "COMPRA", "triangle-up", "green", 18, "Aprovado Compra")
            _add_signal_trace(approved_markers, "VENDA", "triangle-down", "red", 18, "Aprovado Venda")

        blocked_markers = chart_signals[
            chart_signals["blocked_signal"].isin(list(ACTIONABLE_SIGNALS))
        ].copy()
        if not blocked_markers.empty:
            blocked_markers["signal_value"] = blocked_markers["blocked_signal"]
            _add_signal_trace(blocked_markers, "COMPRA", "x", "orange", 13, "Bloqueado Compra", blocked=True)
            _add_signal_trace(blocked_markers, "VENDA", "x", "orange", 13, "Bloqueado Venda", blocked=True)

    if "is_closed" in chart_data.columns and not bool(chart_data["is_closed"].iloc[-1]):
        current_row = chart_data.iloc[-1]
        fig.add_annotation(
            x=chart_data.index[-1],
            y=float(current_row["close"]),
            text="Tempo real",
            showarrow=True,
            arrowhead=1,
            ax=35,
            ay=-35,
            bgcolor="rgba(15, 118, 110, 0.15)",
        )

    fig.update_layout(
        title=f"{symbol} - {timeframe}",
        height=520,
        xaxis_rangeslider_visible=False,
        showlegend=True,
    )
    fig.update_yaxes(title_text="Preço ($)", row=1, col=1)

    st.plotly_chart(fig, width="stretch")

    if stream_status and stream_status.get("connected"):
        provider = stream_status.get("provider") or "stream"
        message_age = stream_status.get("last_message_age_sec")
        age_label = f"{message_age}s" if message_age is not None else "agora"
        mode_label = "inclui vela em formação" if "is_closed" in chart_data.columns and not bool(chart_data["is_closed"].iloc[-1]) else "somente candles fechados"
        st.caption(f"Mercado em tempo real via {provider} | ultima mensagem ha {age_label} | {mode_label}.")
    else:
        st.caption("Grafico exibido com snapshot fallback; stream realtime nao confirmou conexao neste ciclo.")


@st.fragment(run_every=5)
def render_bot_operation_snapshot(symbol, timeframe, stream_status=None):
    runtime_key, runtime_db_state, trader_bot_state = _get_active_bot_runtime_state()
    heartbeat_age = _runtime_heartbeat_age_seconds(runtime_db_state)
    heartbeat_fresh = bool(trader_bot_state.get("heartbeat_fresh"))
    running = bool(trader_bot_state.get("running"))
    blocked = bool((runtime_db_state or {}).get("blocked"))
    last_error = str((runtime_db_state or {}).get("last_error") or "").strip()

    if blocked:
        status_label = "BLOQUEADO"
        status_kind = "danger"
    elif last_error:
        status_label = "ERRO"
        status_kind = "danger"
    elif running and heartbeat_fresh:
        status_label = "OPERANDO"
        status_kind = "accent"
    elif running:
        status_label = "ONLINE"
        status_kind = "warm"
    else:
        status_label = "AGUARDANDO"
        status_kind = "default"

    render_dashboard_strip(
        "Painel operacional do bot: prioriza runtime, heartbeat, sinal e posicao em vez de mostrar apenas candle de mercado.",
        badges=[
            _build_status_pill("Bot", status_label, status_kind),
            _build_status_pill("Modo", trader_bot_state.get("mode_label", "Testnet"), "warm"),
            _build_status_pill("Mercado", f"{symbol} · {timeframe}", "default"),
            _build_status_pill("Heartbeat", _format_age_label(heartbeat_age), "accent" if heartbeat_fresh else "danger"),
        ],
    )

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    with metric_col1:
        st.metric("Status do Bot", status_label)
    with metric_col2:
        st.metric("Ambiente", trader_bot_state.get("mode_label", "Testnet"))
    with metric_col3:
        st.metric("Ultimo Sinal", str((runtime_db_state or {}).get("last_signal") or "-").upper())
    with metric_col4:
        st.metric("Posicao", str((runtime_db_state or {}).get("position_side") or "flat").upper())

    detail_col1, detail_col2, detail_col3 = st.columns(3)
    with detail_col1:
        st.metric("Ultimo Candle", str((runtime_db_state or {}).get("last_candle_timestamp") or "-"))
    with detail_col2:
        st.metric("Heartbeat", _format_age_label(heartbeat_age))
    with detail_col3:
        pid_label = trader_bot_state.get("pid") or (
            "EMBEDDED" if trader_bot_state.get("managed_externally") else trader_bot_state.get("status_source", "-")
        )
        st.metric("Origem", str(pid_label).upper())

    last_reason = str((runtime_db_state or {}).get("last_signal_reason") or "").strip()
    if runtime_db_state:
        st.caption(f"Runtime DB: {runtime_key} | Motivo do ultimo sinal: {last_reason or '-'}")
    else:
        st.warning("Ainda nao encontrei snapshot do bot no banco. Assim que o evo-bot gravar o primeiro heartbeat, esta area atualiza sozinha.")

    if stream_status:
        stream_label = "conectado" if stream_status.get("connected") else "conectando"
        st.caption(f"Stream de mercado auxiliar: {stream_label}. Ele serve como contexto; a decisao vem do runtime do bot.")

    if blocked:
        st.warning(f"Runtime bloqueado: {(runtime_db_state or {}).get('block_reason') or 'sem motivo informado'}")
    if last_error:
        st.error(f"Ultimo erro persistido: {last_error}")

    payload = (runtime_db_state or {}).get("state_payload") or {}
    last_signal_details = payload.get("last_signal_details") or {}
    if last_signal_details:
        with st.expander("Detalhes do ultimo sinal do bot", expanded=False):
            st.json(last_signal_details)


def render_market_operational_summary(
    *,
    symbol,
    timeframe,
    rsi_period,
    rsi_min,
    rsi_max,
    last_candle,
    candidate_signal,
    approved_signal,
    blocked_signal,
    analytical_block_reason,
    signal,
    operational_state,
    operational_block_reason,
    operational_block_source,
    data_age_seconds,
    risk_plan,
    guardrail_edge_summary,
    governance_summary,
    context_evaluation,
    regime_evaluation,
    structure_evaluation,
    confirmation_evaluation,
    entry_quality_evaluation,
    scenario_evaluation,
    trade_decision,
    hard_block_evaluation,
):
    st.subheader("🔍 Análise Atual")

    if (
        guardrail_edge_summary
        and guardrail_edge_summary.get("status") == "degraded"
        and guardrail_edge_summary.get("paper_closed_trades", 0) >= ProductionConfig.MIN_PAPER_TRADES_FOR_EDGE_GUARDRAIL
    ):
        st.warning(
            "Guardrail ativo: leitura degradada no paper trade. "
            "O sinal foi bloqueado ate recuperar edge live."
        )

    if risk_plan:
        if risk_plan.get("allowed"):
            st.info(
                f"Plano de risco ({risk_plan.get('risk_mode', 'normal')}): arriscar "
                f"{risk_plan.get('risk_per_trade_pct', 0):.2f}% "
                f"(${risk_plan.get('risk_amount', 0):.2f}) | "
                f"Posicao ${risk_plan.get('position_notional', 0):.2f} | "
                f"Qtd {risk_plan.get('quantity', 0):.6f}"
            )
        else:
            st.warning(f"Risk guardrail: {risk_plan.get('risk_reason') or risk_plan.get('reason')}")

        if operational_block_source == "stale_data":
            st.warning(
                f"Bloqueio por stale data: ultimo update ha "
                f"{(data_age_seconds or 0):.0f}s (limite {MAX_SIGNAL_DATA_AGE_SECONDS}s)."
            )

        portfolio_risk_summary = get_risk_management_service().get_portfolio_risk_summary()
        st.caption(
            f"Portfolio paper: {portfolio_risk_summary.get('open_trades', 0)} trades abertos | "
            f"Risco aberto {portfolio_risk_summary.get('total_open_risk_pct', 0):.2f}% | "
            f"Notional ${portfolio_risk_summary.get('total_open_position_notional', 0):.2f} | "
            f"Drawdown {portfolio_risk_summary.get('current_drawdown_pct', 0):.2f}% | "
            f"Losing streak {portfolio_risk_summary.get('consecutive_losses', 0)} | "
            f"Modo {portfolio_risk_summary.get('risk_mode', 'normal')}"
        )
        if not portfolio_risk_summary.get("circuit_breaker_allowed", True):
            st.error(
                f"Circuit breaker: {portfolio_risk_summary.get('circuit_breaker_reason')} | "
                f"PnL diário {portfolio_risk_summary.get('daily_realized_pnl_pct', 0):.2f}% | "
                f"Losses consecutivos {portfolio_risk_summary.get('consecutive_losses', 0)}"
            )
        else:
            st.caption(
                f"PnL diário paper: {portfolio_risk_summary.get('daily_realized_pnl_pct', 0):.2f}% | "
                f"Losses consecutivos: {portfolio_risk_summary.get('consecutive_losses', 0)}"
            )

    analysis_col1, analysis_col2 = st.columns(2)

    with analysis_col1:
        st.info(f"""
        **Par:** {symbol}  
        **Timeframe:** {timeframe}  
        **Preço Atual:** ${last_candle['close']:.6f}  
        **RSI({rsi_period}):** {last_candle['rsi']:.2f}  
        **MACD:** {last_candle['macd']:.4f}  
        **MACD Signal:** {last_candle['macd_signal']:.4f}  
        **Volume:** {last_candle['volume']:,.0f}  
        **Volume MA:** {last_candle['volume_ma']:,.0f}
        """)
        st.caption(
            f"Candidato: {candidate_signal} | "
            f"Aprovado: {approved_signal or 'NEUTRO'} | "
            f"Bloqueado: {blocked_signal or '-'}"
        )
        st.caption(
            f"Status operacional: {'liberado' if operational_state.get('runtime_allowed') and not operational_block_reason else 'bloqueado'} | "
            f"Sinal operacional: {signal} | "
            f"Motivo operacional: {operational_block_reason or '-'}"
        )
        if governance_summary:
            st.caption(
                f"Governanca adaptativa: {governance_summary.get('governance_status', 'research')} | "
                f"Modo {governance_summary.get('governance_mode', 'blocked')} | "
                f"Alinhamento {governance_summary.get('alignment_status', 'insufficient')} | "
                f"Regime atual {governance_summary.get('current_regime') or '-'} "
                f"({governance_summary.get('current_regime_status', 'unknown')})"
            )
            if governance_summary.get("allowed_regimes") or governance_summary.get("blocked_regimes"):
                st.caption(
                    f"Regimes aprovados: {', '.join(governance_summary.get('allowed_regimes', [])) or '-'} | "
                    f"Regimes reduzidos: {', '.join(governance_summary.get('reduced_regimes', [])) or '-'} | "
                    f"Regimes bloqueados: {', '.join(governance_summary.get('blocked_regimes', [])) or '-'}"
                )
            if governance_summary.get("action_reason"):
                if governance_summary.get("governance_mode") == "blocked":
                    st.warning(f"Governanca: {governance_summary.get('action_reason')}")
                elif governance_summary.get("governance_mode") == "reduced":
                    st.info(f"Governanca reduzida: {governance_summary.get('action_reason')}")
        if context_evaluation:
            st.caption(
                f"Contexto: {context_evaluation.get('market_bias', 'neutral')} | "
                f"{context_evaluation.get('regime', '-')} | "
                f"Forca {context_evaluation.get('context_strength', 0):.2f}/10"
            )
        if regime_evaluation:
            st.caption(
                f"Regime atual: {regime_evaluation.get('regime', 'range')} | "
                f"{regime_evaluation.get('volatility_state', 'normal_volatility')} | "
                f"Forca {regime_evaluation.get('regime_score', 0):.2f}/10 | "
                f"ADX {regime_evaluation.get('adx', 0):.2f} | "
                f"ATR% {regime_evaluation.get('atr_pct', 0):.2f} | "
                f"Trend {regime_evaluation.get('trend_state', 'range')} | "
                f"Parabolico {regime_evaluation.get('parabolic', False)}"
            )
        if structure_evaluation:
            st.caption(
                f"Estrutura: {structure_evaluation.get('structure_state', 'weak_structure')} | "
                f"{structure_evaluation.get('price_location', 'mid_range')} | "
                f"Qualidade {structure_evaluation.get('structure_quality', 0):.2f}/10"
            )
        if confirmation_evaluation:
            conflicts_preview = ", ".join(confirmation_evaluation.get("conflicts", [])[:2]) or "sem conflitos relevantes"
            st.caption(
                f"Confirmacao: {confirmation_evaluation.get('confirmation_state', 'weak')} | "
                f"Score {confirmation_evaluation.get('confirmation_score', 0):.2f}/10 | "
                f"Conflitos: {conflicts_preview}"
            )
        if entry_quality_evaluation:
            st.caption(
                f"Entrada: {entry_quality_evaluation.get('entry_quality', 'bad')} | "
                f"Leitura {entry_quality_evaluation.get('market_pattern') or entry_quality_evaluation.get('setup_type') or '-'} | "
                f"Score {float(entry_quality_evaluation.get('entry_score', 0) or 0):.2f}/10 | "
                f"RSI {entry_quality_evaluation.get('rsi_state', 'neutral')} | "
                f"Candle {entry_quality_evaluation.get('candle_quality', 'bad')} | "
                f"Momentum {entry_quality_evaluation.get('momentum_state', 'weak')} | "
                f"RR {entry_quality_evaluation.get('rr_estimate', 0):.2f} | "
                f"Rejeicao {entry_quality_evaluation.get('rejection_reason') or '-'}"
            )
        if scenario_evaluation:
            st.caption(
                f"Cenario: {scenario_evaluation.get('scenario_score', 0):.2f}/10 | "
                f"Grade {scenario_evaluation.get('scenario_grade', 'D')}"
            )
        if trade_decision:
            st.caption(
                f"Decisao analitica: {trade_decision.get('action', 'wait')} | "
                f"Confianca {trade_decision.get('confidence', 0):.2f}/10 | "
                f"Motivo: {trade_decision.get('entry_reason') or trade_decision.get('block_reason') or '-'}"
            )
        if hard_block_evaluation and hard_block_evaluation.get("hard_block"):
            st.error(
                f"Hard block analitico: {hard_block_evaluation.get('block_reason')} "
                f"({hard_block_evaluation.get('block_source', 'signal_engine')})"
            )

    with analysis_col2:
        if approved_signal == "COMPRA":
            st.success(f"""
            🟢 **SINAL ANALITICO APROVADO - COMPRA FORTE**  
            RSI cruzou acima de {rsi_min} com tendencia alinhada nas EMAs.  
            Considere entrada em posição de compra.
            """)
        elif approved_signal == "VENDA":
            st.error(f"""
            🔴 **SINAL ANALITICO APROVADO - VENDA FORTE**  
            RSI cruzou abaixo de {rsi_max} com tendencia alinhada nas EMAs.  
            Considere saída da posição ou entrada em venda.
            """)
        elif blocked_signal in ACTIONABLE_SIGNALS:
            st.warning(f"""
            ⚠️ **SINAL BLOQUEADO**  
            Candidato detectado: {blocked_signal}.  
            Motivo: {analytical_block_reason or '-'}.
            """)
        else:
            st.warning("""
            ⚪ **SINAL NEUTRO**  
            Indicadores em zona neutra.  
            Aguardar melhor oportunidade.
            """)


def render_market_signal_history(symbol: str, timeframe: str, require_volume: bool, require_trend: bool):
    st.subheader("📋 Histórico de Sinais")

    history_col1, history_col2 = st.columns(2)
    with history_col1:
        show_source = st.radio(
            "Fonte dos dados:",
            ["Sessão Atual", "Banco de Dados (Persistente)"],
            help="Escolha se quer ver apenas sinais da sessão atual ou todo o histórico salvo",
            key="market_signal_history_source",
        )

    with history_col2:
        if show_source == "Banco de Dados (Persistente)":
            limit_signals = st.number_input(
                "Quantidade de sinais:",
                min_value=10,
                max_value=1000,
                value=100,
                key="market_signal_history_limit",
            )
        else:
            limit_signals = len(st.session_state.signals_history) if st.session_state.signals_history else 0

    if show_source == "Banco de Dados (Persistente)":
        try:
            db_signals = db.get_recent_signals(limit=limit_signals)
            if db_signals:
                signals_df = pd.DataFrame(db_signals)
                if 'created_at_br' in signals_df.columns:
                    signals_df['timestamp'] = pd.to_datetime(
                        signals_df['created_at_br'],
                        format='%d/%m/%Y %H:%M:%S',
                        errors='coerce',
                    )
                signals_df = signals_df.sort_values('timestamp', ascending=False)
                signals_df = signals_df.rename(
                    columns={
                        'signal_type': 'signal',
                        'created_at_br': 'timestamp',
                    }
                )
            else:
                signals_df = None
                st.info("📋 Nenhum sinal encontrado no banco de dados.")
        except Exception as e:
            st.error(f"❌ Erro ao carregar dados do banco: {str(e)}")
            signals_df = None
    else:
        if st.session_state.signals_history:
            signals_df = pd.DataFrame(st.session_state.signals_history)
            signals_df['timestamp'] = pd.to_datetime(signals_df['timestamp'])
            signals_df = signals_df.sort_values('timestamp', ascending=False)
        else:
            signals_df = None

    if signals_df is None or len(signals_df) == 0:
        return

    try:
        display_df = signals_df.copy()
        display_df = display_df.loc[:, ~display_df.columns.duplicated()]
        required_cols = ['timestamp', 'symbol', 'price', 'rsi', 'signal']
        missing_cols = [col for col in required_cols if col not in display_df.columns]

        if missing_cols:
            st.error(f"Colunas ausentes nos dados: {missing_cols}")
            return

        if not pd.api.types.is_datetime64_any_dtype(display_df['timestamp']):
            display_df['timestamp'] = pd.to_datetime(display_df['timestamp'], errors='coerce')

        display_df = display_df.dropna(subset=['timestamp'])
        if len(display_df) == 0:
            st.warning("Não foi possível exibir os dados do histórico devido a problemas na formatação.")
            return

        display_df['timestamp'] = display_df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
        display_df['price'] = display_df['price'].apply(lambda x: f"${x:.6f}" if pd.notna(x) else "N/A")
        display_df['rsi'] = display_df['rsi'].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "N/A")

        if 'macd' in display_df.columns:
            display_df['macd'] = display_df['macd'].apply(lambda x: f"{x:.4f}" if pd.notna(x) else "N/A")
        if 'macd_signal' in display_df.columns:
            display_df['macd_signal'] = display_df['macd_signal'].apply(lambda x: f"{x:.4f}" if pd.notna(x) else "N/A")

        display_df = display_df.rename(
            columns={
                'timestamp': 'Data/Hora',
                'symbol': 'Par',
                'timeframe': 'Timeframe',
                'price': 'Preço',
                'rsi': 'RSI',
                'macd': 'MACD',
                'macd_signal': 'MACD Signal',
                'signal': 'Sinal',
                'signal_type': 'Sinal',
                'candidate_signal': 'Candidato',
                'approved_signal': 'Aprovado',
                'blocked_signal': 'Bloqueado',
                'block_reason': 'Motivo Bloqueio',
                'operational_signal': 'Sinal Operacional',
                'operational_block_reason': 'Motivo Operacional',
            }
        )

        visible_columns = [
            col for col in [
                'Data/Hora', 'Par', 'Timeframe', 'Preço', 'RSI', 'MACD', 'MACD Signal',
                'Candidato', 'Aprovado', 'Bloqueado', 'Motivo Bloqueio',
                'Sinal Operacional', 'Motivo Operacional', 'Sinal'
            ]
            if col in display_df.columns
        ]
        display_df = display_df[visible_columns]
    except Exception as e:
        st.error(f"Erro ao processar dados do histórico: {str(e)}")
        return

    st.dataframe(display_df, width='stretch', hide_index=True)

    history_actions_col1, history_actions_col2 = st.columns(2)
    with history_actions_col1:
        if show_source == "Sessão Atual" and st.button("🗑️ Limpar Histórico", key="clear_market_signal_history"):
            st.session_state.signals_history = []
            st.rerun()

    with history_actions_col2:
        if show_source == "Banco de Dados (Persistente)":
            try:
                stats = db.get_statistics()
                runtime_strategy_version = get_effective_strategy_settings(
                    symbol,
                    timeframe,
                    require_volume=require_volume,
                    require_trend=require_trend,
                )["strategy_version"]
                paper_summary = get_paper_trade_service().get_summary(symbol=symbol, timeframe=timeframe)
                edge_summary = get_cached_edge_monitor_summary(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_version=runtime_strategy_version,
                )
                st.caption(
                    f"Paper trades {symbol} {timeframe}: "
                    f"{paper_summary.get('closed_trades', 0)} fechados | "
                    f"Win rate {paper_summary.get('win_rate', 0):.1f}% | "
                    f"Resultado acumulado {paper_summary.get('total_result_pct', 0):.2f}%"
                )
                edge_status = edge_summary.get('status')
                edge_message = (
                    f"Edge monitor {symbol} {timeframe}: {edge_summary.get('status_message')} "
                    f"| Baseline PF {edge_summary.get('baseline_profit_factor', 0):.2f} "
                    f"vs Paper PF {edge_summary.get('paper_profit_factor', 0):.2f}"
                )
                if edge_status == "aligned":
                    st.success(edge_message)
                elif edge_status in {"degraded", "watchlist"}:
                    st.warning(edge_message)
                else:
                    st.info(edge_message)
                st.info(f"📊 Estatísticas: {stats['total_signals']} sinais total | {stats['signals_24h']} últimas 24h")
            except Exception as e:
                st.warning(f"⚠️ Erro ao carregar estatísticas: {str(e)}")


def _workspace_runtime_symbol_options(execution_context):
    options = [
        str(item).strip()
        for item in (execution_context.get("allowed_symbols") or [])
        if str(item).strip()
    ]
    if options:
        return list(dict.fromkeys(options))
    fallback_symbol = str(os.getenv("SYMBOL", AppConfig.DEFAULT_SYMBOL)).strip() or AppConfig.DEFAULT_SYMBOL
    return [fallback_symbol]


def _workspace_runtime_timeframe_options(execution_context):
    options = [
        str(item).strip()
        for item in (execution_context.get("allowed_timeframes") or [])
        if str(item).strip()
    ]
    if options:
        return list(dict.fromkeys(options))
    fallback_timeframe = str(os.getenv("TIMEFRAME", AppConfig.DEFAULT_TIMEFRAME)).strip() or AppConfig.DEFAULT_TIMEFRAME
    return [fallback_timeframe]


def _build_workspace_remote_runtime_health(control_row, runtime_db_state):
    desired_state = str((control_row or {}).get("desired_state") or "stopped").strip().lower()
    requested_mode = str((control_row or {}).get("requested_mode") or "real").strip().lower()
    heartbeat_age_seconds = _runtime_heartbeat_age_seconds(runtime_db_state)
    heartbeat_fresh = _runtime_heartbeat_is_recent(runtime_db_state)
    last_command_at = _parse_runtime_datetime((control_row or {}).get("last_command_at"))
    recent_command = False
    if last_command_at is not None:
        try:
            recent_command = (datetime.now(last_command_at.tzinfo) - last_command_at).total_seconds() <= 90
        except Exception:
            recent_command = False

    if heartbeat_fresh:
        status_label = "ON"
        tone = "accent"
    elif desired_state == "running" and recent_command:
        status_label = "PARTINDO"
        tone = "warm"
    elif desired_state == "running":
        status_label = "SEM HEARTBEAT"
        tone = "danger"
    else:
        status_label = "OFF"
        tone = "default"

    return {
        "desired_state": desired_state,
        "requested_mode": requested_mode,
        "heartbeat_fresh": heartbeat_fresh,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "status_label": status_label,
        "tone": tone,
    }


def render_workspace_account_runtime_panel(
    *,
    user_id: int,
    workspace_user: dict,
    workspace_subscription: dict,
    selected_account: dict,
    execution_context: dict,
):
    selected_account_id = str(selected_account["account_id"])
    selected_account_alias = str(
        execution_context.get("account_alias")
        or selected_account.get("account_alias")
        or selected_account_id
    )
    selected_exchange = str(
        execution_context.get("exchange_name")
        or selected_account.get("exchange")
        or "binanceusdm"
    )

    symbol_options = _workspace_runtime_symbol_options(execution_context)
    timeframe_options = _workspace_runtime_timeframe_options(execution_context)
    default_symbol = str(os.getenv("SYMBOL", AppConfig.DEFAULT_SYMBOL)).strip() or AppConfig.DEFAULT_SYMBOL
    default_timeframe = str(os.getenv("TIMEFRAME", AppConfig.DEFAULT_TIMEFRAME)).strip() or AppConfig.DEFAULT_TIMEFRAME
    symbol_index = symbol_options.index(default_symbol) if default_symbol in symbol_options else 0
    timeframe_index = timeframe_options.index(default_timeframe) if default_timeframe in timeframe_options else 0

    st.markdown("### Runtime Remoto")
    st.caption(
        "Este painel não sobe processo local na dashboard. "
        "Ele grava o comando no Postgres para o serviço privado `evo-bot` ligar ou parar o runtime da sua conta."
    )

    selector_col1, selector_col2, selector_col3 = st.columns([1.2, 1.0, 1.0])
    with selector_col1:
        runtime_symbol = st.selectbox(
            "Símbolo do Runtime",
            options=symbol_options,
            index=symbol_index,
            key=f"workspace_runtime_symbol_{selected_account_id}",
        )
    with selector_col2:
        runtime_timeframe = st.selectbox(
            "Timeframe do Runtime",
            options=timeframe_options,
            index=timeframe_index,
            key=f"workspace_runtime_timeframe_{selected_account_id}",
        )

    runtime_control = db.get_user_runtime_control(
        user_id=user_id,
        account_id=selected_account_id,
        exchange=selected_exchange,
        symbol=runtime_symbol,
        timeframe=runtime_timeframe,
    )
    requested_mode = str((runtime_control or {}).get("requested_mode") or "real").strip().lower()
    with selector_col3:
        st.metric(
            "Ambiente",
            "Conta Real",
        )
        selected_mode = "real"
        if requested_mode != "real":
            st.caption("Comandos antigos em testnet serao normalizados para conta real no proximo start.")

    runtime_key = build_account_runtime_key(
        user_id=user_id,
        account_id=selected_account_id,
        exchange=selected_exchange,
        symbol=runtime_symbol,
        timeframe=runtime_timeframe,
    )
    runtime_rows = db.get_bot_runtime_state(runtime_key=runtime_key, limit=1)
    runtime_db_state = runtime_rows[0] if runtime_rows else None
    runtime_health = _build_workspace_remote_runtime_health(runtime_control, runtime_db_state)
    desired_state = str(runtime_health.get("desired_state") or "stopped").strip().lower()
    desired_state_label = "RUNNING" if desired_state == "running" else "STOPPED"
    runtime_online = bool(runtime_health.get("heartbeat_fresh"))
    latest_position = str((runtime_db_state or {}).get("position_side") or "flat")
    latest_signal = str((runtime_db_state or {}).get("last_signal") or "-")
    db_status = str((runtime_db_state or {}).get("status") or "-")

    render_dashboard_strip(
        "O botão aciona um comando persistido. O daemon multiusuário do `evo-bot` reconciliará esse estado em poucos segundos.",
        badges=[
            _build_status_pill("Runtime", runtime_health.get("status_label", "OFF"), runtime_health.get("tone", "default")),
            _build_status_pill("Desejado", desired_state_label, "accent" if desired_state == "running" else "default"),
            _build_status_pill("Modo", "Conta Real", "danger"),
            _build_status_pill("Mercado", f"{runtime_symbol} · {runtime_timeframe}", "default"),
        ],
    )

    runtime_col1, runtime_col2, runtime_col3, runtime_col4, runtime_col5 = st.columns(5)
    with runtime_col1:
        st.metric("Status Remoto", runtime_health.get("status_label", "OFF"))
    with runtime_col2:
        st.metric("Desejado", desired_state_label)
    with runtime_col3:
        st.metric("Heartbeat", _format_age_label(runtime_health.get("heartbeat_age_seconds")))
    with runtime_col4:
        st.metric("DB Status", db_status)
    with runtime_col5:
        st.metric("Posição", latest_position)

    runtime_meta_col1, runtime_meta_col2, runtime_meta_col3, runtime_meta_col4 = st.columns(4)
    with runtime_meta_col1:
        st.metric("Último Sinal", latest_signal)
    with runtime_meta_col2:
        st.metric("Revisão", int((runtime_control or {}).get("command_revision", 0) or 0))
    with runtime_meta_col3:
        st.metric("Live da Conta", "ON" if bool(selected_account.get("live_enabled")) else "OFF")
    with runtime_meta_col4:
        st.metric("Credencial", "OK" if execution_context.get("api_key_ref") else "PENDENTE")

    st.caption(
        f"Runtime key: `{runtime_key}` | "
        f"Último comando: {(runtime_control or {}).get('last_command_at') or '-'} | "
        f"Último start: {(runtime_control or {}).get('last_started_at') or '-'} | "
        f"Último stop: {(runtime_control or {}).get('last_stopped_at') or '-'}"
    )

    risk_profile = execution_context.get("risk_profile") or {}
    license_payload = (workspace_user or {}).get("license") or {}
    subscription_gate_required = bool(ProductionConfig.REQUIRE_ACTIVE_SUBSCRIPTION_FOR_BOT)
    subscription_gate_satisfied = bool(
        (not subscription_gate_required) or bool(workspace_subscription.get("is_active"))
    )
    license_gate_required = bool(getattr(ProductionConfig, "REQUIRE_DASHBOARD_DEVICE_LICENSE", True))
    license_gate_satisfied = bool((not license_gate_required) or license_payload.get("allowed"))

    start_block_reasons = []
    if subscription_gate_required and not subscription_gate_satisfied:
        start_block_reasons.append("Assinatura inativa ou expirada para uso do runtime.")
    if license_gate_required and not license_gate_satisfied:
        start_block_reasons.append(
            f"Licença de dispositivo/IP bloqueada: {license_payload.get('reason') or 'não autorizada'}."
        )
    if str(selected_account.get("status") or "disabled").strip().lower() != "active":
        start_block_reasons.append("Conta está desabilitada.")
    if not bool(selected_account.get("live_enabled")):
        start_block_reasons.append("Ative `Live Enabled` na conta para usar o runtime remoto.")
    if not bool(execution_context.get("api_key_ref")):
        start_block_reasons.append("Cadastre uma credencial criptografada na aba Credenciais.")
    if not bool(risk_profile.get("is_valid", False)):
        start_block_reasons.append("Perfil de risco inválido ou ausente.")
    if not bool(risk_profile.get("live_enabled", False)):
        start_block_reasons.append("Perfil de risco ainda não liberou operação live/runtime.")
    if bool(execution_context.get("governance_blocked")):
        start_block_reasons.append(
            execution_context.get("governance_block_reason") or "Governança bloqueou a conta."
        )

    runtime_control_error = str((runtime_control or {}).get("last_error") or "").strip()
    runtime_db_error = str((runtime_db_state or {}).get("last_error") or "").strip()
    if runtime_db_state and runtime_db_state.get("blocked"):
        st.warning(
            f"Runtime bloqueado no DB: {runtime_db_state.get('block_reason') or 'sem motivo informado'}"
        )
    if runtime_control_error:
        st.error(f"Último erro do reconciliador: {runtime_control_error}")
    if runtime_db_error:
        st.error(f"Último erro persistido pelo bot: {runtime_db_error}")
    if requested_mode != "real" or str((runtime_db_state or {}).get("environment") or "").strip().lower() == "testnet":
        st.warning(
            "Este workspace agora opera somente em conta real. "
            "Se houver runtime antigo em testnet, pare-o e ligue novamente para normalizar."
        )
    if desired_state == "running" and not runtime_online:
        st.info(
            "Existe um comando de start pendente ou degradado. "
            "O `evo-bot` privado deve reconciliar isso no próximo ciclo."
        )

    st.info(
        "O fluxo do cliente neste workspace usa apenas credenciais reais. "
        "Cada conta opera com um runtime isolado e um unico par de chaves por `conta + exchange`."
    )

    runtime_action_col1, runtime_action_col2, runtime_action_col3 = st.columns([1.1, 1.1, 0.8])
    start_label = "🔁 Reenviar Start" if desired_state == "running" and not runtime_online else "▶️ Ligar Runtime"
    stop_disabled = desired_state != "running" and not runtime_online
    with runtime_action_col1:
        if st.button(
            start_label,
            key=f"workspace_runtime_start_{selected_account_id}_{runtime_symbol}_{runtime_timeframe}",
            disabled=bool(runtime_online) or bool(start_block_reasons),
        ):
            control_row = db.set_user_runtime_control(
                user_id=user_id,
                account_id=selected_account_id,
                exchange=selected_exchange,
                symbol=runtime_symbol,
                timeframe=runtime_timeframe,
                desired_state="running",
                requested_mode=selected_mode,
                requested_by_user_id=user_id,
                requested_by_scope="workspace",
                requested_reason=f"workspace_runtime_start:{selected_account_alias}",
            )
            db.save_user_execution_event(
                {
                    "user_id": user_id,
                    "account_id": selected_account_id,
                    "exchange": selected_exchange,
                    "symbol": runtime_symbol,
                    "timeframe": runtime_timeframe,
                    "strategy_version": None,
                    "event_type": "runtime_control_request",
                    "event_status": "queued",
                    "message": (
                        f"Start remoto solicitado para {selected_account_alias} em conta real."
                    ),
                    "details_json": {
                        "runtime_key": runtime_key,
                        "desired_state": "running",
                        "requested_mode": selected_mode,
                        "command_revision": control_row.get("command_revision"),
                        "requested_by_scope": "workspace",
                    },
                }
            )
            st.success(
                "Comando enviado para o `evo-bot`. "
                "Aguarde alguns segundos para o primeiro heartbeat aparecer."
            )
            st.rerun()
    with runtime_action_col2:
        if st.button(
            "⏹️ Parar Runtime",
            key=f"workspace_runtime_stop_{selected_account_id}_{runtime_symbol}_{runtime_timeframe}",
            disabled=bool(stop_disabled),
        ):
            control_row = db.set_user_runtime_control(
                user_id=user_id,
                account_id=selected_account_id,
                exchange=selected_exchange,
                symbol=runtime_symbol,
                timeframe=runtime_timeframe,
                desired_state="stopped",
                requested_mode=selected_mode,
                requested_by_user_id=user_id,
                requested_by_scope="workspace",
                requested_reason=f"workspace_runtime_stop:{selected_account_alias}",
            )
            db.save_user_execution_event(
                {
                    "user_id": user_id,
                    "account_id": selected_account_id,
                    "exchange": selected_exchange,
                    "symbol": runtime_symbol,
                    "timeframe": runtime_timeframe,
                    "strategy_version": None,
                    "event_type": "runtime_control_request",
                    "event_status": "queued",
                    "message": f"Stop remoto solicitado para {selected_account_alias}.",
                    "details_json": {
                        "runtime_key": runtime_key,
                        "desired_state": "stopped",
                        "requested_mode": selected_mode,
                        "command_revision": control_row.get("command_revision"),
                        "requested_by_scope": "workspace",
                    },
                }
            )
            st.warning("Comando de parada enviado para o `evo-bot`.")
            st.rerun()
    with runtime_action_col3:
        if st.button(
            "🔄 Atualizar",
            key=f"workspace_runtime_refresh_{selected_account_id}_{runtime_symbol}_{runtime_timeframe}",
        ):
            st.rerun()

    if start_block_reasons:
        for reason in start_block_reasons:
            st.warning(reason)

    with st.expander("Diagnóstico bruto do runtime remoto", expanded=False):
        st.json(
            {
                "control_row": runtime_control or {},
                "runtime_db_state": runtime_db_state or {},
                "runtime_health": runtime_health,
            }
        )


def render_multiuser_workspace_tab():
    workspace_user = get_authenticated_dashboard_user()
    admin_session_active = is_admin_dashboard_session_active()
    st.subheader("👤 Meu Workspace")
    st.caption("Área isolada por usuário para contas, risco, credenciais e monitoramento operacional.")

    if not workspace_user and not admin_session_active:
        st.info("Faça login na barra lateral para acessar seu workspace multiusuário.")
        st.markdown(
            """
            Este espaço foi preparado para o modelo multiusuário:
            - cada usuário enxerga apenas as próprias contas
            - credenciais ficam protegidas no vault
            - risco, permissões e governança são acompanhados por conta

            Como entrar pela primeira vez:
            - o administrador cria seu acesso para você
            - você recebe `login` e `senha inicial`
            - faz login na barra lateral em `Workspace Multiusuário`
            """
        )
        return

    if not workspace_user and admin_session_active:
        st.success("Sessão Admin ativa. Todas as ações da dashboard estão liberadas para uso operacional.")
        st.info(
            "O login do Workspace continua disponível apenas se você quiser simular a experiência do usuário final. "
            "Como admin, você já pode operar e configurar o sistema sem esse login."
        )
        summary = {}
        overview_rows: List[Dict[str, Any]] = []
        try:
            summary = db.get_multiuser_dashboard_summary()
            overview_rows = db.get_multiuser_account_overview(limit=20)
        except Exception as admin_workspace_exc:
            st.warning(f"Não foi possível carregar o resumo multiusuário: {admin_workspace_exc}")

        admin_col1, admin_col2, admin_col3, admin_col4, admin_col5 = st.columns(5)
        with admin_col1:
            st.metric("Contas Ativas", int(summary.get("active_accounts", 0) or 0))
        with admin_col2:
            st.metric("Paper Only", int(summary.get("paper_accounts", 0) or 0))
        with admin_col3:
            st.metric("Bloqueadas", int(summary.get("blocked_accounts", 0) or 0))
        with admin_col4:
            st.metric("Erros 24h", int(summary.get("operational_error_accounts", 0) or 0))
        with admin_col5:
            st.metric("Mismatch", int(summary.get("mismatch_accounts", 0) or 0))

        shortcut_col1, shortcut_col2, shortcut_col3 = st.columns(3)
        with shortcut_col1:
            if st.button("Abrir Admin", key="workspace_admin_shortcut"):
                st.session_state.default_tab = "admin"
                st.session_state.dashboard_main_section = "👑 Admin"
                st.rerun()
        with shortcut_col2:
            if st.button("Abrir Bot Trader", key="workspace_bot_shortcut"):
                st.session_state.default_tab = "bot"
                st.session_state.dashboard_main_section = "🤖 Bot Trader"
                st.rerun()
        with shortcut_col3:
            if st.button("Abrir Mercado", key="workspace_market_shortcut"):
                st.session_state.default_tab = "market"
                st.session_state.dashboard_main_section = "📈 Mercado"
                st.rerun()

        if overview_rows:
            st.markdown("### Visão Operacional das Contas")
            overview_df = pd.DataFrame(overview_rows)
            preferred_columns = [
                "user_id",
                "account_id",
                "account_alias",
                "exchange",
                "status",
                "live_enabled",
                "paper_enabled",
                "risk_mode",
                "permission_status",
                "token_status",
                "reconciliation_status",
                "open_positions",
                "pending_orders",
            ]
            display_columns = [column for column in preferred_columns if column in overview_df.columns]
            st.dataframe(overview_df[display_columns], use_container_width=True, hide_index=True)
        else:
            st.caption("Nenhuma conta operacional encontrada ainda. Você pode criá-las em `Admin > Contas`.")

        with st.expander("Por que o login do Workspace continua aparecendo?", expanded=False):
            st.markdown(
                """
                - `Admin` autenticado tem bypass global para operação e configuração
                - `Workspace` continua existindo para representar a jornada do usuário final
                - você só precisa usar o login do Workspace se quiser validar permissões e experiência do cliente
                """
            )
        return

    user_id = int(workspace_user["user_id"])
    user_label = (
        workspace_user.get("first_name")
        or workspace_user.get("username")
        or workspace_user.get("login_name")
        or str(user_id)
    )
    st.success(f"Sessão ativa: {user_label} | User ID {user_id}")
    workspace_subscription = workspace_user.get("subscription") or {}
    subscription_plan = str(workspace_subscription.get("plan_code") or "free").upper()
    subscription_status = str(workspace_subscription.get("status") or "inactive").lower()
    if workspace_subscription.get("is_active"):
        st.success(
            f"Assinatura ativa: {subscription_plan} | "
            f"expira em {workspace_subscription.get('expires_at')}"
        )
        if workspace_subscription.get("expiring_soon"):
            st.warning(
                f"Renovação recomendada: faltam {int(workspace_subscription.get('days_remaining', 0))} dia(s) para expirar."
            )
    else:
        st.warning(
            f"Assinatura {subscription_plan} está {subscription_status}. "
            "Ative um plano para operar o bot em runtime."
        )
    if workspace_user.get("require_password_change"):
        st.warning("Sua conta exige troca de senha antes de uso recorrente. Atualize abaixo.")

    workspace_accounts = db.get_user_workspace_accounts(user_id=user_id, limit=100)

    summary_col1, summary_col2, summary_col3, summary_col4 = st.columns(4)
    with summary_col1:
        st.metric("Contas", len(workspace_accounts))
    with summary_col2:
        st.metric("Live Habilitado", sum(1 for item in workspace_accounts if bool(item.get("live_enabled"))))
    with summary_col3:
        st.metric("Paper Habilitado", sum(1 for item in workspace_accounts if bool(item.get("paper_enabled"))))
    with summary_col4:
        st.metric("Risk Profiles Válidos", sum(1 for item in workspace_accounts if bool(item.get("risk_profile_valid"))))

    if workspace_accounts:
        account_lookup = {
            f"{row.get('account_alias') or row.get('account_id')} | {row.get('exchange')} | {row.get('account_id')}": row
            for row in workspace_accounts
        }
        selected_account_label = st.selectbox(
            "Selecionar Conta",
            options=list(account_lookup.keys()),
            key="workspace_account_selector",
        )
        selected_account = account_lookup[selected_account_label]
        selected_account_id = str(selected_account["account_id"])
        selected_exchange = str(selected_account.get("exchange") or "")
        try:
            execution_context = db.build_account_execution_context(
                user_id=user_id,
                account_id=selected_account_id,
                exchange=selected_exchange,
            )
        except Exception:
            risk_profile_fallback = db.get_user_risk_profile(user_id=user_id, account_id=selected_account_id) or {}
            credential_fallback = db.get_user_exchange_credential(
                user_id=user_id,
                account_id=selected_account_id,
                exchange=selected_exchange,
                include_encrypted=False,
            ) or {}
            governance_fallback = db.get_user_governance_state(
                user_id=user_id,
                account_id=selected_account_id,
                exchange=selected_exchange,
            ) or {}
            execution_context = {
                "user_id": user_id,
                "account_id": selected_account_id,
                "account_alias": selected_account.get("account_alias") or selected_account_id,
                "exchange_name": selected_exchange,
                "api_key_ref": credential_fallback.get("api_key_ref"),
                "token_ref": credential_fallback.get("token_ref"),
                "live_enabled": bool(selected_account.get("live_enabled")),
                "paper_enabled": bool(selected_account.get("paper_enabled")),
                "governance_status": governance_fallback.get("governance_status") or "unknown",
                "governance_mode": governance_fallback.get("governance_mode") or "blocked",
                "governance_blocked": bool(governance_fallback.get("blocked", False)),
                "governance_block_reason": governance_fallback.get("block_reason"),
                "risk_profile": risk_profile_fallback,
                "allowed_symbols": selected_account.get("allowed_symbols") or [],
                "allowed_timeframes": selected_account.get("allowed_timeframes") or [],
                "capital_base": float(selected_account.get("capital_base", 0.0) or 0.0),
                "risk_mode": selected_account.get("risk_mode") or "normal",
                "notes": selected_account.get("notes"),
                "permission_status": credential_fallback.get("permission_status") or selected_account.get("permission_status") or "unknown",
                "token_status": credential_fallback.get("token_status") or selected_account.get("token_status") or "unknown",
                "reconciliation_status": credential_fallback.get("reconciliation_status") or selected_account.get("reconciliation_status") or "unknown",
            }

        st.markdown("### Estado da Conta")
        account_col1, account_col2, account_col3, account_col4, account_col5 = st.columns(5)
        with account_col1:
            st.metric("Status", selected_account.get("status", "-"))
        with account_col2:
            st.metric("Live", "ON" if bool(selected_account.get("live_enabled")) else "OFF")
        with account_col3:
            st.metric("Paper", "ON" if bool(selected_account.get("paper_enabled")) else "OFF")
        with account_col4:
            st.metric("Governança", execution_context.get("governance_status", "-"))
        with account_col5:
            st.metric("Modo de Risco", execution_context.get("risk_mode", selected_account.get("risk_mode", "-")))

        ops_col1, ops_col2, ops_col3, ops_col4 = st.columns(4)
        with ops_col1:
            st.metric("Capital Base", f"${float(selected_account.get('capital_base', 0.0) or 0.0):,.2f}")
        with ops_col2:
            st.metric("Posições Abertas", int(selected_account.get("open_positions", 0) or 0))
        with ops_col3:
            st.metric("Ordens Pendentes", int(selected_account.get("pending_orders", 0) or 0))
        with ops_col4:
            st.metric("Credencial", "OK" if execution_context.get("api_key_ref") else "PENDENTE")

        st.caption(
            f"Símbolos permitidos: {', '.join(execution_context.get('allowed_symbols', [])) or '-'} | "
            f"Timeframes permitidos: {', '.join(execution_context.get('allowed_timeframes', [])) or '-'} | "
            f"Permissões: {execution_context.get('permission_status', 'unknown')} | "
            f"Token: {execution_context.get('token_status', 'unknown')} | "
            f"Reconciliação: {execution_context.get('reconciliation_status', 'unknown')}"
        )
        if execution_context.get("governance_block_reason"):
            st.warning(f"Bloqueio operacional: {execution_context.get('governance_block_reason')}")

        detail_tab1, detail_tab2, detail_tab3, detail_tab4, detail_tab5 = st.tabs(
            ["⚙️ Conta", "🛡️ Risco", "🔑 Credenciais", "▶️ Runtime", "📜 Eventos"]
        )

        with detail_tab1:
            with st.form(f"workspace_account_form_{selected_account_id}"):
                acc_col1, acc_col2, acc_col3 = st.columns(3)
                with acc_col1:
                    account_alias = st.text_input(
                        "Alias",
                        value=str(selected_account.get("account_alias") or selected_account_id),
                        key=f"workspace_alias_{selected_account_id}",
                    )
                    account_status = st.selectbox(
                        "Status",
                        options=["active", "disabled"],
                        index=0 if str(selected_account.get("status") or "active").lower() == "active" else 1,
                        key=f"workspace_status_{selected_account_id}",
                    )
                with acc_col2:
                    live_enabled = st.checkbox(
                        "Live Enabled",
                        value=bool(selected_account.get("live_enabled")),
                        key=f"workspace_live_{selected_account_id}",
                    )
                    paper_enabled = st.checkbox(
                        "Paper Enabled",
                        value=bool(selected_account.get("paper_enabled")),
                        key=f"workspace_paper_{selected_account_id}",
                    )
                with acc_col3:
                    capital_base = st.number_input(
                        "Capital Base",
                        min_value=0.0,
                        value=float(selected_account.get("capital_base", 0.0) or 0.0),
                        step=100.0,
                        key=f"workspace_capital_{selected_account_id}",
                    )
                    risk_mode = st.selectbox(
                        "Risk Mode",
                        options=["normal", "reduced", "blocked"],
                        index=["normal", "reduced", "blocked"].index(
                            str(selected_account.get("risk_mode") or "normal")
                            if str(selected_account.get("risk_mode") or "normal") in {"normal", "reduced", "blocked"}
                            else "normal"
                        ),
                        key=f"workspace_risk_mode_{selected_account_id}",
                    )

                allowed_symbols_raw = st.text_input(
                    "Símbolos Permitidos",
                    value=",".join(execution_context.get("allowed_symbols", [])),
                    key=f"workspace_symbols_{selected_account_id}",
                )
                allowed_timeframes = st.multiselect(
                    "Timeframes Permitidos",
                    options=["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                    default=execution_context.get("allowed_timeframes", []),
                    key=f"workspace_timeframes_{selected_account_id}",
                )
                account_notes = st.text_area(
                    "Notas da Conta",
                    value=str(selected_account.get("notes") or ""),
                    key=f"workspace_notes_{selected_account_id}",
                )

                if st.form_submit_button("Salvar Conta"):
                    db.upsert_user_account(
                        {
                            "user_id": user_id,
                            "account_id": selected_account_id,
                            "account_alias": account_alias.strip() or selected_account_id,
                            "exchange": selected_exchange,
                            "status": account_status,
                            "live_enabled": bool(live_enabled),
                            "paper_enabled": bool(paper_enabled),
                            "capital_base": float(capital_base),
                            "risk_mode": risk_mode,
                            "allowed_symbols": [item.strip() for item in allowed_symbols_raw.split(",") if item.strip()],
                            "allowed_timeframes": list(allowed_timeframes),
                            "notes": account_notes,
                        }
                    )
                    st.success("Conta atualizada com sucesso.")
                    st.rerun()

            with st.expander("Adicionar Nova Conta", expanded=False):
                with st.form(f"workspace_new_account_form_{user_id}"):
                    new_col1, new_col2, new_col3 = st.columns(3)
                    with new_col1:
                        new_account_id = st.text_input("Novo Account ID", key=f"workspace_new_account_id_{user_id}")
                        new_account_alias = st.text_input("Alias da Nova Conta", key=f"workspace_new_account_alias_{user_id}")
                    with new_col2:
                        new_exchange = st.selectbox(
                            "Exchange",
                            options=AppConfig.BRAZIL_SUPPORTED_EXCHANGES or ["binance"],
                            key=f"workspace_new_exchange_{user_id}",
                        )
                        new_status = st.selectbox(
                            "Status da Conta",
                            options=["active", "disabled"],
                            key=f"workspace_new_status_{user_id}",
                        )
                    with new_col3:
                        new_capital_base = st.number_input(
                            "Capital Base Inicial",
                            min_value=0.0,
                            value=10000.0,
                            step=100.0,
                            key=f"workspace_new_capital_{user_id}",
                        )
                        new_live_enabled = st.checkbox("Live Enabled", value=False, key=f"workspace_new_live_{user_id}")
                        new_paper_enabled = st.checkbox("Paper Enabled", value=True, key=f"workspace_new_paper_{user_id}")

                    new_symbols = st.text_input(
                        "Símbolos Permitidos",
                        value="BTC/USDT,ETH/USDT",
                        key=f"workspace_new_symbols_{user_id}",
                    )
                    new_timeframes = st.multiselect(
                        "Timeframes Permitidos",
                        options=["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                        default=["15m", "1h"],
                        key=f"workspace_new_timeframes_{user_id}",
                    )
                    new_notes = st.text_area("Notas", key=f"workspace_new_notes_{user_id}")

                    if st.form_submit_button("Adicionar Conta"):
                        if not str(new_account_id).strip():
                            st.error("Informe um account_id válido.")
                        else:
                            db.upsert_user_account(
                                {
                                    "user_id": user_id,
                                    "account_id": str(new_account_id).strip(),
                                    "account_alias": str(new_account_alias or new_account_id).strip(),
                                    "exchange": new_exchange,
                                    "status": new_status,
                                    "live_enabled": bool(new_live_enabled),
                                    "paper_enabled": bool(new_paper_enabled),
                                    "capital_base": float(new_capital_base),
                                    "risk_mode": "normal",
                                    "allowed_symbols": [item.strip() for item in str(new_symbols).split(",") if item.strip()],
                                    "allowed_timeframes": list(new_timeframes),
                                    "notes": new_notes,
                                }
                            )
                            st.success("Nova conta criada com sucesso.")
                            st.rerun()

        with detail_tab2:
            risk_profile = execution_context.get("risk_profile") or {}
            with st.form(f"workspace_risk_form_{selected_account_id}"):
                risk_col1, risk_col2, risk_col3 = st.columns(3)
                with risk_col1:
                    max_risk_per_trade = st.number_input(
                        "Risco por Trade %",
                        min_value=0.0,
                        value=float(risk_profile.get("max_risk_per_trade", 0.5) or 0.5),
                        step=0.1,
                        key=f"workspace_risk_trade_{selected_account_id}",
                    )
                    max_daily_loss = st.number_input(
                        "Loss Diário %",
                        min_value=0.0,
                        value=float(risk_profile.get("max_daily_loss", 2.0) or 2.0),
                        step=0.1,
                        key=f"workspace_daily_loss_{selected_account_id}",
                    )
                with risk_col2:
                    max_drawdown = st.number_input(
                        "Drawdown Máx %",
                        min_value=0.0,
                        value=float(risk_profile.get("max_drawdown", 10.0) or 10.0),
                        step=0.5,
                        key=f"workspace_drawdown_{selected_account_id}",
                    )
                    max_portfolio_open_risk_pct = st.number_input(
                        "Risco Aberto Máx %",
                        min_value=0.0,
                        value=float(risk_profile.get("max_portfolio_open_risk_pct", 2.0) or 2.0),
                        step=0.1,
                        key=f"workspace_open_risk_{selected_account_id}",
                    )
                with risk_col3:
                    allowed_position_count = st.number_input(
                        "Máx Posições",
                        min_value=0,
                        value=int(risk_profile.get("allowed_position_count", 3) or 3),
                        step=1,
                        key=f"workspace_positions_{selected_account_id}",
                    )
                    leverage_cap = st.number_input(
                        "Leverage Cap",
                        min_value=0.0,
                        value=float(risk_profile.get("leverage_cap", 5.0) or 5.0),
                        step=0.5,
                        key=f"workspace_leverage_{selected_account_id}",
                    )

                preferred_symbols = st.text_input(
                    "Símbolos Preferidos",
                    value=",".join(risk_profile.get("preferred_symbols", execution_context.get("allowed_symbols", []))),
                    key=f"workspace_pref_symbols_{selected_account_id}",
                )
                risk_mode_profile = st.selectbox(
                    "Modo do Perfil",
                    options=["normal", "reduced", "blocked"],
                    index=["normal", "reduced", "blocked"].index(
                        str(risk_profile.get("risk_mode") or "normal")
                        if str(risk_profile.get("risk_mode") or "normal") in {"normal", "reduced", "blocked"}
                        else "normal"
                    ),
                    key=f"workspace_profile_mode_{selected_account_id}",
                )
                risk_is_valid = st.checkbox(
                    "Perfil Válido",
                    value=bool(risk_profile.get("is_valid", True)),
                    key=f"workspace_profile_valid_{selected_account_id}",
                )
                risk_live_enabled = st.checkbox(
                    "Live liberado no risco",
                    value=bool(risk_profile.get("live_enabled", True)),
                    key=f"workspace_risk_live_{selected_account_id}",
                )
                risk_paper_enabled = st.checkbox(
                    "Paper liberado no risco",
                    value=bool(risk_profile.get("paper_enabled", True)),
                    key=f"workspace_risk_paper_{selected_account_id}",
                )

                if st.form_submit_button("Salvar Perfil de Risco"):
                    db.upsert_user_risk_profile(
                        {
                            "user_id": user_id,
                            "account_id": selected_account_id,
                            "max_risk_per_trade": float(max_risk_per_trade),
                            "max_daily_loss": float(max_daily_loss),
                            "max_drawdown": float(max_drawdown),
                            "max_portfolio_open_risk_pct": float(max_portfolio_open_risk_pct),
                            "allowed_position_count": int(allowed_position_count),
                            "preferred_symbols": [item.strip() for item in preferred_symbols.split(",") if item.strip()],
                            "leverage_cap": float(leverage_cap),
                            "risk_mode": risk_mode_profile,
                            "is_valid": bool(risk_is_valid),
                            "live_enabled": bool(risk_live_enabled),
                            "paper_enabled": bool(risk_paper_enabled),
                        }
                    )
                    st.success("Perfil de risco atualizado com sucesso.")
                    st.rerun()

        with detail_tab3:
            vault = None
            vault_error = ""
            try:
                from services.credential_vault import CredentialVault

                vault = CredentialVault(strict=False)
            except Exception as exc:
                vault_error = str(exc)

            if vault_error:
                st.error(f"Vault indisponível: {vault_error}")
            elif not vault or not vault.is_configured():
                st.warning("Configure CREDENTIAL_ENCRYPTION_KEY para liberar o armazenamento seguro das credenciais.")
            else:
                st.success(
                    f"Credencial atual: {execution_context.get('api_key_ref') or 'não cadastrada'} | "
                    f"Token ref: {execution_context.get('token_ref') or 'não cadastrado'}"
                )
                with st.form(f"workspace_credentials_form_{selected_account_id}"):
                    credential_alias = st.text_input(
                        "Alias da Credencial",
                        value=str(selected_account.get("account_alias") or selected_account_id),
                        key=f"workspace_cred_alias_{selected_account_id}",
                    )
                    api_key = st.text_input("API Key", type="password", key=f"workspace_api_key_{selected_account_id}")
                    api_secret = st.text_input("API Secret", type="password", key=f"workspace_api_secret_{selected_account_id}")
                    credential_notes = st.text_area("Notas da Credencial", key=f"workspace_cred_notes_{selected_account_id}")

                    if st.form_submit_button("Salvar Credenciais"):
                        if not api_key or not api_secret:
                            st.error("Informe API Key e API Secret para atualizar as credenciais.")
                        else:
                            vault.store_exchange_credentials(
                                db,
                                user_id=user_id,
                                account_id=selected_account_id,
                                exchange=selected_exchange,
                                api_key=api_key,
                                api_secret=api_secret,
                                credential_alias=credential_alias,
                                permissions_read=True,
                                permissions_trade=True,
                                permissions_withdraw=False,
                                permission_status=selected_account.get("permission_status", "unknown"),
                                token_status=selected_account.get("token_status", "unknown"),
                                reconciliation_status=selected_account.get("reconciliation_status", "unknown"),
                                notes=credential_notes,
                            )
                            st.success("Credenciais atualizadas com criptografia.")
                            st.rerun()

        with detail_tab4:
            render_workspace_account_runtime_panel(
                user_id=user_id,
                workspace_user=workspace_user,
                workspace_subscription=workspace_subscription,
                selected_account=selected_account,
                execution_context=execution_context,
            )

        with detail_tab5:
            events = db.get_user_execution_events(user_id=user_id, account_id=selected_account_id, limit=20)
            positions = db.get_user_live_positions(user_id=user_id, account_id=selected_account_id)
            orders = db.get_user_live_orders(user_id=user_id, account_id=selected_account_id)

            if positions:
                st.caption("Posições Live")
                st.dataframe(pd.DataFrame(positions), width="stretch", hide_index=True)
            else:
                st.info("Nenhuma posição live registrada para esta conta.")

            if orders:
                st.caption("Ordens Live")
                st.dataframe(pd.DataFrame(orders), width="stretch", hide_index=True)
            else:
                st.info("Nenhuma ordem live pendente para esta conta.")

            if events:
                events_df = pd.DataFrame(events)
                st.caption("Eventos Operacionais Recentes")
                st.dataframe(events_df, width="stretch", hide_index=True)
            else:
                st.info("Nenhum evento operacional recente para esta conta.")
    else:
        st.info("Nenhuma conta cadastrada para este usuário. Use o admin panel ou a abertura inicial de conta para começar.")

    st.markdown("---")
    st.subheader("🔒 Segurança da Sessão")
    with st.form(f"workspace_password_change_{user_id}"):
        pwd_col1, pwd_col2, pwd_col3 = st.columns(3)
        with pwd_col1:
            current_password = st.text_input("Senha Atual", type="password", key=f"workspace_current_password_{user_id}")
        with pwd_col2:
            new_password = st.text_input("Nova Senha", type="password", key=f"workspace_new_password_{user_id}")
        with pwd_col3:
            confirm_password = st.text_input("Confirmar Nova Senha", type="password", key=f"workspace_confirm_password_{user_id}")

        if st.form_submit_button("Atualizar Senha"):
            if new_password != confirm_password:
                st.error("A confirmação da nova senha não confere.")
            else:
                changed = db.change_dashboard_user_password(
                    user_id=user_id,
                    current_password=current_password,
                    new_password=new_password,
                )
                if changed:
                    refreshed_auth = dict(workspace_user)
                    refreshed_auth["require_password_change"] = False
                    refreshed_auth["session_token"] = workspace_user.get("session_token")
                    st.session_state.dashboard_user_auth = refreshed_auth
                    st.success("Senha atualizada com sucesso.")
                else:
                    st.error("Não foi possível atualizar a senha. Verifique a senha atual.")

# Configure page

def main():
    st.set_page_config(
        page_title="Trading Signals Dashboard",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    inject_dashboard_theme()

    # Incluir JavaScript para refresh suave
    st.markdown("""
    <script>
    // Auto-refresh suave sem recarregar página
    let refreshTimer = null;

    function smoothRefresh() {
        // Mostrar indicador de carregamento sutil
        const indicator = document.createElement('div');
        indicator.innerHTML = '🔄 Atualizando...';
        indicator.style.position = 'fixed';
        indicator.style.top = '10px';
        indicator.style.right = '10px';
        indicator.style.background = '#f0f8ff';
        indicator.style.padding = '5px 10px';
        indicator.style.borderRadius = '5px';
        indicator.style.fontSize = '12px';
        indicator.style.zIndex = '9999';
        indicator.style.opacity = '0.8';
        document.body.appendChild(indicator);
        
        // Remover indicador após 2 segundos
        setTimeout(() => {
            if (indicator.parentNode) {
                indicator.parentNode.removeChild(indicator);
            }
        }, 2000);
    }

    // Configurar refresh automático mais suave
    if (typeof window.streamlitAutoRefresh === 'undefined') {
        window.streamlitAutoRefresh = true;
        
        // Refresh a cada 45 segundos
        setInterval(() => {
            if (!document.hidden) {
                smoothRefresh();
                // Triggerar atualização suave do Streamlit
                window.parent.postMessage({
                    type: 'streamlit:componentReady',
                    data: { refresh: true }
                }, '*');
            }
        }, 45000);
    }
    </script>
    """, unsafe_allow_html=True)

    # Sidebar configuration - Move this section before session state initialization
    st.sidebar.title("🔧 Configurações")

    # Exchange selection
    st.sidebar.subheader("🌎 Exchange")

    supported_exchanges = [
        ExchangeConfig.normalize_exchange_name(exchange_name)
        for exchange_name in (AppConfig.BRAZIL_SUPPORTED_EXCHANGES or ["binanceusdm"])
    ]
    supported_exchanges = list(dict.fromkeys(supported_exchanges)) or ["binanceusdm"]
    current_exchange = ExchangeConfig.normalize_exchange_name(
        st.session_state.get("runtime_exchange_name") or os.getenv("SINGLE_USER_RUNTIME_EXCHANGE", "binanceusdm")
    )
    if current_exchange not in supported_exchanges:
        current_exchange = supported_exchanges[0]
    exchange_label_by_name = {
        exchange_name: ExchangeConfig.get_exchange_label(exchange_name)
        for exchange_name in supported_exchanges
    }
    selected_exchange_label = st.sidebar.selectbox(
        "Corretora do runtime",
        options=[exchange_label_by_name[exchange_name] for exchange_name in supported_exchanges],
        index=supported_exchanges.index(current_exchange),
        key="runtime_exchange_selector",
        help="Define onde as credenciais serão salvas e qual exchange o runtime real vai usar.",
    )
    selected_exchange = next(
        exchange_name
        for exchange_name, label in exchange_label_by_name.items()
        if label == selected_exchange_label
    )
    st.session_state.runtime_exchange_name = selected_exchange
    st.sidebar.success(f"✅ Runtime selecionado: {ExchangeConfig.get_exchange_label(selected_exchange)}")
    if selected_exchange == "binanceusdm":
        st.sidebar.info("📡 Mercado em tempo real via Binance Futures.")
    else:
        st.sidebar.warning(
            "Bybit habilitada para credenciais/runtime via CCXT. Valide em testnet antes de operar conta real."
        )

    initialize_dashboard_session_state()
    runtime_bootstrap_error = ""
    runtime_trading_bot = get_session_trading_bot_safe(selected_exchange, force_init=True)
    if runtime_trading_bot is None:
        runtime_bootstrap_error = "Runtime de mercado indisponível no momento."

    if ProductionConfig.ENABLE_DASHBOARD_BACKGROUND_BOT:
        logger.warning("ENABLE_DASHBOARD_BACKGROUND_BOT foi definido, mas o modo recomendado e executar o bot por bot_runner.py")

    dashboard_user = get_authenticated_dashboard_user()
    admin_session_active = is_admin_dashboard_session_active()
    admin_entry_requested = is_admin_dashboard_entry_requested()
    dashboard_sections = [
        ("workspace", "👤 Workspace"),
        ("market", "📈 Mercado"),
        ("bot", "🤖 Bot Trader"),
        ("backtest", "🔬 Backtest"),
    ]
    if admin_session_active or admin_entry_requested:
        dashboard_sections.append(("admin", "👑 Admin"))
    dashboard_section_labels = [label for _, label in dashboard_sections]
    dashboard_section_by_label = {label: section_id for section_id, label in dashboard_sections}
    raw_default_dashboard_section = str(st.session_state.get("default_tab") or "").strip().lower()
    legacy_dashboard_section_map = {
        "websocket": "market",
        "futures": "market",
        "export": "backtest",
    }
    default_dashboard_section = legacy_dashboard_section_map.get(raw_default_dashboard_section, raw_default_dashboard_section)
    if default_dashboard_section not in {section_id for section_id, _ in dashboard_sections}:
        if dashboard_user:
            default_dashboard_section = "market"
        elif admin_session_active or admin_entry_requested:
            default_dashboard_section = "admin"
        else:
            default_dashboard_section = "workspace"

    if "market_view_mode" not in st.session_state:
        if raw_default_dashboard_section == "futures":
            st.session_state.market_view_mode = "Operacao & Risco"
        else:
            st.session_state.market_view_mode = "Bot em Operacao"

    default_dashboard_index = next(
        (index for index, (section_id, _) in enumerate(dashboard_sections) if section_id == default_dashboard_section),
        1 if dashboard_user else (len(dashboard_sections) - 1 if admin_session_active or admin_entry_requested else 0),
    )
    sidebar_selected_dashboard_label = str(
        st.session_state.get("dashboard_main_section") or dashboard_section_labels[default_dashboard_index]
    )
    if sidebar_selected_dashboard_label not in dashboard_section_by_label:
        sidebar_selected_dashboard_label = dashboard_section_labels[default_dashboard_index]
        st.session_state.dashboard_main_section = sidebar_selected_dashboard_label
    sidebar_active_section = dashboard_section_by_label.get(sidebar_selected_dashboard_label, default_dashboard_section)
    live_sidebar_sections = {"market"}
    show_live_sidebar_controls = sidebar_active_section in live_sidebar_sections

    if show_live_sidebar_controls:
        if get_session_trading_bot_safe(selected_exchange) is None:
            message = runtime_bootstrap_error or f"Erro ao configurar {selected_exchange}."
            st.sidebar.error(message)

    st.sidebar.markdown("---")
    st.sidebar.subheader("👤 Workspace Multiusuário")
    if dashboard_user:
        dashboard_user_label = (
            dashboard_user.get("first_name")
            or dashboard_user.get("username")
            or dashboard_user.get("login_name")
            or str(dashboard_user.get("user_id"))
        )
        expires_at_label = str(dashboard_user.get("expires_at") or "")
        st.sidebar.success(f"Sessão ativa: {dashboard_user_label}")
        if expires_at_label:
            st.sidebar.caption(f"Sessão válida até: {expires_at_label}")
        subscription_payload = dashboard_user.get("subscription") or {}
        subscription_plan = str(subscription_payload.get("plan_code") or "free").upper()
        subscription_status = str(subscription_payload.get("status") or "inactive").lower()
        subscription_expires_at = subscription_payload.get("expires_at")
        if subscription_payload.get("is_active"):
            st.sidebar.success(f"Plano ativo: {subscription_plan}")
        else:
            st.sidebar.warning(f"Plano inativo: {subscription_plan} ({subscription_status})")
        if subscription_expires_at:
            st.sidebar.caption(f"Assinatura expira em: {subscription_expires_at}")
        if subscription_payload.get("expiring_soon"):
            st.sidebar.warning(
                f"Sua assinatura expira em {int(subscription_payload.get('days_remaining', 0))} dia(s). Renove para não interromper o bot."
            )
        license_payload = dashboard_user.get("license") or validate_dashboard_license_for_user(dashboard_user)
        dashboard_user["license"] = license_payload
        if license_payload.get("allowed"):
            if license_payload.get("bound_now"):
                st.sidebar.success("Licença vinculada a este acesso.")
            else:
                st.sidebar.success("Licença válida neste dispositivo/IP.")
        else:
            st.sidebar.error(f"Licença bloqueada: {license_payload.get('reason') or 'acesso não autorizado'}")
        if dashboard_user.get("require_password_change"):
            st.sidebar.warning("Troque sua senha no workspace antes de operar regularmente.")
        if st.sidebar.button("Sair do Workspace", key="dashboard_user_logout"):
            clear_dashboard_user_session()
            st.rerun()
    else:
        if admin_session_active:
            st.sidebar.success("Sessão Admin ativa: acesso total liberado.")
            st.sidebar.caption(
                "O login do Workspace abaixo é opcional e serve apenas para testar a jornada do usuário final."
            )
        with st.sidebar.form("dashboard_user_login_form"):
            login_value = st.text_input("Login do Workspace", key="dashboard_user_login")
            password_value = st.text_input("Senha do Workspace", type="password", key="dashboard_user_password")
            if st.form_submit_button("Entrar no Workspace"):
                st.session_state.dashboard_user_auth_error = ""
                authenticated_user = db.authenticate_dashboard_user(
                    login_name=login_value,
                    password=password_value,
                )
                if authenticated_user:
                    license_payload = validate_dashboard_license_for_user(authenticated_user)
                    authenticated_user["license"] = license_payload
                    if not license_payload.get("allowed"):
                        st.session_state.dashboard_user_auth_error = (
                            "❌ Licença bloqueada para este IP/dispositivo. "
                            f"Motivo: {license_payload.get('reason') or 'não autorizado'}."
                        )
                    else:
                        session_expires_at = (
                            now_brazil() + timedelta(hours=ProductionConfig.DASHBOARD_USER_SESSION_TIMEOUT_HOURS)
                        ).isoformat()
                        authenticated_user["expires_at"] = session_expires_at
                        try:
                            session_token = db.create_dashboard_user_session(
                                user_id=int(authenticated_user["user_id"]),
                                login_name=str(authenticated_user.get("login_name") or login_value),
                                expires_at=session_expires_at,
                            )
                            authenticated_user["session_token"] = session_token
                            _set_persistent_dashboard_session_token(session_token)
                        except Exception:
                            logger.warning("Falha ao criar sessao persistente da dashboard.", exc_info=True)
                        st.session_state.dashboard_user_auth = authenticated_user
                        st.session_state.dashboard_user_login = ""
                        st.session_state.dashboard_user_password = ""
                        st.session_state.dashboard_user_auth_error = ""
                        st.rerun()
                else:
                    st.session_state.dashboard_user_auth_error = "❌ Login ou senha inválidos."
        if st.session_state.get("dashboard_user_auth_error"):
            st.sidebar.error(st.session_state.dashboard_user_auth_error)
        if ProductionConfig.ALLOW_SELF_SERVICE_SIGNUP and admin_session_active:
            st.sidebar.caption(
                "Cadastro assistido pelo Admin. Use apenas para criar acesso sob sua supervisão."
            )
            with st.sidebar.expander("📝 Criar Conta Agora", expanded=False):
                with st.form("dashboard_self_signup_form"):
                    st.text_input("Login desejado", key="dashboard_self_signup_login")
                    st.text_input("Senha", type="password", key="dashboard_self_signup_password")
                    st.text_input("Confirmar senha", type="password", key="dashboard_self_signup_password_confirm")
                    st.text_input("Nome de exibição (opcional)", key="dashboard_self_signup_display_name")
                    st.text_input("Contato (Telegram/email opcional)", key="dashboard_self_signup_contact")
                    st.text_area("Observações (opcional)", key="dashboard_self_signup_notes")
                    if st.form_submit_button("Criar Conta"):
                        signup_login = str(st.session_state.get("dashboard_self_signup_login") or "").strip()
                        signup_password = str(st.session_state.get("dashboard_self_signup_password") or "")
                        signup_password_confirm = str(st.session_state.get("dashboard_self_signup_password_confirm") or "")
                        if not signup_login or not signup_password:
                            st.error("Preencha login e senha para criar a conta.")
                        elif signup_password != signup_password_confirm:
                            st.error("A confirmação da senha não confere.")
                        else:
                            try:
                                created = db.register_dashboard_user_selfservice(
                                    {
                                        "login_name": signup_login,
                                        "password": signup_password,
                                        "display_name": st.session_state.get("dashboard_self_signup_display_name"),
                                        "contact_text": st.session_state.get("dashboard_self_signup_contact"),
                                        "notes": st.session_state.get("dashboard_self_signup_notes"),
                                    }
                                )
                                st.success(
                                    f"Conta criada com sucesso (User ID {created.get('user_id')}). "
                                    "Faça login e ative um plano para operar o bot."
                                )
                                st.session_state.dashboard_self_signup_login = ""
                                st.session_state.dashboard_self_signup_password = ""
                                st.session_state.dashboard_self_signup_password_confirm = ""
                                st.session_state.dashboard_self_signup_display_name = ""
                                st.session_state.dashboard_self_signup_contact = ""
                                st.session_state.dashboard_self_signup_notes = ""
                            except Exception as signup_exc:
                                st.error(f"Não foi possível criar a conta: {signup_exc}")
        else:
            st.sidebar.caption(
                "Cadastro público desativado. O acesso é liberado somente pelo administrador."
            )

    # Continue with sidebar configuration

    if show_live_sidebar_controls:
        if st.sidebar.button("🧪 Testar Exchange Selecionada"):
            with st.spinner(f"Testando {ExchangeConfig.get_exchange_label(selected_exchange)}..."):
                try:
                    use_testnet_runtime = str(os.getenv("TESTNET", "true")).strip().lower() in {"1", "true", "yes", "on", "y", "sim"}
                    success, message = ExchangeConfig.test_connection(selected_exchange, testnet=use_testnet_runtime)

                    if success:
                        st.sidebar.success("✅ Exchange selecionada respondeu com sucesso!")
                        with st.sidebar.expander("📊 Detalhes da Conexão"):
                            st.text(message)
                    else:
                        st.sidebar.error("❌ Problema com a exchange selecionada")
                        with st.sidebar.expander("🔍 Detalhes do Erro"):
                            st.text(message)

                except Exception as e:
                    st.sidebar.error(f"❌ Erro: {str(e)}")

        if st.sidebar.button("🔍 Diagnóstico WebSocket"):
            with st.spinner("Executando diagnóstico WebSocket..."):
                st.sidebar.markdown("**🔍 Relatório WebSocket:**")

                try:
                    import requests
                    requests.get("https://httpbin.org/ip", timeout=5).raise_for_status()
                    st.sidebar.success("✅ Conexão com internet OK")
                except Exception:
                    st.sidebar.error("❌ Sem conexão com internet")

                try:
                    import requests
                    exchange_is_bybit = ExchangeConfig.normalize_exchange_name(selected_exchange) == "bybit"
                    use_testnet_runtime = str(os.getenv("TESTNET", "true")).strip().lower() in {"1", "true", "yes", "on", "y", "sim"}
                    if exchange_is_bybit:
                        api_ping_url = (
                            "https://api-testnet.bybit.com/v5/market/time"
                            if use_testnet_runtime
                            else "https://api.bybit.com/v5/market/time"
                        )
                    else:
                        api_ping_url = (
                            "https://testnet.binancefuture.com/fapi/v1/ping"
                            if use_testnet_runtime
                            else "https://fapi.binance.com/fapi/v1/ping"
                        )
                    requests.get(api_ping_url, timeout=5).raise_for_status()
                    st.sidebar.success("✅ API da exchange acessível")
                except Exception:
                    st.sidebar.error("❌ Problema com a API da exchange")

                try:
                    import requests
                    requests.get("https://fstream.binance.com", timeout=5).raise_for_status()
                    st.sidebar.success("✅ WebSocket Binance Futures disponível")
                except Exception as e:
                    st.sidebar.error(f"❌ WebSocket: {str(e)[:50]}...")

    available_pairs = AppConfig.get_supported_pairs()
    supported_timeframes = AppConfig.get_supported_timeframes()
    timeframe_default = AppConfig.DEFAULT_TIMEFRAME if AppConfig.DEFAULT_TIMEFRAME in supported_timeframes else supported_timeframes[0]
    symbol = AppConfig.DEFAULT_SYMBOL if AppConfig.DEFAULT_SYMBOL in available_pairs else available_pairs[0]
    selected_symbols = [symbol]
    enable_multi_symbol = False
    timeframe = timeframe_default
    rsi_period = AppConfig.DEFAULT_RSI_PERIOD
    rsi_min = AppConfig.DEFAULT_RSI_MIN
    rsi_max = AppConfig.DEFAULT_RSI_MAX
    crypto_settings = AppConfig.get_crypto_timeframe_settings(timeframe)
    min_confidence = crypto_settings['min_confidence']
    require_volume = False
    require_trend = False
    avoid_ranging = False
    day_trading_mode = False
    filter_extreme_volatility = True
    require_stoch_confirmation = True
    peak_hours_only = False
    avoid_lunch_time = False
    alert_volume_spike = False
    alert_breakout = False
    auto_refresh = bool(st.session_state.get("auto_refresh", True))

    if show_live_sidebar_controls:
        st.sidebar.subheader("📊 Configuração de Pares")

        if AppConfig.SINGLE_SETUP_MODE:
            symbol = st.sidebar.selectbox(
                "📈 Par para análise:",
                available_pairs,
                index=available_pairs.index(AppConfig.DEFAULT_SYMBOL) if AppConfig.DEFAULT_SYMBOL in available_pairs else 0,
                help="O perfil global continua fixo, mas a auditoria analitica pode ser feita em outros pares.",
                key="single_setup_symbol",
            )
            selected_symbols = [symbol]
            symbol_family_label = AppConfig.get_symbol_profile_family_label(symbol)
            st.sidebar.info(
                "\n".join(
                    [
                        f"Perfil base: {AppConfig.DEFAULT_BACKTEST_PRESET}",
                        f"Janela base: {AppConfig.DEFAULT_TIMEFRAME} + contexto {AppConfig.PRIMARY_CONTEXT_TIMEFRAME}",
                        f"Familia observada: {symbol_family_label}",
                        f"Analisando: {symbol}",
                    ]
                )
            )
        else:
            trading_mode = st.sidebar.radio(
                "Modo de Análise:",
                ["Par Único", "Múltiplos Pares"],
                help="Escolha analisar um par ou monitorar vários simultaneamente"
            )

            if trading_mode == "Múltiplos Pares":
                enable_multi_symbol = True
                selected_symbols = st.sidebar.multiselect(
                    "📊 Selecionar pares para monitorar:",
                    available_pairs,
                    default=available_pairs[:3] if len(available_pairs) >= 3 else available_pairs,
                    help="Escolha até 10 pares para análise simultânea"
                )

                if not selected_symbols:
                    st.sidebar.warning("⚠️ Selecione pelo menos um par")
                    selected_symbols = [available_pairs[0]]

                symbol = selected_symbols[0]
            else:
                symbol = st.sidebar.selectbox(
                    "📈 Par Principal de Trading:",
                    available_pairs,
                    index=available_pairs.index(AppConfig.DEFAULT_SYMBOL) if AppConfig.DEFAULT_SYMBOL in available_pairs else 0,
                    help="Par principal para análise detalhada"
                )
                selected_symbols = [symbol]

        st.sidebar.success(f"✅ Par ativo: {symbol}")
        st.sidebar.info(f"🔄 WebSocket conectará automaticamente ao {symbol.replace('/', '')}")

        timeframe = st.sidebar.selectbox(
            "Timeframe",
            supported_timeframes,
            index=supported_timeframes.index(timeframe_default),
            disabled=AppConfig.SINGLE_SETUP_MODE
        )

        st.sidebar.subheader("📊 Gatilhos RSI do Motor EMA/RSI")
        rsi_period = st.sidebar.slider("Período RSI", 5, 50, AppConfig.DEFAULT_RSI_PERIOD, help="14 períodos é o padrão mais testado")
        rsi_min = st.sidebar.slider("RSI Gatilho Compra", 45, 70, AppConfig.DEFAULT_RSI_MIN, help="RSI precisa cruzar acima deste nivel para compra")
        rsi_max = st.sidebar.slider("RSI Gatilho Venda", 30, 55, AppConfig.DEFAULT_RSI_MAX, help="RSI precisa cruzar abaixo deste nivel para venda")

        with st.sidebar.expander("📈 Day Trading Otimizado", expanded=True):
            st.markdown("**⚡ Configurações para Day Trader**")

            day_trading_supported = timeframe in {"1m", "5m", "15m"} and not AppConfig.SINGLE_SETUP_MODE
            day_trading_mode = st.checkbox(
                "🚀 Modo Day Trading",
                value=False,
                disabled=not day_trading_supported,
                help="Configurações otimizadas para operações rápidas"
            )
            if not day_trading_supported:
                st.caption("Modo day trading indisponivel para o timeframe ou leitura operacional atual.")

            if day_trading_mode:
                day_settings = AppConfig.get_day_trading_settings(timeframe)
                st.success(f"✅ **Day Trading {timeframe}**: RSI {day_settings['rsi_oversold']}-{day_settings['rsi_overbought']}")
                st.info(f"⚡ Confiança: {day_settings['min_confidence']}% | Volume: {day_settings['min_volume_ratio']}x")
                rsi_min = day_settings['rsi_oversold']
                rsi_max = day_settings['rsi_overbought']
                min_confidence = day_settings['min_confidence']
                if timeframe == "1m":
                    st.warning("⚡ **SCALPING MODE** - Apenas para traders experientes")
                elif timeframe == "5m":
                    st.success("🎯 **Configuração IDEAL para Day Trading**")
            else:
                crypto_settings = AppConfig.get_crypto_timeframe_settings(timeframe)
                min_confidence = crypto_settings['min_confidence']
                st.info(
                    f"📊 **Auto-Config {timeframe}**: RSI {crypto_settings['rsi_oversold']}-{crypto_settings['rsi_overbought']}, "
                    f"Confiança {crypto_settings['min_confidence']}%"
                )

        with st.sidebar.expander("⚙️ Configurações Avançadas", expanded=False):
            if not day_trading_mode:
                use_auto_config = st.checkbox("🤖 Usar Configuração Automática", value=True, help="Configuração otimizada para crypto + timeframe")

                if use_auto_config:
                    crypto_settings = AppConfig.get_crypto_timeframe_settings(timeframe)
                    min_confidence = crypto_settings['min_confidence']
                    rsi_min = crypto_settings['rsi_oversold']
                    rsi_max = crypto_settings['rsi_overbought']
                    st.success(f"✅ Auto: RSI {rsi_min}-{rsi_max}, Confiança {min_confidence}%")
                else:
                    st.markdown("**Filtros de Qualidade de Sinal**")
                    min_confidence = st.slider("Confiança Mínima (%)", 50, 90, 70, help="Apenas sinais com alta confiança")
            else:
                st.markdown("**✅ Day Trading: Configurações Otimizadas Ativas**")

            require_volume = st.checkbox("Exigir Volume Alto", value=False, help="Volume 80%+ acima da média")
            require_trend = st.checkbox("Exigir Tendência Clara", value=False, help="ADX > 28")
            avoid_ranging = st.checkbox("Evitar Mercados Laterais", value=False, help="Filtro anti-ranging")

            if day_trading_mode:
                st.markdown("**⚡ Filtros Day Trading**")
                filter_extreme_volatility = st.checkbox("Filtrar Volatilidade Extrema", value=True, help="Evitar ATR > 12% para day trading")
                require_stoch_confirmation = st.checkbox("Exigir StochRSI Extremo", value=True, help="StochRSI < 15 ou > 85")
                peak_hours_only = st.checkbox("Apenas Horários de Pico", value=True, help="9-11h, 14-16h, 20-22h BRT")
                avoid_lunch_time = st.checkbox("Evitar Horário Almoço", value=True, help="12-14h tem menos volume")
                st.markdown("**🎯 Alertas Day Trading**")
                alert_volume_spike = st.checkbox("Alertar Picos de Volume", value=True, help="Volume > 3x média")
                alert_breakout = st.checkbox("Alertar Breakouts", value=True, help="Rompimento de Bollinger Bands")
            else:
                st.markdown("**🚀 Filtros Especiais Crypto**")
                filter_extreme_volatility = st.checkbox("Filtrar Volatilidade Extrema", value=True, help="Evitar ATR > 8%")
                require_stoch_confirmation = st.checkbox("Exigir Confirmação StochRSI", value=True, help="StochRSI em extremos")
                peak_hours_only = st.checkbox("Apenas Horários de Pico", value=False, help="8-16h e 20-23h BRT")

        auto_refresh = st.sidebar.checkbox("🔄 Atualização Automática", value=True)
        st.session_state.auto_refresh = auto_refresh

        if st.sidebar.button("🔄 Atualizar Agora"):
            with st.spinner('🔄 Atualizando dados...'):
                try:
                    st.session_state.last_update = None
                    st.session_state.current_data = None
                    st.session_state.last_market_timestamp = None
                    runtime_bot = get_session_trading_bot_safe(selected_exchange)
                    if runtime_bot is None:
                        st.error("❌ Runtime de mercado indisponível. Tente novamente em alguns segundos.")
                    else:
                        new_data = runtime_bot.get_market_data()
                        if new_data is not None:
                            market_timestamp = _extract_market_timestamp_reference(new_data)
                            st.session_state.current_data = new_data
                            st.session_state.last_update = get_brazil_datetime_naive()
                            st.session_state.last_market_timestamp = market_timestamp

                    st.success("✅ Dados atualizados!")
                    st.rerun()
                except Exception:
                    logger.warning("Falha ao atualizar dados manualmente no dashboard.", exc_info=True)
                    st.error("❌ Não foi possível atualizar os dados agora.")
    else:
        st.sidebar.caption(
            "Controles de mercado ao vivo ficam visíveis apenas na seção Mercado."
        )

    telegram_enabled = False
    if show_live_sidebar_controls:
        st.sidebar.markdown("---")
        st.sidebar.subheader("📱 Configuração Telegram")

        telegram_service_available = is_telegram_service_available()
        if telegram_service_available:
            telegram_bot = get_or_init_session_telegram_bot()
            config_status = telegram_bot.get_config_status()
            has_secrets = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))

            if config_status['configured'] or has_secrets:
                if has_secrets:
                    st.sidebar.success("✅ Telegram configurado via variaveis de ambiente!")
                else:
                    st.sidebar.success("✅ Telegram configurado!")

                col1, col2 = st.sidebar.columns(2)
                with col1:
                    if st.sidebar.button("🧪 Testar"):
                        try:
                            success, msg = run_async_task_sync(telegram_bot.test_connection())
                            if success:
                                st.sidebar.success(msg)
                            else:
                                st.sidebar.error(msg)
                        except Exception as e:
                            st.sidebar.error(f"❌ Erro no teste: {str(e)}")

                with col2:
                    if st.sidebar.button("🗑️ Remover"):
                        telegram_bot.disable()
                        st.rerun()

                telegram_enabled = st.sidebar.checkbox(
                    "Ativar notificações automáticas",
                    value=True,
                    help="Enviar sinais automaticamente via Telegram"
                )
                st.session_state.telegram_notifications = telegram_enabled
            else:
                st.sidebar.info("🔧 Configure seu bot do Telegram para esta sessao:")

                with st.sidebar.form("telegram_config"):
                    st.markdown("""
                    **Como obter suas credenciais:**
                    1. **Token do Bot:** Fale com @BotFather no Telegram
                    2. **Chat ID:** Envie /start para @userinfobot
                    """)

                    bot_token = st.text_input(
                        "🤖 Token do Bot:",
                        type="password",
                        help="Obtido do @BotFather",
                        placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
                    )

                    chat_id = st.text_input(
                        "💬 Chat ID:",
                        help="Seu ID de chat pessoal",
                        placeholder="123456789"
                    )

                    st.caption("Esses dados valem apenas nesta sessao do dashboard. Para persistir, use TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no ambiente.")

                    submitted = st.form_submit_button("Aplicar nesta sessao")

                    if submitted:
                        if bot_token and chat_id:
                            success, message = telegram_bot.configure(bot_token, chat_id)
                            if success:
                                st.sidebar.success(message)
                                st.rerun()
                            else:
                                st.sidebar.error(message)
                        else:
                            st.sidebar.warning("⚠️ Preencha todos os campos!")

                st.session_state.telegram_notifications = False
        else:
            st.sidebar.error("⚠️ Biblioteca Telegram não disponível")
            st.sidebar.info("Execute: pip install python-telegram-bot")
            st.session_state.telegram_notifications = False
    else:
        st.session_state.telegram_notifications = False

    # Telegram configuration completed - previous duplicate code removed

    live_strategy_settings = None
    active_live_profile = None
    if show_live_sidebar_controls:
        runtime_bot = get_session_trading_bot_safe(selected_exchange)
        if runtime_bot is None:
            st.sidebar.error("Runtime de mercado indisponível para atualizar parâmetros agora.")
            config_changed = False
        else:
            config_changed = runtime_bot.update_config(
                symbol=symbol,
                timeframe=timeframe,
                rsi_period=rsi_period,
                rsi_min=rsi_min,
                rsi_max=rsi_max
            )

        if config_changed:
            logger.info(
                "Configuracao do bot atualizada: %s %s RSI(%s) %s-%s",
                symbol,
                timeframe,
                rsi_period,
                rsi_min,
                rsi_max
            )

        live_strategy_settings = get_effective_strategy_settings(
            symbol,
            timeframe,
            require_volume=require_volume,
            require_trend=require_trend,
        )
        active_live_profile = live_strategy_settings.get("active_profile")
        if active_live_profile and runtime_bot is not None:
            runtime_bot.update_config(
                symbol=symbol,
                timeframe=timeframe,
                rsi_period=live_strategy_settings["rsi_period"],
                rsi_min=live_strategy_settings["rsi_min"],
                rsi_max=live_strategy_settings["rsi_max"],
            )

    # Main dashboard
    WEBSOCKET_AVAILABLE = False

    FUTURES_AVAILABLE = FuturesTrading is not None

    if FUTURES_AVAILABLE and st.session_state.get("futures_trading") is None:
        try:
            st.session_state.futures_trading = FuturesTrading()
        except Exception as e:
            st.sidebar.warning(f"⚠️ Futures trading não disponível: {str(e)}")
            st.session_state.futures_trading = None
            FUTURES_AVAILABLE = False

    selected_dashboard_label = st.radio(
        "Seção da Dashboard",
        dashboard_section_labels,
        index=default_dashboard_index,
        horizontal=True,
        key="dashboard_main_section",
        label_visibility="collapsed",
    )
    active_dashboard_section = dashboard_section_by_label[selected_dashboard_label]
    st.session_state.default_tab = active_dashboard_section

    render_dashboard_shell(
        active_section_label=selected_dashboard_label,
        dashboard_user=dashboard_user,
        admin_session_active=admin_session_active,
        selected_exchange=selected_exchange,
    )

    active_market_view = None
    if active_dashboard_section == "market":
        render_section_hero(
            kicker="Mercado",
            title="Central de mercado e leitura operacional",
            subtitle="Acompanhe streaming, gráfico, sinal e contexto sem misturar isso com o controle do runtime.",
            badges=[
                _build_status_pill("Símbolo", symbol, "accent"),
                _build_status_pill("Timeframe", timeframe, "default"),
                _build_status_pill("Fonte", "WebSocket Binance", "warm"),
            ],
        )
        market_view_mode = st.radio(
            "Visao de Mercado",
            options=["Bot em Operacao", "Operacao & Risco"],
            horizontal=True,
            key="market_view_mode",
            help="Use Bot em Operacao para acompanhar o runtime real; Operacao & Risco mostra sinal, contexto e calculadoras.",
        )
        active_market_view = "websocket" if market_view_mode == "Bot em Operacao" else "futures"

    if active_dashboard_section == "workspace":
        render_multiuser_workspace_tab()

    # Central de mercado - streaming visual
    if active_market_view == "websocket":
        st.subheader("🤖 Bot em Operacao")
        st.markdown("**Acompanhe se o evo-bot esta vivo, sincronizado e tomando decisoes.**")

        try:
            from trading_bot_websocket import StreamlinedTradingBot

            WEBSOCKET_AVAILABLE = True
        except ImportError:
            WEBSOCKET_AVAILABLE = False

        if WEBSOCKET_AVAILABLE:
            # Interface limpa do WebSocket
                
            # Auto-conectar WebSocket baseado na configuração da sidebar
            st.success(f"📊 **Auto-Conectado:** {symbol} | **Timeframe:** {timeframe}")
            st.info("🚀 *WebSocket conecta automaticamente com as configurações da sidebar*")
            
            # Configurações WebSocket usando o stream compartilhado do TradingBot
            ws_display_symbol = symbol.replace('/', '')  # BTC/USDT -> BTCUSDT
            ws_timeframe = timeframe
            ws_key = f"{symbol}_{ws_timeframe}"
            stream_client = None
            stream_status = None
            runtime_bot = get_session_trading_bot_safe(selected_exchange)

            if 'ws_auto_connected' not in st.session_state:
                st.session_state.ws_auto_connected = False
            if 'ws_current_key' not in st.session_state:
                st.session_state.ws_current_key = None

            if runtime_bot is None:
                st.session_state.ws_auto_connected = False
                st.error("❌ Runtime de mercado indisponível para inicializar stream.")
            else:
                try:
                    stream_client = runtime_bot._get_realtime_stream_client(
                        symbol=symbol,
                        timeframe=ws_timeframe,
                    )
                    if stream_client is None:
                        raise RuntimeError("Cliente de stream nao inicializado.")
                    stream_status = stream_client.get_current_status()
                    st.session_state.ws_auto_connected = True
                    if st.session_state.get('ws_current_key') != ws_key:
                        st.session_state.ws_current_key = ws_key
                        st.success(f"✅ Stream compartilhado pronto para {ws_display_symbol}")
                except Exception as e:
                    st.session_state.ws_auto_connected = False
                    stream_client = None
                    stream_status = None
                    st.error(f"❌ Erro ao inicializar stream compartilhado: {e}")
            
            # Status e controles do WebSocket
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if stream_status and stream_status.get("connected"):
                    st.success("🟢 **Conectado**")
                    if enable_multi_symbol:
                        st.info(f"📊 Modo: {len(selected_symbols)} pares")
                    else:
                        st.info(f"📈 Foco: {symbol}")
                elif stream_client:
                    st.warning("🟡 **Conectando**")
                else:
                    st.error("🔴 **Desconectado**")
                        
            with col2:
                if st.button("📊 Status Detalhado"):
                    if stream_client:
                        try:
                            status = stream_client.get_current_status()
                            st.json(status)
                        except Exception:
                            st.info("📊 Bot ativo - Status em tempo real")
                    else:
                        st.warning("⚠️ Bot não inicializado")
                        
            with col3:
                if st.button("🔄 Reconectar"):
                    try:
                        if runtime_bot is None:
                            st.warning("⚠️ Runtime de mercado indisponível para reconexão.")
                        else:
                            runtime_bot.reset_stream_client(
                                symbol=symbol,
                                timeframe=ws_timeframe,
                            )
                            stream_client = runtime_bot._get_realtime_stream_client(
                                symbol=symbol,
                                timeframe=ws_timeframe,
                            )
                            if stream_client is None:
                                raise RuntimeError("Cliente de stream nao inicializado apos reconexao.")
                            stream_status = stream_client.get_current_status()
                            st.session_state.ws_auto_connected = True
                            st.session_state.ws_current_key = ws_key
                            st.success("✅ WebSocket reconectado")
                    except Exception as e:
                        st.session_state.ws_auto_connected = False
                        st.error(f"❌ Erro na reconexão: {e}")
            
            # Área de dados em tempo real 
            if stream_client:
                st.markdown("---")
                st.subheader("🤖 Runtime do Bot")
                
                # Status do streaming
                st.success("🔗 **WebSocket ativo** - dados de mercado disponiveis como apoio")
                
                # Informações de conexão
                st.info(f"📡 Contexto de mercado para {ws_display_symbol} no timeframe {ws_timeframe}")

                render_bot_operation_snapshot(
                    symbol=symbol,
                    timeframe=ws_timeframe,
                    stream_status=stream_status,
                )
                
                # Métricas em tempo real
                st.markdown("### Contexto de Mercado Auxiliar")
                col1, col2, col3, col4 = st.columns(4)
                market_data = st.session_state.get("current_data")
                latest_market_row = None
                if isinstance(market_data, pd.DataFrame) and not market_data.empty:
                    latest_market_row = market_data.iloc[-1]
                
                with col1:
                    try:
                        price = float((stream_status or {}).get("last_price") or 0)
                        if price <= 0 and latest_market_row is not None:
                            price = float(latest_market_row.get("close", 0) or 0)
                        if price > 0:
                            st.metric(
                                label="💰 Preço",
                                value=f"${price:.6f}",
                                delta="WebSocket"
                            )
                        else:
                            st.metric(
                                label="💰 Preço",
                                value="Conectando...",
                                delta="Aguarde"
                            )
                    except Exception:
                        st.metric(
                            label="💰 Preço",
                            value="Carregando...",
                            delta="WebSocket"
                        )
                        
                with col2:
                    rsi_value = None
                    if latest_market_row is not None:
                        rsi_value = latest_market_row.get("rsi")
                    st.metric(
                        label="📊 RSI",
                        value=f"{float(rsi_value):.2f}" if pd.notna(rsi_value) else "Aguardando",
                        delta="Indicadores"
                    )
                    
                with col3:
                    macd_value = None
                    if latest_market_row is not None:
                        macd_value = latest_market_row.get("macd")
                    st.metric(
                        label="📈 MACD",
                        value=f"{float(macd_value):.4f}" if pd.notna(macd_value) else "Aguardando",
                        delta="Indicadores"
                    )
                    
                with col4:
                    try:
                        signal = "AGUARDANDO"
                        if latest_market_row is not None:
                            signal = latest_market_row.get("signal") or signal
                        elif stream_status and stream_status.get("connected"):
                            signal = "STREAMING"
                        st.metric(
                            label="🎯 Sinal",
                            value=signal,
                            delta="Compartilhado"
                        )
                    except Exception:
                        st.metric(
                            label="🎯 Sinal",
                            value="CONECTANDO",
                            delta="WebSocket"
                        )
                
                st.success("✅ **Stream compartilhado ativo** - usado como contexto, nao como painel principal")

                st.markdown("---")
                with st.expander("📈 Grafico de mercado auxiliar", expanded=False):
                    render_live_market_chart(symbol=symbol, timeframe=ws_timeframe, fallback_data=market_data)
                    
            # Informações sobre dados públicos
            with st.expander("ℹ️ Sobre o stream publico da Binance Futures", expanded=False):
                st.markdown("""
                **🔗 Conexão WebSocket Pública:**
                
                ✅ **Sem credenciais necessárias**
                - Dados de preço em tempo real
                - Volume e estatísticas 24h
                - Candlesticks (klines) ao vivo
                
                📊 **Análise Técnica:**
                - RSI, MACD, Bollinger Bands
                - Médias móveis (SMA, EMA)
                - Sinais de compra/venda automáticos
                
                ⚡ **Vantagens:**
                - Loop automático a cada 60 segundos
                - Sem limite de rate API  
                - Dados em tempo real
                - Totalmente gratuito
                
                ⏰ **Papel nesta tela:**
                - Serve como contexto de mercado para o bot
                - O painel principal vem do heartbeat/runtime persistido
                - Se o stream cair, o bot ainda pode aparecer online pelo banco
                """)
                
            # Área de logs WebSocket
            with st.expander("📋 Logs WebSocket", expanded=False):
                if 'ws_logs' not in st.session_state:
                    st.session_state.ws_logs = []
                    
                if st.session_state.ws_logs:
                    for log in st.session_state.ws_logs[-10:]:  # Últimos 10 logs
                        st.text(log)
                else:
                    st.text("Nenhum log disponível")
                    
        else:
            st.error("❌ **WebSocket não disponível** - Módulo não carregado")
            st.info("Verifique se todas as dependências estão instaladas")

    # Continuar com as abas existentes...

    # Set default tab to Backtesting if requested
    if 'default_tab' not in st.session_state:
        st.session_state.default_tab = 'backtest'

    if active_market_view == "futures":
        st.subheader("🚀 Trading de Mercado Futuro")
        st.markdown("**Trade com alavancagem, posições long/short e gerenciamento avançado de risco**")
        st.info(
            "Escopo desta aba: análise operacional em tempo real (preço, contexto, sinais e risco). "
            "Não usa curva histórica de backtest para decisão."
        )

        # Warning banner
        st.warning("⚠️ **ATENÇÃO:** Mercado futuro envolve alto risco. Nunca arrisque mais do que pode perder!")

        # Configurações específicas de futuros na sidebar expandida
        st.sidebar.markdown("---")
        st.sidebar.subheader("🚀 Configurações Futuros")

        futures_leverage = st.sidebar.selectbox(
            "Alavancagem",
            [1, 2, 3, 5, 10, 20, 25, 50],
            index=3,
            help="Multiplicador de posição"
        )

        futures_mode = st.sidebar.selectbox(
            "Modo de Trading",
            ["Cross Margin", "Isolated Margin"],
            help="Cross: usa todo saldo | Isolated: limita risco por posição"
        )

        risk_level = st.sidebar.selectbox(
            "Nível de Risco",
            ["Conservador", "Moderado", "Agressivo"],
            index=1
        )

        # Tabs dentro da análise de futuros
        futures_tab1, futures_tab2, futures_tab3 = st.tabs([
            "🎯 Sinais & Análise", "⚖️ Calculadoras", "📊 Cenários Teóricos"
        ])

    # Tab 1: Análise e Sinais para Futuros
        with futures_tab1:
            st.markdown("### 🎯 Análise Técnica para Futuros")

            # Multi-Symbol Overview (if enabled) - with caching and performance optimization
            if enable_multi_symbol and len(selected_symbols) > 1:
                st.subheader("🔀 Overview - Múltiplos Pares")

            # Initialize multi-symbol last signals tracking
            if 'multi_symbol_signals' not in st.session_state:
                st.session_state.multi_symbol_signals = {}

            # Create overview table for all selected symbols
            overview_data = []
            current_time = now_brazil()
            runtime_bot = get_session_trading_bot_safe(selected_exchange)
            if runtime_bot is None:
                st.warning("⚠️ Runtime de mercado indisponível para análise multi-símbolo neste momento.")

            for sym in selected_symbols:
                # Initialize variables at the start of each iteration
                analytical_signal = "NEUTRO"
                last_candle = None
                sym_data = None
                signal_pipeline = None
                operational_state = None
                data_last_update = None

                try:
                    if runtime_bot is None:
                        continue
                    symbol_strategy_settings = get_effective_strategy_settings(
                        sym,
                        timeframe,
                        require_volume=require_volume,
                        require_trend=require_trend,
                        avoid_ranging=avoid_ranging,
                    )

                    # Check if we have cached data for this symbol that's less than 60 seconds old
                    cache_key = f"{sym}_{timeframe}_{symbol_strategy_settings['strategy_version']}"
                    should_refresh = True
                    cached_data = None
                    cache_age = 0

                    if cache_key in st.session_state.multi_symbol_data:
                        cached_data = st.session_state.multi_symbol_data[cache_key]
                        cache_age = _compute_data_age_seconds(
                            cached_data.get('last_update'),
                            now_reference=current_time,
                        )
                        if cache_age is None:
                            cache_age = float("inf")
                        # Cache mais agressivo para reduzir API calls
                        cache_timeout = 30 if len(selected_symbols) > 5 else 60
                        if cached_data['last_update'] and cache_age < cache_timeout:
                            should_refresh = False
                            sym_data = cached_data['data']
                            analytical_signal = cached_data.get('analytical_signal', "NEUTRO")
                            last_candle = cached_data['last_candle']
                            signal_pipeline = cached_data.get('signal_pipeline')
                            operational_state = cached_data.get('operational_state')
                            data_last_update = cached_data.get('market_timestamp') or cached_data.get('last_update')

                    if should_refresh:
                        # Mostrar progresso para símbolos múltiplos
                        with st.spinner(f'📡 Atualizando {sym}...'):
                            try:
                                # Use shared trading bot instance
                                runtime_bot.update_config(
                                    symbol=sym,
                                    timeframe=timeframe,
                                    rsi_period=symbol_strategy_settings["rsi_period"],
                                    rsi_min=symbol_strategy_settings["rsi_min"],
                                    rsi_max=symbol_strategy_settings["rsi_max"],
                                )
                                sym_data = runtime_bot.get_market_data(limit=200)

                                if sym_data is not None and not sym_data.empty:
                                    fetch_timestamp = current_time
                                    market_timestamp_reference = _extract_market_timestamp_reference(sym_data)
                                    last_candle = sym_data.iloc[-1]
                                    signal_pipeline = runtime_bot.evaluate_signal_pipeline(
                                        sym_data,
                                        min_confidence=min_confidence,
                                        timeframe=timeframe,
                                        require_volume=symbol_strategy_settings["require_volume"],
                                        require_trend=symbol_strategy_settings["require_trend"],
                                        avoid_ranging=symbol_strategy_settings.get("avoid_ranging", avoid_ranging),
                                        day_trading_mode=day_trading_mode,
                                        context_timeframe=symbol_strategy_settings.get("context_timeframe"),
                                        stop_loss_pct=symbol_strategy_settings.get("stop_loss_pct"),
                                        take_profit_pct=symbol_strategy_settings.get("take_profit_pct"),
                                        allowed_execution_setups=symbol_strategy_settings.get("allowed_execution_setups"),
                                    )
                                    analytical_signal = signal_pipeline["analytical_signal"]
                                    data_last_update = market_timestamp_reference or fetch_timestamp

                                    # Cache the data com timestamp
                                    st.session_state.multi_symbol_data[cache_key] = {
                                        'data': sym_data,
                                        'analytical_signal': analytical_signal,
                                        'last_candle': last_candle,
                                        'last_update': fetch_timestamp,
                                        'market_timestamp': market_timestamp_reference,
                                        'signal_pipeline': signal_pipeline,
                                        'operational_state': operational_state,
                                    }
                                else:
                                    continue
                            except Exception as e:
                                st.warning(f"⚠️ Erro ao atualizar {sym}: {str(e)}")
                                continue

                    # Skip if we don't have valid data
                    if last_candle is None:
                        continue

                    is_data_fresh, data_age_seconds = _is_data_fresh(
                        data_last_update,
                        max_age_seconds=MAX_SIGNAL_DATA_AGE_SECONDS,
                        now_reference=current_time,
                    )
                    if not is_data_fresh:
                        operational_state = _build_stale_data_operational_state(
                            data_age_seconds,
                            max_age_seconds=MAX_SIGNAL_DATA_AGE_SECONDS,
                        )
                    elif operational_state is None:
                        operational_state = build_operational_signal_state(
                            analytical_signal,
                            float(last_candle['close']),
                            symbol_strategy_settings,
                            regime_evaluation=(signal_pipeline or {}).get("regime_evaluation"),
                        )

                    if cache_key in st.session_state.multi_symbol_data:
                        st.session_state.multi_symbol_data[cache_key]['operational_state'] = operational_state

                    candidate_signal = (signal_pipeline or {}).get("candidate_signal", "NEUTRO")
                    approved_signal = (signal_pipeline or {}).get("approved_signal")
                    blocked_signal = (signal_pipeline or {}).get("blocked_signal")
                    analytical_block_reason = (signal_pipeline or {}).get("block_reason")
                    operational_signal = (operational_state or {}).get("final_signal", "NEUTRO")

                    # Check for new signals to send alerts
                    if operational_signal not in ["NEUTRO"] and st.session_state.telegram_notifications:
                        telegram_bot = get_or_init_session_telegram_bot()
                        if telegram_bot.is_configured():

                            last_signal_key = f"{sym}_last_signal"
                            if (last_signal_key not in st.session_state.multi_symbol_signals or
                                st.session_state.multi_symbol_signals[last_signal_key]['signal'] != operational_signal or
                                (current_time - st.session_state.multi_symbol_signals[last_signal_key]['timestamp']).total_seconds() > 300):

                                try:
                                    run_async_task_sync(
                                        telegram_bot.send_signal_alert(
                                            symbol=sym,
                                            signal=operational_signal,
                                            price=last_candle['close'],
                                            rsi=last_candle['rsi'],
                                            macd=last_candle['macd'],
                                            macd_signal=last_candle['macd_signal']
                                        )
                                    )
                                    st.session_state.multi_symbol_signals[last_signal_key] = {
                                        'signal': operational_signal,
                                        'timestamp': current_time
                                    }
                                except Exception:
                                    logger.debug("Falha ao enviar alerta multi-simbolo para %s.", sym, exc_info=True)

                    history_signature = (
                        candidate_signal,
                        approved_signal or "NEUTRO",
                        blocked_signal or "-",
                        operational_signal,
                        analytical_block_reason or "-",
                        (operational_state or {}).get("block_reason") or "-",
                    )
                    should_record_history = (
                        candidate_signal in ACTIONABLE_SIGNALS
                        or approved_signal in ACTIONABLE_SIGNALS
                        or blocked_signal in ACTIONABLE_SIGNALS
                    )
                    if should_record_history:
                        previous_entry = st.session_state.signals_history[-1] if st.session_state.signals_history else None
                        previous_signature = None
                        if previous_entry and previous_entry.get('symbol') == sym and previous_entry.get('timeframe') == timeframe:
                            previous_signature = (
                                previous_entry.get('candidate_signal', 'NEUTRO'),
                                previous_entry.get('approved_signal') or "NEUTRO",
                                previous_entry.get('blocked_signal') or "-",
                                previous_entry.get('operational_signal', previous_entry.get('signal', 'NEUTRO')),
                                previous_entry.get('block_reason') or "-",
                                previous_entry.get('operational_block_reason') or "-",
                            )
                        if (
                            previous_signature != history_signature
                            or not previous_entry
                            or _compare_timestamps(previous_entry['timestamp'], current_time - timedelta(minutes=5))
                        ):
                            st.session_state.signals_history.append({
                                'timestamp': current_time,
                                'symbol': sym,
                                'timeframe': timeframe,
                                'price': last_candle['close'],
                                'rsi': last_candle['rsi'],
                                'macd': last_candle['macd'],
                                'macd_signal': last_candle['macd_signal'],
                                'signal': operational_signal,
                                'candidate_signal': candidate_signal,
                                'approved_signal': approved_signal,
                                'blocked_signal': blocked_signal,
                                'block_reason': analytical_block_reason,
                                'block_source': (signal_pipeline or {}).get("block_source"),
                                'operational_signal': operational_signal,
                                'operational_block_reason': (operational_state or {}).get("block_reason"),
                            })

                    # Only add to overview if we have valid data
                    if last_candle is not None:
                        overview_data.append({
                            'Par': sym,
                            'Preço': f"${last_candle['close']:.6f}",
                            'RSI': f"{last_candle['rsi']:.2f}",
                            'MACD': f"{last_candle['macd']:.4f}",
                            'Candidato': candidate_signal,
                            'Aprovado': approved_signal or "NEUTRO",
                            'Bloqueado': blocked_signal or "-",
                            'Motivo Bloqueio': analytical_block_reason or "-",
                            'Sinal Operacional': operational_signal,
                            'Long Score': 'N/A',
                            'Short Score': 'N/A',
                            'Variação': f"{((last_candle['close'] - last_candle['open']) / last_candle['open'] * 100):.2f}%"
                        })

                except Exception as e:
                    overview_data.append({
                        'Par': sym,
                        'Preço': 'Erro',
                        'RSI': 'N/A',
                        'MACD': 'N/A', 
                        'Candidato': 'ERRO',
                        'Aprovado': 'ERRO',
                        'Bloqueado': '-',
                        'Motivo Bloqueio': str(e),
                        'Sinal Operacional': 'ERRO',
                        'Long Score': 'N/A',
                        'Short Score': 'N/A',
                        'Variação': 'N/A'
                    })

            # Trim signals history to last 50 across all symbols
            if len(st.session_state.signals_history) > 50:
                st.session_state.signals_history = st.session_state.signals_history[-50:]

            if overview_data:
                overview_df = pd.DataFrame(overview_data)

                # Style the dataframe
                def style_futures_signals(val):
                    if isinstance(val, str):
                        if val == 'COMPRA':
                            return 'background-color: #90EE90'
                        elif val == 'VENDA':
                            return 'background-color: #FFB6C1'
                    elif isinstance(val, (int, float)):
                        if val >= 70:
                            return 'background-color: #90EE90'
                        elif val >= 50:
                            return 'background-color: #FFFF99'
                        elif val <= 30:
                            return 'background-color: #FFB6C1'
                    return ''

                styled_df = overview_df.style.map(style_futures_signals)
                st.dataframe(styled_df, width='stretch', hide_index=True)

            st.markdown("---")

            # Usar símbolo configurado centralmente
            futures_symbol = symbol  # Usar o símbolo já configurado na sidebar
            
            st.subheader(f"📈 Análise Detalhada de Futuros - {futures_symbol}")
            st.success(f"✅ **Configuração Ativa:** {futures_symbol} | {timeframe} | RSI({rsi_period}) {rsi_min}-{rsi_max}")
            st.info("💡 *Configurações centralizadas na barra lateral* ⬅️")

    # Helper function para calcular scores de futuros
    def _calculate_futures_score(last_candle, position_type):
        """Calcular score específico para posições LONG/SHORT em futuros"""
        try:
            score = 0

            # RSI scoring
            rsi = last_candle.get('rsi', 50)
            if position_type == 'LONG':
                if rsi < 30: score += 30
                elif rsi < 40: score += 20
                elif rsi > 70: score -= 20
            else:  # SHORT
                if rsi > 70: score += 30
                elif rsi > 60: score += 20
                elif rsi < 30: score -= 20

            # MACD scoring
            macd = last_candle.get('macd', 0)
            macd_signal = last_candle.get('macd_signal', 0)

            if position_type == 'LONG':
                if macd > macd_signal: score += 25
                if last_candle.get('macd_histogram', 0) > 0: score += 15
            else:  # SHORT
                if macd < macd_signal: score += 25
                if last_candle.get('macd_histogram', 0) < 0: score += 15

            # Volume scoring
            volume_ratio = last_candle.get('volume_ratio', 1)
            if volume_ratio > 1.5: score += 15
            elif volume_ratio > 1.2: score += 10

            # Trend scoring (simplified)
            sma_21 = last_candle.get('sma_21', last_candle['close'])
            if position_type == 'LONG':
                if last_candle['close'] > sma_21: score += 15
            else:  # SHORT
                if last_candle['close'] < sma_21: score += 15

            return min(max(score, 0), 100)  # Normalize to 0-100

        except Exception:
            return 0

    if show_live_sidebar_controls:
        st.caption("Notificações Telegram foram movidas para a aba `Bot Trader`, junto com o runtime do bot.")

    # Status indicators for main symbol - renderizar apenas na visao de operacao de mercado
    futures_tab1 = futures_tab2 = futures_tab3 = None
    if active_market_view == "futures":
        status_container = st.container()
        with status_container:
            col1, col2, col3, col4, col5 = st.columns(5)
    else:
        col1 = col2 = col3 = col4 = col5 = None

    # Check if we need to update data
    should_update = (
        st.session_state.last_update is None or 
        (get_brazil_datetime_naive() - st.session_state.last_update).total_seconds() > 60
    )
    runtime_bot = get_session_trading_bot_safe(selected_exchange)

    if active_market_view == "futures" and should_update and runtime_bot is not None:
        try:
            with st.spinner('Carregando dados...'):
                data = runtime_bot.get_market_data()
                if data is not None:
                    market_timestamp = _extract_market_timestamp_reference(data)
                    st.session_state.current_data = data
                    st.session_state.last_update = get_brazil_datetime_naive()
                    st.session_state.last_market_timestamp = market_timestamp
        except Exception as e:
            error_text = str(e or "")
            if "451" in error_text and "restricted location" in error_text.lower():
                st.warning(
                    "⚠️ Ambiente do Railway bloqueado por região para alguns endpoints da Binance. "
                    "A dashboard pode exibir dados limitados, enquanto o bot continua em TESTNET."
                )
                logger.warning("Falha de georrestrição Binance no carregamento inicial da dashboard: %s", error_text)
            else:
                st.error(f"Erro ao carregar dados: {error_text}")
    elif active_market_view == "futures" and should_update and runtime_bot is None:
        st.info("ℹ️ Runtime de mercado ainda inicializando. Aguarde alguns segundos.")

    # Store multi-symbol data (already initialized above)

    if active_market_view == "futures" and st.session_state.current_data is not None and runtime_bot is not None:
        data = st.session_state.current_data
        last_candle = data.iloc[-1]
        market_timestamp_reference = (
            st.session_state.last_market_timestamp
            or _extract_market_timestamp_reference(data)
        )
        st.session_state.last_market_timestamp = market_timestamp_reference
        data_is_fresh, data_age_seconds = _is_data_fresh(
            market_timestamp_reference or st.session_state.last_update,
            max_age_seconds=MAX_SIGNAL_DATA_AGE_SECONDS,
        )
        guardrail_edge_summary = None
        risk_plan = None
        live_strategy_settings = get_effective_strategy_settings(
            symbol,
            timeframe,
            require_volume=require_volume,
            require_trend=require_trend,
            avoid_ranging=avoid_ranging,
        )
        runtime_strategy_version = live_strategy_settings["strategy_version"]
        guardrail_edge_summary = None
        context_evaluation = None
        regime_evaluation = None
        structure_evaluation = None
        confirmation_evaluation = None
        entry_quality_evaluation = None
        hard_block_evaluation = None
        signal_pipeline = runtime_bot.evaluate_signal_pipeline(
            data,
            min_confidence=min_confidence,
            timeframe=timeframe,
            require_volume=live_strategy_settings["require_volume"],
            require_trend=live_strategy_settings["require_trend"],
            avoid_ranging=live_strategy_settings.get("avoid_ranging", avoid_ranging),
            day_trading_mode=day_trading_mode,
            context_timeframe=live_strategy_settings.get("context_timeframe"),
            stop_loss_pct=live_strategy_settings.get("stop_loss_pct"),
            take_profit_pct=live_strategy_settings.get("take_profit_pct"),
            allowed_execution_setups=live_strategy_settings.get("allowed_execution_setups"),
        )
        candidate_signal = signal_pipeline["candidate_signal"]
        analytical_signal = signal_pipeline["analytical_signal"]
        approved_signal = signal_pipeline.get("approved_signal")
        blocked_signal = signal_pipeline.get("blocked_signal")
        analytical_block_reason = signal_pipeline.get("block_reason")
        context_evaluation = signal_pipeline.get("context_evaluation")
        regime_evaluation = signal_pipeline.get("regime_evaluation")
        structure_evaluation = signal_pipeline.get("structure_evaluation")
        confirmation_evaluation = signal_pipeline.get("confirmation_evaluation")
        entry_quality_evaluation = signal_pipeline.get("entry_quality_evaluation")
        scenario_evaluation = signal_pipeline.get("scenario_evaluation")
        trade_decision = signal_pipeline.get("trade_decision")
        hard_block_evaluation = signal_pipeline.get("hard_block_evaluation")

        if data_is_fresh:
            operational_state = build_operational_signal_state(
                analytical_signal,
                float(last_candle['close']),
                live_strategy_settings,
                regime_evaluation=regime_evaluation,
            )
        else:
            operational_state = _build_stale_data_operational_state(
                data_age_seconds,
                max_age_seconds=MAX_SIGNAL_DATA_AGE_SECONDS,
            )

        signal = operational_state["final_signal"]
        guardrail_edge_summary = operational_state["edge_summary"]
        risk_plan = operational_state["risk_plan"]
        governance_summary = operational_state.get("governance_summary")
        operational_block_reason = operational_state["block_reason"]
        operational_block_source = operational_state["block_source"]
        risk_guardrail_blocked = bool(risk_plan and not risk_plan.get("allowed"))
        entry_reason = analytical_signal
        if analytical_signal != "NEUTRO":
            reason_parts = [analytical_signal]
            if context_evaluation:
                reason_parts.append(
                    f"ctx:{context_evaluation.get('market_bias', 'neutral')}/{context_evaluation.get('regime', '-')}"
                )
            if regime_evaluation:
                reason_parts.append(
                    f"regime:{regime_evaluation.get('regime', '-')}/{regime_evaluation.get('volatility_state', '-')}"
                )
            if structure_evaluation:
                reason_parts.append(
                    f"struct:{structure_evaluation.get('structure_state', '-')}/{structure_evaluation.get('price_location', '-')}"
                )
            if confirmation_evaluation:
                reason_parts.append(
                    f"confirm:{confirmation_evaluation.get('confirmation_state', '-')}/{confirmation_evaluation.get('confirmation_score', 0):.1f}"
                )
            if entry_quality_evaluation:
                reason_parts.append(
                    f"entry:{entry_quality_evaluation.get('market_pattern') or entry_quality_evaluation.get('setup_type') or '-'}"
                    f"/{entry_quality_evaluation.get('entry_quality', '-')}"
                    f"/s{float(entry_quality_evaluation.get('entry_score', 0) or 0):.1f}"
                )
            if scenario_evaluation:
                reason_parts.append(
                    f"scenario:{scenario_evaluation.get('scenario_grade', '-')}/{scenario_evaluation.get('scenario_score', 0):.2f}"
                )
            entry_reason = " | ".join(reason_parts)

        try:
            get_paper_trade_service().evaluate_open_trades(symbol=symbol, timeframe=timeframe, market_data=data)
        except Exception as e:
            logger.warning("Falha ao avaliar paper trades do dashboard: %s", e)

        # Store data for multi-symbol monitoring
        st.session_state.multi_symbol_data[symbol] = {
            'data': data,
            'analytical_signal': analytical_signal,
            'last_candle': last_candle,
            'last_update': st.session_state.last_update,
            'market_timestamp': market_timestamp_reference,
            'edge_summary': guardrail_edge_summary,
            'risk_plan': risk_plan,
            'governance_summary': governance_summary,
            'signal_pipeline': signal_pipeline,
            'operational_state': operational_state,
            'context_evaluation': context_evaluation,
            'regime_evaluation': regime_evaluation,
            'structure_evaluation': structure_evaluation,
            'confirmation_evaluation': confirmation_evaluation,
            'entry_quality_evaluation': entry_quality_evaluation,
            'hard_block_evaluation': hard_block_evaluation,
        }

        candle_timestamp_value = last_candle.name if hasattr(last_candle, "name") else None
        candle_timestamp_iso = (
            candle_timestamp_value.isoformat()
            if hasattr(candle_timestamp_value, "isoformat")
            else (str(candle_timestamp_value) if candle_timestamp_value is not None else None)
        )

        history_signature = (
            candidate_signal,
            approved_signal or "NEUTRO",
            blocked_signal or "-",
            signal,
            analytical_block_reason or "-",
            operational_block_reason or "-",
            candle_timestamp_iso or "-",
        )
        previous_entry = st.session_state.signals_history[-1] if st.session_state.signals_history else None
        previous_signature = None
        if previous_entry and previous_entry.get('symbol') == symbol and previous_entry.get('timeframe') == timeframe:
            previous_signature = (
                previous_entry.get('candidate_signal', 'NEUTRO'),
                previous_entry.get('approved_signal') or "NEUTRO",
                previous_entry.get('blocked_signal') or "-",
                previous_entry.get('operational_signal', previous_entry.get('signal', 'NEUTRO')),
                previous_entry.get('block_reason') or "-",
                previous_entry.get('operational_block_reason') or "-",
                previous_entry.get('candle_timestamp') or "-",
            )

        # Add signal to history if it's a new analytical event
        if (
            candidate_signal in ACTIONABLE_SIGNALS
            or approved_signal in ACTIONABLE_SIGNALS
            or blocked_signal in ACTIONABLE_SIGNALS
        ) and (
            previous_signature != history_signature or
            not previous_entry or
            _compare_timestamps(st.session_state.signals_history[-1]['timestamp'], get_brazil_datetime_naive() - timedelta(minutes=5))
        ):
            # Send Telegram notification if enabled
            if st.session_state.telegram_notifications:
                telegram_bot = get_or_init_session_telegram_bot()
                if telegram_bot.is_configured():
                    try:
                        run_async_task_sync(
                            telegram_bot.send_signal_alert(
                                symbol=symbol,
                                signal=signal,
                                price=last_candle['close'],
                                rsi=last_candle['rsi'],
                                macd=last_candle['macd'],
                                macd_signal=last_candle['macd_signal']
                            )
                        )
                    except Exception as e:
                        st.sidebar.warning(f"⚠️ Erro ao enviar alerta: {str(e)}")

            # Criar dados do sinal para salvar
            signal_data = {
                'timestamp': get_brazil_datetime_naive(),
                'candle_timestamp': candle_timestamp_iso,
                'symbol': symbol,
                'timeframe': timeframe,
                'price': last_candle['close'],
                'rsi': last_candle['rsi'],
                'macd': last_candle['macd'],
                'macd_signal': last_candle['macd_signal'],
                'signal': signal,
                'candidate_signal': candidate_signal,
                'approved_signal': approved_signal,
                'blocked_signal': blocked_signal,
                'block_reason': analytical_block_reason,
                'block_source': (hard_block_evaluation or {}).get("block_source"),
                'operational_signal': signal,
                'operational_block_reason': operational_block_reason,
                'context_timeframe': live_strategy_settings.get("context_timeframe"),
                'strategy_version': runtime_strategy_version,
                'regime': (regime_evaluation or {}).get("regime"),
                'macd_value': last_candle['macd'],
                'signal_strength': abs(last_candle['rsi'] - 50) / 50,  # Força do sinal baseada no RSI
                'volume': last_candle.get('volume', 0)
            }

            # Salvar no banco de dados
            try:
                db.save_trading_signal(signal_data)
            except Exception as e:
                st.error(f"Erro ao salvar sinal no banco: {str(e)}")

            try:
                if risk_plan and risk_plan.get("allowed"):
                    fallback_signal_score = last_candle.get("signal_confidence")
                    if fallback_signal_score is None or pd.isna(fallback_signal_score):
                        fallback_signal_score = runtime_bot.get_signal_with_confidence(data).get(
                            "confidence",
                            0.0,
                        )
                    get_paper_trade_service().register_signal(
                        symbol=symbol,
                        timeframe=timeframe,
                        signal=signal,
                        entry_price=float(last_candle['close']),
                        entry_timestamp=signal_data['candle_timestamp'] or signal_data['timestamp'],
                        context_timeframe=live_strategy_settings.get("context_timeframe"),
                        source="dashboard",
                        strategy_version=runtime_strategy_version,
                        stop_loss_pct=live_strategy_settings.get("stop_loss_pct"),
                        take_profit_pct=live_strategy_settings.get("take_profit_pct"),
                        risk_plan=risk_plan,
                        setup_name=(
                            (entry_quality_evaluation or {}).get("market_pattern")
                            or (entry_quality_evaluation or {}).get("setup_type")
                            or runtime_strategy_version
                        ),
                        regime=(regime_evaluation or {}).get("regime") or last_candle.get("market_regime"),
                        signal_score=(entry_quality_evaluation or {}).get("entry_score", fallback_signal_score),
                        atr=last_candle.get("atr", 0.0),
                        entry_reason=entry_reason,
                        entry_quality=(entry_quality_evaluation or {}).get("entry_quality"),
                        rejection_reason=(entry_quality_evaluation or {}).get("rejection_reason"),
                        sample_type="paper",
                    )
            except Exception as e:
                logger.warning("Falha ao registrar paper trade do dashboard: %s", e)

            # Manter no histórico da sessão também
            st.session_state.signals_history.append(signal_data)

            # Keep only last 50 signals
            if len(st.session_state.signals_history) > 50:
                st.session_state.signals_history = st.session_state.signals_history[-50:]

        # Display current metrics - com containers para atualização suave
        with col1:
            price_container = st.empty()
            with price_container.container():
                st.metric(
                    label="💰 Preço Atual",
                    value=f"${last_candle['close']:.6f}",
                    delta=f"{((last_candle['close'] - last_candle['open']) / last_candle['open'] * 100):.2f}%"
                )

        with col2:
            rsi_color = "normal"
            if last_candle['rsi'] > rsi_max:
                rsi_color = "inverse"
            elif last_candle['rsi'] < rsi_min:
                rsi_color = "inverse"

            st.metric(
                label="📊 RSI",
                value=f"{last_candle['rsi']:.2f}",
                delta=None
            )

        with col3:
            signal_emoji = {
                "COMPRA": "🟢", "VENDA": "🔴", "NEUTRO": "⚪"
            }
            st.metric(
                label="🚨 Sinal Operacional",
                value=f"{signal_emoji.get(signal, '⚪')} {signal.replace('_', ' ')}",
                delta=None
            )

        with col4:
            if not pd.isna(last_candle['macd']) and not pd.isna(last_candle['macd_signal']):
                macd_trend = "📈" if last_candle['macd'] > last_candle['macd_signal'] else "📉"
                st.metric(
                    label="📊 MACD",
                    value=f"{macd_trend} {last_candle['macd']:.4f}",
                    delta=f"Signal: {last_candle['macd_signal']:.4f}"
                )
            else:
                st.metric(
                    label="📊 MACD",
                    value="Calculando...",
                    delta=None
                )

        with col5:
            # Status dinâmico com indicador de cache otimizado
            current_time_now = get_brazil_datetime_naive()
            if st.session_state.last_update:
                seconds_since_update = (current_time_now - st.session_state.last_update).total_seconds()
                
                if seconds_since_update < 60:
                    status_color = "🟢"
                    status_text = "Cache Ativo"
                    delta_text = f"Há {int(seconds_since_update)}s"
                elif seconds_since_update < 90:
                    status_color = "🟡"
                    status_text = "Aguardando"
                    delta_text = f"Há {int(seconds_since_update)}s"
                else:
                    status_color = "🔵"
                    status_text = "Atualizando"
                    delta_text = "Em breve..."
            else:
                status_color = "⚪"
                status_text = "Iniciando"
                delta_text = "..."
            
            st.metric(
                label="📡 Status",
                value=f"{status_color} {status_text}",
                delta=delta_text
            )

        market_operations_view = st.radio(
            "Painel Operacional",
            options=["Resumo", "Historico de Sinais"],
            horizontal=True,
            key="market_operations_view_mode",
            help="Resumo para leitura atual do trade. Historico para auditoria dos sinais gerados.",
        )
        st.caption("O grafico de mercado fica como auxiliar em Bot em Operacao. Esta visao foca decisao, risco e auditoria.")

        if market_operations_view == "Resumo":
            render_market_operational_summary(
                symbol=symbol,
                timeframe=timeframe,
                rsi_period=runtime_bot.rsi_period,
                rsi_min=rsi_min,
                rsi_max=rsi_max,
                last_candle=last_candle,
                candidate_signal=candidate_signal,
                approved_signal=approved_signal,
                blocked_signal=blocked_signal,
                analytical_block_reason=analytical_block_reason,
                signal=signal,
                operational_state=operational_state,
                operational_block_reason=operational_block_reason,
                operational_block_source=operational_block_source,
                data_age_seconds=data_age_seconds,
                risk_plan=risk_plan,
                guardrail_edge_summary=guardrail_edge_summary,
                governance_summary=governance_summary,
                context_evaluation=context_evaluation,
                regime_evaluation=regime_evaluation,
                structure_evaluation=structure_evaluation,
                confirmation_evaluation=confirmation_evaluation,
                entry_quality_evaluation=entry_quality_evaluation,
                scenario_evaluation=scenario_evaluation,
                trade_decision=trade_decision,
                hard_block_evaluation=hard_block_evaluation,
            )
        else:
            render_market_signal_history(
                symbol=symbol,
                timeframe=timeframe,
                require_volume=require_volume,
                require_trend=require_trend,
            )
    elif active_market_view == "futures" and st.session_state.current_data is not None and runtime_bot is None:
        st.warning("⚠️ Dados em cache existem, mas o runtime de mercado está indisponível para processar sinais.")

    if active_market_view == "futures" and futures_tab2 is not None and futures_tab3 is not None:
        # Tab 2: Calculadoras
        with futures_tab2:
            st.markdown("### ⚖️ Calculadoras de Trading")

            calc_tab1, calc_tab2, calc_tab3 = st.tabs([
                "🧮 Calculadora de Posição", "💀 Preço de Liquidação", "💰 P&L Simulador"
            ])

            with calc_tab1:
                st.markdown("#### 🧮 Calculadora de Tamanho da Posição")

                col1, col2 = st.columns(2)

                with col1:
                    account_balance = st.number_input("Saldo da Conta ($)", value=10000.0, min_value=100.0)
                    risk_percent = st.slider("Risco por Trade (%)", 1, 10, 3)
                    leverage_calc = st.selectbox("Alavancagem Calc", [1, 2, 3, 5, 10, 20, 25, 50], index=3)
                    entry_price = st.number_input(
                        "Preço de Entrada ($)",
                        value=float(st.session_state.current_data.iloc[-1]['close']) if st.session_state.current_data is not None else 1.0
                    )

                with col2:
                    risk_amount = account_balance * (risk_percent / 100)
                    position_size_usdt = risk_amount * leverage_calc
                    quantity = position_size_usdt / entry_price
                    margin_required = position_size_usdt / leverage_calc

                    st.metric("💰 Valor Arriscado", f"${risk_amount:.2f}")
                    st.metric("📊 Tamanho da Posição", f"${position_size_usdt:.2f}")
                    st.metric("🪙 Quantidade", f"{quantity:.6f}")
                    st.metric("🏦 Margem Necessária", f"${margin_required:.2f}")

            with calc_tab2:
                st.markdown("#### 💀 Calculadora de Preço de Liquidação")

                col1, col2 = st.columns(2)

                with col1:
                    entry_price_liq = st.number_input("Preço de Entrada Liq", value=1.0)
                    leverage_liq = st.selectbox("Alavancagem Liq", [1, 2, 3, 5, 10, 20, 25, 50], index=3)
                    position_side = st.radio("Lado da Posição", ["LONG", "SHORT"])

                with col2:
                    if position_side == "LONG":
                        liquidation_price = entry_price_liq * (1 - (0.9 / leverage_liq))
                        distance = ((entry_price_liq - liquidation_price) / entry_price_liq) * 100
                    else:
                        liquidation_price = entry_price_liq * (1 + (0.9 / leverage_liq))
                        distance = ((liquidation_price - entry_price_liq) / entry_price_liq) * 100

                    st.metric("💀 Preço de Liquidação", f"${liquidation_price:.6f}")
                    st.metric("📏 Distância", f"{distance:.2f}%")

                    if distance < 5:
                        st.error("⚠️ ALTO RISCO DE LIQUIDAÇÃO!")
                    elif distance < 10:
                        st.warning("⚠️ Risco moderado de liquidação")
                    else:
                        st.success("✅ Distância segura da liquidação")

            with calc_tab3:
                st.markdown("#### 💰 Simulador de Profit & Loss")

                col1, col2 = st.columns(2)

                with col1:
                    entry_price_pnl = st.number_input("Preço de Entrada PnL", value=1.0)
                    position_size_pnl = st.number_input("Tamanho da Posição ($)", value=1000.0)
                    leverage_pnl = st.selectbox("Alavancagem PnL", [1, 2, 3, 5, 10, 20, 25, 50], index=3)

                    st.markdown("**Cenários de Preço:**")
                    scenario_1 = st.number_input("Cenário 1 ($)", value=entry_price_pnl * 1.02)
                    scenario_2 = st.number_input("Cenário 2 ($)", value=entry_price_pnl * 1.05)
                    scenario_3 = st.number_input("Cenário 3 ($)", value=entry_price_pnl * 0.98)

                with col2:
                    st.markdown("**Resultados:**")
                    for i, price in enumerate([scenario_1, scenario_2, scenario_3], 1):
                        price_change_pct = ((price - entry_price_pnl) / entry_price_pnl)
                        pnl = position_size_pnl * price_change_pct * leverage_pnl
                        color = "🟢" if pnl > 0 else "🔴"
                        st.write(f"**Cenário {i}:** {color} ${pnl:+.2f} ({price_change_pct * leverage_pnl * 100:+.1f}%)")

        # Tab 3: Cenários teóricos
        with futures_tab3:
            st.markdown("### 📊 Simulador Educacional de Cenários")

            mock_positions = [
                {
                    "Par": symbol,
                    "Lado": "LONG",
                    "Tamanho": f"${5000 * futures_leverage:.0f}",
                    "Alavancagem": f"{futures_leverage}x",
                    "Entrada": f"${st.session_state.current_data.iloc[-1]['close']:.6f}" if st.session_state.current_data is not None else "$1.000000",
                    "Atual": f"${st.session_state.current_data.iloc[-1]['close'] * 1.015:.6f}" if st.session_state.current_data is not None else "$1.015000",
                    "PnL": f"+${5000 * futures_leverage * 0.015:.2f}",
                    "PnL %": f"+{futures_leverage * 1.5:.1f}%",
                    "Margem": f"${5000:.0f}",
                    "Liquidação": f"${st.session_state.current_data.iloc[-1]['close'] * (1 - 0.9/futures_leverage):.6f}" if st.session_state.current_data is not None else "$0.900000"
                }
            ]

            if st.button("🔄 Gerar Cenário Teórico"):
                positions_df = pd.DataFrame(mock_positions)
                st.dataframe(positions_df, width="stretch")

                profit = 5000 * futures_leverage * 0.015
                profit_pct = futures_leverage * 1.5
                st.success(f"💰 PnL Total Simulado: +${profit:.2f} (+{profit_pct:.1f}%)")
                st.info(f"🏦 Margem Total Usada: $5,000 com {futures_mode}")
                st.warning("⚠️ Isto não representa posição real aberta nem paper trade salvo")
            else:
                st.info("📭 Clique para gerar um cenário teórico com base na configuração atual")

    # Auto-refresh mechanism otimizado - cache inteligente
    if auto_refresh and active_dashboard_section == "market":
        current_time_check = get_brazil_datetime_naive()
        cache_timeout = 90

        should_update_data = (
            st.session_state.last_update is None or
            (current_time_check - st.session_state.last_update).total_seconds() > cache_timeout
        )

        if should_update_data:
            with st.spinner('🔄 Atualizando dados do mercado...'):
                try:
                    runtime_bot = get_session_trading_bot_safe(selected_exchange)
                    if runtime_bot is None:
                        st.info("ℹ️ Runtime de mercado indisponível para atualização automática no momento.")
                    else:
                        new_data = runtime_bot.get_market_data()
                        if new_data is not None:
                            market_timestamp = _extract_market_timestamp_reference(new_data)
                            st.session_state.current_data = new_data
                            st.session_state.last_update = current_time_check
                            st.session_state.last_market_timestamp = market_timestamp
                            st.success("✅ Dados atualizados!")
                        else:
                            st.warning("⚠️ Não foi possível atualizar os dados")
                except Exception as e:
                    error_text = str(e or "")
                    if "451" in error_text and "restricted location" in error_text.lower():
                        st.warning(
                            "⚠️ Ambiente do Railway bloqueado por região para alguns endpoints da Binance. "
                            "O bot pode continuar em TESTNET enquanto a dashboard exibe dados limitados."
                        )
                        logger.warning("Falha de georrestrição Binance na atualização da dashboard: %s", error_text)
                    else:
                        st.error(f"❌ Erro na atualização: {error_text}")

    if active_dashboard_section == "bot":
        runtime_reference_settings = get_effective_strategy_settings(
            symbol,
            timeframe,
            require_volume=False,
            require_trend=False,
        )
        runtime_family_label = AppConfig.get_symbol_profile_family_label(symbol)
        bot_runtime_key = _resolve_account_runtime_key(bool(st.session_state.get("trader_bot_testnet", True)))
        bot_process_state = get_trader_bot_process_state(
            runtime_db_state=get_cached_bot_runtime_db_state(runtime_key=bot_runtime_key, limit=1),
            runtime_key=bot_runtime_key,
        )
        workspace_session_active = bool(dashboard_user)
        admin_session_active = bool(st.session_state.get("admin_authenticated"))
        operator_session_active = bool(workspace_session_active or admin_session_active)
        operator_session_label = "Workspace" if workspace_session_active else ("Admin" if admin_session_active else "OFF")
        telegram_library_ready = is_telegram_service_available()
        websocket_library_ready = is_websocket_runtime_available()
        session_notifications_enabled = bool(st.session_state.get("telegram_notifications"))
        subscription_payload = (dashboard_user or {}).get("subscription") or {}
        subscription_active = bool(subscription_payload.get("is_active"))
        subscription_plan = str(subscription_payload.get("plan_code") or "free").upper()
        subscription_gate_required = bool(ProductionConfig.REQUIRE_ACTIVE_SUBSCRIPTION_FOR_BOT)
        subscription_gate_satisfied = bool(admin_session_active or not subscription_gate_required or subscription_active)
        license_payload = (dashboard_user or {}).get("license") or {}
        license_gate_required = bool(getattr(ProductionConfig, "REQUIRE_DASHBOARD_DEVICE_LICENSE", True))
        license_gate_satisfied = bool(admin_session_active or not license_gate_required or license_payload.get("allowed"))
        bot_start_allowed = bool(
            operator_session_active
            and websocket_library_ready
            and subscription_gate_satisfied
            and license_gate_satisfied
        )
        bot_start_block_reason = ""
        if not operator_session_active:
            bot_start_block_reason = "Faça login no Workspace ou autentique o Admin para habilitar o runtime."
        elif not websocket_library_ready:
            bot_start_block_reason = "Biblioteca websockets ausente no ambiente. Instale as dependências antes de ligar o bot."
        elif subscription_gate_required and not subscription_gate_satisfied:
            bot_start_block_reason = (
                "Assinatura inativa/expirada. Ative um plano semanal, mensal ou anual para ligar o bot."
            )
        elif license_gate_required and not license_gate_satisfied:
            bot_start_block_reason = (
                "Licença de IP/dispositivo bloqueada. Peça ao admin para resetar a licença deste usuário."
            )

        render_section_hero(
            kicker="Bot Trader",
            title="Central operacional do runtime",
            subtitle=(
                "Ligue, pare e acompanhe o processo do bot com leitura rápida de status. "
                "Gráficos, streaming e contexto ficam concentrados na aba Mercado."
            ),
            badges=[
                _build_status_pill("Processo", "ON" if bot_process_state.get("running") else "OFF", "accent" if bot_process_state.get("running") else "danger"),
                _build_status_pill("Sessão", operator_session_label, "warm" if operator_session_active else "danger"),
                _build_status_pill("WebSocket", "OK" if websocket_library_ready else "Pendente", "accent" if websocket_library_ready else "danger"),
                _build_status_pill("Assinatura", subscription_plan if subscription_gate_satisfied else f"{subscription_plan} OFF", "accent" if subscription_gate_satisfied else "danger"),
                _build_status_pill("Licença", "OK" if license_gate_satisfied else "Bloqueada", "accent" if license_gate_satisfied else "danger"),
            ],
        )

        bot_view_mode = st.radio(
            "Visao do Bot",
            options=["Runtime", "Prontidao", "Guia"],
            horizontal=True,
            key="bot_view_mode",
            help="Runtime para operar o processo, Prontidao para conferir dependencias e Guia para o fluxo recomendado.",
        )

        context_col1, context_col2, context_col3, context_col4 = st.columns(4)
        with context_col1:
            st.metric("Referencia Analitica", symbol)
        with context_col2:
            st.metric("Timeframe", timeframe)
        with context_col3:
            st.metric("Familia", runtime_family_label)
        with context_col4:
            st.metric("Telegram", "ON" if st.session_state.get("telegram_notifications") else "OPCIONAL")

        render_dashboard_strip(
            "A configuração analítica abaixo serve como referência da sessão atual. Use Mercado para gráfico e sinal, e Bot Trader para o comando do runtime.",
            badges=[
                _build_status_pill("Símbolo", symbol, "accent"),
                _build_status_pill("Timeframe", timeframe, "default"),
                _build_status_pill("Família", runtime_family_label, "warm"),
            ],
        )

        active_runtime_profile = runtime_reference_settings.get("active_profile") or "global"
        runtime_risk_profile = runtime_reference_settings.get("risk_profile") or "normal"
        render_dashboard_strip(
            (
                f"Perfil analítico de referência {active_runtime_profile}. "
                f"RSI({runtime_reference_settings.get('rsi_period')}) "
                f"{runtime_reference_settings.get('rsi_min')}/{runtime_reference_settings.get('rsi_max')} "
                f"com risco {runtime_risk_profile}."
            ),
            badges=[
                _build_status_pill("Perfil", active_runtime_profile, "accent"),
                _build_status_pill("RSI", f"{runtime_reference_settings.get('rsi_min')}/{runtime_reference_settings.get('rsi_max')}", "default"),
                _build_status_pill("Risco", runtime_risk_profile, "warm"),
            ],
        )

        readiness_col1, readiness_col2, readiness_col3, readiness_col4, readiness_col5, readiness_col6 = st.columns(6)
        with readiness_col1:
            st.metric("Processo", "ON" if bot_process_state.get("running") else "OFF")
        with readiness_col2:
            st.metric("Sessão Operador", operator_session_label)
        with readiness_col3:
            st.metric("Lib Telegram", "OK" if telegram_library_ready else "PENDENTE")
        with readiness_col4:
            st.metric("Notif. Sessao", "ON" if session_notifications_enabled else "OFF")
        with readiness_col5:
            subscription_status_label = "ADMIN BYPASS" if admin_session_active and subscription_gate_required else f"{subscription_plan} {'ON' if subscription_active else 'OFF'}"
            st.metric("Assinatura", subscription_status_label)
        with readiness_col6:
            st.metric("Lib WebSocket", "OK" if websocket_library_ready else "PENDENTE")

        if admin_session_active and not workspace_session_active:
            st.info(
                "Sessão Admin detectada. O runtime do bot pode ser ligado para operação técnica mesmo sem login no Workspace."
            )

        if bot_view_mode == "Runtime":
            if not websocket_library_ready:
                st.warning("Biblioteca websockets indisponível. O bot não deve ser ligado sem streaming ativo.")
            if bot_start_block_reason:
                st.warning(bot_start_block_reason)

            if not operator_session_active:
                st.caption(
                    "Para liberar o botão, você pode seguir um destes caminhos: "
                    "`Sidebar -> Entrar no Workspace` ou `Admin -> Entrar`."
                )

            render_bot_telegram_notifications_panel(section_key="bot_runtime_notifications")
            st.markdown("### ▶️ Runtime")
            render_trader_bot_runtime_controls(
                section_key="bot_hub",
                allow_start=bot_start_allowed,
                block_reason=bot_start_block_reason,
            )

        elif bot_view_mode == "Prontidao":
            st.markdown("### 🧪 Checklist de Prontidão")
            st.caption("Antes de ligar o bot, confirme se o ambiente, o acesso e a estratégia de referência estão coerentes.")

            readiness_status_col1, readiness_status_col2 = st.columns(2)
            with readiness_status_col1:
                st.info(
                    f"Workspace ativo: {'sim' if workspace_session_active else 'nao'}\n\n"
                    f"Admin autenticado: {'sim' if admin_session_active else 'nao'}\n\n"
                    f"Sessão operadora: {operator_session_label}\n\n"
                    f"Telegram opcional: {'configurado' if st.session_state.get('telegram_notifications') else 'nao configurado'}\n\n"
                    f"Biblioteca Telegram: {'ok' if telegram_library_ready else 'pendente'}\n\n"
                    f"Biblioteca WebSocket: {'ok' if websocket_library_ready else 'pendente'}\n\n"
                    f"Processo em execucao: {'sim' if bot_process_state.get('running') else 'nao'}"
                )
            with readiness_status_col2:
                st.info(
                    f"Perfil de referencia: {active_runtime_profile}\n\n"
                    f"Familia observada: {runtime_family_label}\n\n"
                    f"Timeframe atual: {timeframe}\n\n"
                    f"Banco: {AppConfig.DB_DISPLAY}"
                )

            if not operator_session_active:
                st.warning("Faça login no Workspace ou autentique o Admin para operar o runtime.")
            if subscription_gate_required and not subscription_gate_satisfied:
                st.warning("Assinatura inativa/expirada para uso do bot. Ative um plano para liberar operação.")
            if not telegram_library_ready:
                st.warning("Biblioteca Telegram indisponível neste ambiente. O runtime pode subir sem comandos interativos completos.")
            if not websocket_library_ready:
                st.warning("Biblioteca websockets indisponível. Sem ela o bot não mantém feed constante de candles.")
            if bot_process_state.get("running"):
                st.success(f"Bot trader ativo com PID {bot_process_state.get('pid')}.")
            else:
                st.info("Bot trader parado no momento.")

        else:
            st.markdown("### 🧭 Fluxo Recomendado")
            st.markdown(
                """
                1. Entre no `Workspace` ou autentique o `Admin` para liberar o runtime.
                2. Use `Mercado` para revisar gráfico, contexto, sinal e risco.
                3. Confira `Prontidao` nesta tela antes de subir o processo.
                4. Ligue o bot em `Runtime` e acompanhe o PID.
                5. Use `Backtest` para validar qualquer ajuste estrutural antes de promover.
                """
            )

        with st.expander("ℹ️ Operacao do Bot", expanded=False):
            st.markdown(
                """
                - Esta tela concentra o controle do processo do bot trader.
                - Use `Mercado` para acompanhar graficos, streaming e leitura operacional.
                - Use `Admin` para configuracoes globais do bot interativo e comunicados.
                """
            )

    # Backtesting Tab - Otimizado para foco em testes
    if active_dashboard_section == "backtest":
        render_section_hero(
            kicker="Backtest",
            title="Centro de validação histórica",
            subtitle="Simulação, auditoria e comparação de desempenho sem confundir resultado histórico com sinal ao vivo.",
            badges=[
                _build_status_pill("Fonte", "WebSocket persistida", "accent"),
                _build_status_pill("Uso", "Validação", "warm"),
                _build_status_pill("Foco", "Retorno + Drawdown", "default"),
            ],
        )
        render_dashboard_strip(
            "Origem desta aba: a mesma WebSocket pública da Binance usada pela dashboard. Os candles fechados são acumulados no banco e depois reaproveitados no backtest.",
        )
        backtest_engine = get_or_init_backtest_engine()
        max_backtest_days = 730
        direction_filter_labels = AppConfig.get_backtest_direction_filter_labels()
        market_reading_family_configs = AppConfig.get_market_reading_family_configs()
        risk_profile_configs = AppConfig.get_risk_profile_configs()
        reading_preset_configs = AppConfig.get_backtest_setup_presets()
        reading_preset_notes = AppConfig.get_backtest_preset_notes()

        def _apply_bt_session_updates(
            updates: dict[str, object],
            preset_name: str | None = None,
            start_days: int | None = None,
        ) -> None:
            for state_key, state_value in updates.items():
                st.session_state[state_key] = list(state_value) if isinstance(state_value, list) else state_value
            if start_days is not None:
                st.session_state.bt_start_date = date.today() - timedelta(days=start_days)
            if preset_name is not None:
                st.session_state.bt_reading_preset = preset_name
                st.session_state.bt_last_reading_preset = preset_name
            if "bt_market_family" in updates:
                st.session_state.bt_last_market_family = updates["bt_market_family"]
            if "bt_risk_profile" in updates:
                st.session_state.bt_last_risk_profile = updates["bt_risk_profile"]

        def _apply_bt_preset(preset_name: str, start_days: int | None = None) -> None:
            preset_updates = AppConfig.get_backtest_preset_updates(preset_name)
            _apply_bt_session_updates(preset_updates, preset_name=preset_name, start_days=start_days)
            st.session_state.bt_family_overlay_key = "global"

        def _apply_bt_family_overlay(symbol_name: str) -> dict[str, object]:
            family_profile = AppConfig.get_backtest_family_profile(symbol_name)
            _apply_bt_preset(
                AppConfig.DEFAULT_BACKTEST_PRESET,
                start_days=AppConfig.DEFAULT_BACKTEST_WINDOW_DAYS,
            )
            overlay_updates = dict(family_profile.get("overrides") or {})
            if overlay_updates:
                _apply_bt_session_updates(overlay_updates)
            st.session_state.bt_family_overlay_key = family_profile.get("family_key", "global")
            return family_profile

        # Quick test presets
        st.markdown("### ⚡ Testes Rápidos")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            if st.button(
                "🚀 Teste Agressivo",
                help=f"Aplica o baseline EMA/RSI aprovado ({AppConfig.DEFAULT_RSI_MIN}/{AppConfig.DEFAULT_RSI_MAX}) por 7 dias",
                width="stretch",
            ):
                _apply_bt_preset("Leitura Ativa (15m)", start_days=7)

        with col2:
            if st.button("✅ Perfil Global", help="Aplica o baseline global EMA/RSI para backtest", width="stretch"):
                _apply_bt_preset(
                    AppConfig.DEFAULT_BACKTEST_PRESET,
                    start_days=AppConfig.DEFAULT_BACKTEST_WINDOW_DAYS,
                )

        with col3:
            if st.button("🛡️ Teste Conservador", help="RSI 50/50, 30 dias", width="stretch"):
                _apply_bt_preset("Leitura Conservadora (1h)", start_days=30)

        with col4:
            if st.button("🔄 Reset Padrão", help="Voltar configurações padrão", width="stretch"):
                _apply_bt_preset(
                    AppConfig.DEFAULT_BACKTEST_PRESET,
                    start_days=AppConfig.DEFAULT_BACKTEST_WINDOW_DAYS,
                )

        st.markdown("---")

        default_reading_preset = (
            AppConfig.DEFAULT_BACKTEST_PRESET
            if AppConfig.DEFAULT_BACKTEST_PRESET in reading_preset_configs
            else list(reading_preset_configs.keys())[0]
        )
        default_preset_updates = dict(reading_preset_configs.get(default_reading_preset) or {})

        if "bt_reading_preset" not in st.session_state:
            st.session_state.bt_reading_preset = default_reading_preset
            for state_key, state_value in default_preset_updates.items():
                st.session_state[state_key] = list(state_value) if isinstance(state_value, list) else state_value
            st.session_state.bt_start_date = date.today() - timedelta(days=AppConfig.DEFAULT_BACKTEST_WINDOW_DAYS)
        if "bt_family_overlay_key" not in st.session_state:
            st.session_state.bt_family_overlay_key = "global"
        if "bt_last_reading_preset" not in st.session_state:
            st.session_state.bt_last_reading_preset = st.session_state.bt_reading_preset
        if "bt_market_family" not in st.session_state:
            st.session_state.bt_market_family = default_preset_updates.get("bt_market_family", "all_states")
        if "bt_last_market_family" not in st.session_state:
            st.session_state.bt_last_market_family = st.session_state.bt_market_family
        if "bt_market_pattern_focus" not in st.session_state:
            legacy_focus = st.session_state.get("bt_setup_focus", st.session_state.get("bt_direction_focus", []))
            st.session_state.bt_market_pattern_focus = list(legacy_focus)
        if "bt_risk_profile" not in st.session_state:
            st.session_state.bt_risk_profile = default_preset_updates.get("bt_risk_profile", "manual")
        if "bt_last_risk_profile" not in st.session_state:
            st.session_state.bt_last_risk_profile = st.session_state.bt_risk_profile

        selected_reading_preset = st.selectbox(
            "Preset de Leitura",
            options=list(reading_preset_configs.keys()),
            help="Aplica um conjunto coerente de leitura de mercado, filtros e política de risco.",
            key="bt_reading_preset",
        )
        st.caption("Preset Operacional")
        if st.session_state.bt_last_reading_preset != selected_reading_preset:
            _apply_bt_preset(selected_reading_preset)
        st.caption(reading_preset_notes.get(selected_reading_preset, ""))
        if selected_reading_preset == AppConfig.DEFAULT_BACKTEST_PRESET:
            st.info(AppConfig.DEFAULT_BACKTEST_PRESET_SUMMARY)
        # Compatibilidade Legada de Execução
        # Cesta de Setups (marcador legado para histórico e testes de integração)
        # Filtro de leitura / market pattern
        # allowed_execution_setups = list(dict.fromkeys(bt_setup_focus)) or None
        # allowed_execution_setups = list(dict.fromkeys(bt_market_pattern_focus)) or None
        # Criterios de Aprovacao Real
        # Meta de throughput
        # IA Integrada (XGBoost)
        # IA no Motor

        # Main configuration in tabs
        config_tab1, config_tab2, config_tab3 = st.tabs(["📊 Básico", "⚙️ Avançado", "📈 Otimização"])

        with config_tab1:
            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**🎯 Configuração Principal**")
                
                # Usar sempre o símbolo configurado na sidebar
                bt_symbol = symbol
                st.success(f"✅ **Par do Backtest:** {bt_symbol}")
                st.caption(
                    f"Perfil ativo: {selected_reading_preset} | "
                    f"Familia observada: {AppConfig.get_symbol_profile_family_label(bt_symbol)}"
                )
                st.info("💡 *Usando par configurado na sidebar*")
                family_profile = AppConfig.get_backtest_family_profile(bt_symbol)
                family_override_updates = dict(family_profile.get("overrides") or {})
                active_family_overlay_key = str(st.session_state.get("bt_family_overlay_key") or "global")
                if family_override_updates:
                    overlay_summary = []
                    if family_override_updates.get("bt_enable_volume_filter"):
                        overlay_summary.append("volume ON")
                    if family_override_updates.get("bt_enable_trend_filter"):
                        overlay_summary.append("tendencia ON")
                    if family_override_updates.get("bt_enable_avoid_ranging"):
                        overlay_summary.append("anti-ranging ON")
                    if "bt_stop_loss_pct" in family_override_updates:
                        overlay_summary.append(f"SL {family_override_updates['bt_stop_loss_pct']:.1f}%")
                    if "bt_take_profit_pct" in family_override_updates:
                        overlay_summary.append(f"TP {family_override_updates['bt_take_profit_pct']:.1f}%")

                    st.caption(
                        f"Overlay sugerido para {family_profile.get('label')}: {family_profile.get('description')}"
                    )
                    st.caption(
                        "Ajustes sugeridos sobre o perfil global: "
                        + (", ".join(overlay_summary) if overlay_summary else "sem ajustes extras")
                    )

                    overlay_col1, overlay_col2 = st.columns(2)
                    with overlay_col1:
                        if st.button(
                            f"Aplicar Overlay {family_profile.get('label')}",
                            key=f"bt_apply_family_overlay_{family_profile.get('family_key')}",
                            width="stretch",
                        ):
                            _apply_bt_family_overlay(bt_symbol)
                            st.rerun()
                    with overlay_col2:
                        if active_family_overlay_key != "global" and st.button(
                            "Voltar ao Global",
                            key="bt_clear_family_overlay",
                            width="stretch",
                        ):
                            _apply_bt_preset(
                                AppConfig.DEFAULT_BACKTEST_PRESET,
                                start_days=AppConfig.DEFAULT_BACKTEST_WINDOW_DAYS,
                            )
                            st.rerun()
                else:
                    st.caption(
                        "Esta familia usa o baseline global sem overlay adicional recomendado no momento."
                    )

                bt_timeframe = st.selectbox(
                    "Timeframe:",
                    ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                    index=2,
                    help="O motor lê o mercado exatamente neste timeframe.",
                    key="bt_timeframe"
                )
                import config as runtime_config

                websocket_backtest_status = backtest_engine.sync_backtest_websocket_feed(
                    symbol=bt_symbol,
                    timeframe=bt_timeframe,
                    testnet=bool(getattr(runtime_config, "BACKTEST_USE_TESTNET", False)),
                    snapshot_limit=min(max(int(getattr(AppConfig, "MAX_CANDLES", 1200) or 1200), 600), 3000),
                )
                websocket_coverage = websocket_backtest_status.get("coverage") or {}
                websocket_stream_status = websocket_backtest_status.get("stream_status") or {}
                ws_col1, ws_col2, ws_col3 = st.columns(3)
                with ws_col1:
                    st.metric("Candles WS no Banco", int(websocket_coverage.get("total") or 0))
                with ws_col2:
                    st.metric("Buffer Compartilhado", int(websocket_backtest_status.get("shared_available") or 0))
                with ws_col3:
                    st.metric("Feed Backtest", "ON" if websocket_stream_status.get("connected") else "AQUECENDO")
                st.caption(
                    f"Cobertura acumulada: {websocket_coverage.get('first_timestamp') or '-'} "
                    f"até {websocket_coverage.get('last_timestamp') or '-'} | "
                    f"endpoint: {websocket_stream_status.get('provider') or '-'}"
                )

                context_timeframe_options = [tf for tf in ["5m", "15m", "30m", "1h", "4h", "1d"] if tf != bt_timeframe]
                context_mode_options = ["same_timeframe", *context_timeframe_options]
                if (
                    "bt_context_mode" not in st.session_state
                    or st.session_state.bt_context_mode not in context_mode_options
                ):
                    st.session_state.bt_context_mode = "same_timeframe"

                bt_context_mode = st.selectbox(
                    "Contexto Operacional:",
                    options=context_mode_options,
                    help="Use o proprio timeframe para leitura pura do mercado. Escolha outro apenas se quiser adicionar um filtro extra manual.",
                    key="bt_context_mode",
                    format_func=lambda value: (
                        f"Mesmo timeframe ({bt_timeframe})"
                        if value == "same_timeframe"
                        else value
                    ),
                )
                bt_context_timeframe = (
                    None
                    if bt_context_mode == "same_timeframe"
                    else bt_context_mode
                )
                if bt_context_timeframe:
                    st.caption(f"Contexto extra manual para este teste: {bt_context_timeframe}")
                else:
                    st.caption(f"Leitura principal no proprio {bt_timeframe}, sem filtro superior implicito.")

                bt_market_family = st.selectbox(
                    "Leitura de Mercado:",
                    options=list(market_reading_family_configs.keys()),
                    help="Define a família de estados de mercado que o backtest vai privilegiar. Internamente isso vira uma compatibilidade de execução, mas a decisão continua sendo por leitura do mercado.",
                    key="bt_market_family",
                    format_func=lambda value: market_reading_family_configs[value]["label"],
                )
                if st.session_state.bt_last_market_family != bt_market_family:
                    st.session_state.bt_direction_focus = list(
                        market_reading_family_configs[bt_market_family]["allowed_directions"]
                    )
                    st.session_state.bt_last_market_family = bt_market_family
                st.caption(market_reading_family_configs[bt_market_family]["description"])

                bt_initial_balance = st.number_input(
                    "Capital Inicial ($)", 
                    min_value=100.0, 
                    max_value=1000000.0, 
                    value=10000.0,
                    step=1000.0,
                    help="Quanto você investiria na estratégia",
                    key="bt_initial_balance"
                )

            with col2:
                st.markdown("**📅 Período de Teste**")

                # Presets de período
                period_preset = st.selectbox(
                    "Período Pré-definido:",
                    [
                        "Personalizado",
                        "Última Semana",
                        "Últimas 2 Semanas",
                        "Último Mês",
                        "Últimos 3 Meses",
                        "Últimos 6 Meses",
                        "Último Ano",
                        "Últimos 2 Anos",
                    ],
                    help="Escolha um período comum ou customize",
                    key="bt_period_preset",
                )

                max_date = date.today()

                if period_preset == "Última Semana":
                    default_start = max_date - timedelta(days=7)
                elif period_preset == "Últimas 2 Semanas":
                    default_start = max_date - timedelta(days=14)
                elif period_preset == "Último Mês":
                    default_start = max_date - timedelta(days=30)
                elif period_preset == "Últimos 3 Meses":
                    default_start = max_date - timedelta(days=90)
                elif period_preset == "Últimos 6 Meses":
                    default_start = max_date - timedelta(days=180)
                elif period_preset == "Último Ano":
                    default_start = max_date - timedelta(days=365)
                elif period_preset == "Últimos 2 Anos":
                    default_start = max_date - timedelta(days=max_backtest_days)
                else:
                    default_start = max_date - timedelta(days=30)

                if 'bt_last_period_preset' not in st.session_state:
                    st.session_state.bt_last_period_preset = period_preset

                preset_changed = st.session_state.bt_last_period_preset != period_preset
                if preset_changed and period_preset != "Personalizado":
                    st.session_state.bt_start_date = default_start
                    st.session_state.bt_end_date = max_date
                st.session_state.bt_last_period_preset = period_preset

                bt_start_date = st.date_input(
                    "📅 Data Inicial", 
                    value=getattr(st.session_state, 'bt_start_date', default_start),
                    max_value=max_date,
                    help="Início do backtest",
                    key="bt_start_date"
                )
                bt_end_date = st.date_input(
                    "📅 Data Final", 
                    value=getattr(st.session_state, 'bt_end_date', max_date),
                    max_value=max_date,
                    help="Fim do backtest",
                    key="bt_end_date"
                )

                # Mostrar duração
                if bt_start_date < bt_end_date:
                    duration = (bt_end_date - bt_start_date).days
                    st.info(f"📊 Período: **{duration} dias**")

        with config_tab2:
            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**🎛️ Gatilhos RSI**")

                bt_rsi_period = st.slider(
                    "Período RSI", 
                    5, 50, 
                    getattr(st.session_state, 'bt_rsi_period', AppConfig.DEFAULT_RSI_PERIOD),
                    help="Janela de cálculo do RSI (14 é padrão)",
                    key="bt_rsi_period"
                )

                bt_rsi_min = st.slider(
                    "RSI Gatilho Compra", 
                    45, 70, 
                    getattr(st.session_state, 'bt_rsi_min', AppConfig.DEFAULT_RSI_MIN),
                    help="RSI precisa cruzar acima deste nivel para compra",
                    key="bt_rsi_min"
                )

                bt_rsi_max = st.slider(
                    "RSI Gatilho Venda", 
                    30, 55, 
                    getattr(st.session_state, 'bt_rsi_max', AppConfig.DEFAULT_RSI_MAX),
                    help="RSI precisa cruzar abaixo deste nivel para venda",
                    key="bt_rsi_max"
                )

            with col2:
                st.markdown("**⚡ Configurações de Performance**")

                if "bt_direction_focus" not in st.session_state or not isinstance(st.session_state.bt_direction_focus, list):
                    st.session_state.bt_direction_focus = list(direction_filter_labels.keys())
                if "bt_enable_volume_filter" not in st.session_state:
                    st.session_state.bt_enable_volume_filter = False
                if "bt_enable_trend_filter" not in st.session_state:
                    st.session_state.bt_enable_trend_filter = False
                if "bt_enable_avoid_ranging" not in st.session_state:
                    st.session_state.bt_enable_avoid_ranging = False

                st.info(
                    "A leitura do mercado decide direcao e contexto. SL/TP abaixo definem apenas a politica de risco do usuario."
                )

                enable_volume_filter = st.checkbox(
                    "Filtrar por Volume",
                    help="Apenas trades com volume acima da média",
                    key="bt_enable_volume_filter",
                )

                enable_trend_filter = st.checkbox(
                    "Filtrar por Tendência",
                    help="Usar MACD como filtro adicional",
                    key="bt_enable_trend_filter",
                )

                enable_avoid_ranging = st.checkbox(
                    "Evitar Mercado Lateral",
                    help="Bloqueia trades quando o regime estimado for lateralizado",
                    key="bt_enable_avoid_ranging",
                )

                recommended_stop_loss = 1.0 if bt_timeframe == "1h" else 0.8
                if "bt_stop_loss_pct" not in st.session_state:
                    st.session_state.bt_stop_loss_pct = float(recommended_stop_loss)
                if "bt_take_profit_pct" not in st.session_state:
                    st.session_state.bt_take_profit_pct = 1.8
                if "bt_enable_oos_validation" not in st.session_state:
                    st.session_state.bt_enable_oos_validation = True
                if "bt_validation_split_pct" not in st.session_state:
                    st.session_state.bt_validation_split_pct = 30
                if "bt_risk_profile" not in st.session_state:
                    st.session_state.bt_risk_profile = "manual"
                if "bt_ai_min_win_probability" not in st.session_state:
                    st.session_state.bt_ai_min_win_probability = float(ProductionConfig.AI_MIN_WIN_PROBABILITY)
                if "bt_ai_compare_baseline" not in st.session_state:
                    st.session_state.bt_ai_compare_baseline = bool(ProductionConfig.AI_COMPARE_BASELINE_DEFAULT)
                if "bt_fast_mode" not in st.session_state:
                    st.session_state.bt_fast_mode = bool(ProductionConfig.BACKTEST_FAST_MODE_DEFAULT)

                selected_risk_profile = st.selectbox(
                    "Perfil de Risco do Usuário",
                    options=list(risk_profile_configs.keys()),
                    help="A leitura continua a mesma. Aqui você define como quer transformar essa leitura em risco e alvo.",
                    key="bt_risk_profile",
                    format_func=lambda value: risk_profile_configs[value]["label"],
                )
                if st.session_state.bt_last_risk_profile != selected_risk_profile:
                    risk_profile = risk_profile_configs.get(selected_risk_profile, {})
                    if "stop_loss_pct" in risk_profile:
                        st.session_state.bt_stop_loss_pct = float(risk_profile["stop_loss_pct"])
                    if "take_profit_pct" in risk_profile:
                        st.session_state.bt_take_profit_pct = float(risk_profile["take_profit_pct"])
                    st.session_state.bt_last_risk_profile = selected_risk_profile
                st.caption(risk_profile_configs[selected_risk_profile]["description"])

                stop_loss_pct = st.number_input(
                    "Stop Loss (%)",
                    min_value=0.0,
                    max_value=20.0,
                    step=0.5,
                    help="0 = sem stop loss",
                    key="bt_stop_loss_pct",
                )

                take_profit_pct = st.number_input(
                    "Take Profit (%)",
                    min_value=0.0,
                    max_value=50.0,
                    step=0.5,
                    help="0 = sem take profit",
                    key="bt_take_profit_pct",
                )

                enable_oos_validation = st.checkbox(
                    "Validar Fora da Amostra",
                    help="Reserva a parte final do período para validar a estratégia em dados futuros",
                    key="bt_enable_oos_validation",
                )

                validation_split_pct = st.slider(
                    "Parte Fora da Amostra (%)",
                    10,
                    50,
                    disabled=not enable_oos_validation,
                    help="Percentual final do período reservado para validação temporal",
                    key="bt_validation_split_pct",
                )

                fast_backtest_mode = st.checkbox(
                    "Modo Rápido",
                    help="Reduz auditoria detalhada, explicações da IA e janelas internas do loop para agilizar o backtest.",
                    key="bt_fast_mode",
                )
                if fast_backtest_mode:
                    st.caption("Modo rápido ativo: prioriza fluidez na dashboard para análises iterativas.")

                with st.expander("IA Integrada (XGBoost)", expanded=False):
                    ai_runtime_status = get_ai_runtime_status(backtest_engine)
                    ai_assist_mode = ProductionConfig.AI_ASSIST_MODE if ProductionConfig.ENABLE_AI_ASSISTANT else "disabled"
                    ai_min_win_probability = st.slider(
                        "Piso de probabilidade",
                        min_value=0.50,
                        max_value=0.80,
                        value=float(st.session_state.get("bt_ai_min_win_probability", 0.60) or 0.60),
                        step=0.01,
                        help="Ajuste fino do filtro auxiliar da IA para os testes deste backtest.",
                        key="bt_ai_min_win_probability",
                        disabled=ai_assist_mode != "filter",
                    )
                    ai_compare_baseline = st.checkbox(
                        "Comparar com baseline sem IA",
                        help="Executa o cenário atual e o mesmo cenário sem IA para medir ganho real.",
                        key="bt_ai_compare_baseline",
                    )

                    status_col1, status_col2, status_col3, status_col4 = st.columns(4)
                    with status_col1:
                        st.metric("IA", "ativa" if ProductionConfig.ENABLE_AI_ASSISTANT else "desligada")
                    with status_col2:
                        st.metric("Runtime", "carregado" if ai_runtime_status.get("runtime_loaded") else "indisponível")
                    with status_col3:
                        st.metric("Dataset", int(ai_runtime_status.get("dataset_rows", 0) or 0))
                    with status_col4:
                        st.metric("ROC AUC", f"{float((ai_runtime_status.get('metrics') or {}).get('roc_auc', 0.0) or 0.0):.3f}")

                    ai_model_version = ai_runtime_status.get("runtime_version") or ai_runtime_status.get("model_version") or "-"
                    st.caption(f"Versão do modelo: {ai_model_version} | modo padrão: {ai_assist_mode}")
                    if ai_runtime_status.get("metadata_error"):
                        st.warning(f"Metadados da IA indisponíveis: {ai_runtime_status['metadata_error']}")

                    test_period = ai_runtime_status.get("test_period") or {}
                    if test_period.get("start") and test_period.get("end"):
                        st.caption(
                            "Janela temporal de teste do modelo: "
                            f"{pd.Timestamp(test_period['start']).strftime('%d/%m/%Y %H:%M')} -> "
                            f"{pd.Timestamp(test_period['end']).strftime('%d/%m/%Y %H:%M')}"
                        )

                    top_importances = ai_runtime_status.get("top_feature_importances") or []
                    if top_importances:
                        st.caption("Features mais importantes da versão atual")
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "Feature": item.get("feature"),
                                        "Importância": round(float(item.get("importance", 0.0) or 0.0), 4),
                                    }
                                    for item in top_importances
                                ]
                            ),
                            width="stretch",
                            hide_index=True,
                        )

                with st.expander("Compatibilidade Legada de Execução", expanded=False):
                    st.caption(
                        "Este bloco restringe apenas o lado operacional. O motor principal continua classificando e decidindo por leitura de mercado."
                    )
                    bt_direction_focus = st.multiselect(
                        "Direções Permitidas",
                        options=list(direction_filter_labels.keys()),
                        help="Escolha se o backtest aceita compra, venda ou ambos.",
                        key="bt_direction_focus",
                        format_func=lambda value: direction_filter_labels[value],
                    )
                    if not bt_direction_focus:
                        st.warning("Selecione ao menos uma direção para o backtest.")

        with config_tab3:
            st.markdown("**🔍 Otimização de Parâmetros**")

            # Grid search para RSI
            optimization_allowed = AppConfig.ENABLE_PARAMETER_OPTIMIZATION
            enable_optimization = st.checkbox(
                "🚀 Modo Otimização Automática",
                value=False,
                disabled=not optimization_allowed,
                help="Testa múltiplas combinações de RSI automaticamente"
            )
            if not optimization_allowed:
                st.caption("Otimização global desativada para manter um único motor de leitura fixo.")

            if enable_optimization:
                col1, col2 = st.columns(2)

                with col1:
                    rsi_min_range = st.slider(
                        "Range RSI Compra",
                        45, 60, (50, 55),
                        help="Faixa para testar o gatilho comprador"
                    )

                    rsi_max_range = st.slider(
                        "Range RSI Venda", 
                        40, 55, (45, 50),
                        help="Faixa para testar o gatilho vendedor"
                    )

                with col2:
                    optimization_metric = st.selectbox(
                        "Métrica de Otimização:",
                        ["Total Return", "Sharpe Ratio", "Win Rate", "Profit Factor"],
                        help="Qual métrica maximizar"
                    )

                    max_tests = st.number_input(
                        "Máximo de Testes:",
                        min_value=5,
                        max_value=50,
                        value=20,
                        help="Limite de combinações para testar"
                    )

            # Comparação de timeframes
            scan_allowed = AppConfig.ENABLE_MARKET_SCAN
            compare_timeframes = st.checkbox(
                "📊 Comparar Timeframes",
                disabled=not scan_allowed,
                help="Testa a mesma estratégia em diferentes timeframes"
            )

            compare_symbols = st.checkbox(
                "🪙 Comparar Pares",
                disabled=not scan_allowed,
                help="Executa o mesmo backtest em múltiplos pares para encontrar onde o edge realmente se sustenta"
            )
            if not scan_allowed:
                st.caption("Scan comparativo desativado: foco em um único mercado e timeframe.")

            supported_scan_timeframes = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
            default_scan_timeframes = list(dict.fromkeys([bt_timeframe, "15m", "1h"]))
            default_scan_timeframes = [tf for tf in default_scan_timeframes if tf in supported_scan_timeframes]
            comparison_timeframes = [bt_timeframe]
            if compare_timeframes:
                comparison_timeframes = st.multiselect(
                    "Timeframes do Scan",
                    options=supported_scan_timeframes,
                    default=default_scan_timeframes or [bt_timeframe],
                    help="Compare a robustez da estratégia em múltiplos timeframes",
                    key="bt_comparison_timeframes",
                )

            supported_scan_symbols = AppConfig.get_supported_pairs()
            default_scan_symbols = list(dict.fromkeys([bt_symbol, "BTC/USDT", "ETH/USDT"]))
            default_scan_symbols = [sym for sym in default_scan_symbols if sym in supported_scan_symbols]
            comparison_symbols = [bt_symbol]
            if compare_symbols:
                comparison_symbols = st.multiselect(
                    "Pares do Scan",
                    options=supported_scan_symbols,
                    default=default_scan_symbols or [bt_symbol],
                    help="Selecione os pares para o scan comparativo",
                    key="bt_comparison_symbols",
                )

            comparison_timeframes = comparison_timeframes or [bt_timeframe]
            comparison_symbols = comparison_symbols or [bt_symbol]
            comparison_combo_count = len(comparison_symbols) * len(comparison_timeframes)
            if compare_timeframes or compare_symbols:
                st.caption(
                    f"Scan configurado: {len(comparison_symbols)} par(es) x "
                    f"{len(comparison_timeframes)} timeframe(s) = {comparison_combo_count} cenário(s)"
                )

            st.markdown("**🧱 Robustez Global**")
            default_robustness_symbols = [
                sym for sym in AppConfig.get_global_validation_symbols() if sym in supported_scan_symbols
            ]
            default_robustness_horizons = AppConfig.get_global_validation_horizons()
            robustness_overlay_mode_options = {
                "disabled": "Somente Configuração Atual",
                "recommended": "Atual + Overlay por Família",
            }

            enable_global_robustness = st.checkbox(
                "🧱 Matriz de Robustez Global",
                value=False,
                help="Roda a leitura atual em uma cesta oficial multi-mercado e múltiplos horizontes temporais.",
                key="bt_enable_global_robustness",
            )

            robustness_symbols = default_robustness_symbols
            robustness_horizons = default_robustness_horizons
            robustness_overlay_mode = "disabled"
            if enable_global_robustness:
                robustness_symbols = st.multiselect(
                    "Cesta Oficial",
                    options=supported_scan_symbols,
                    default=default_robustness_symbols,
                    help="Selecione os mercados usados para medir robustez transversal.",
                    key="bt_robustness_symbols",
                )
                robustness_horizons = st.multiselect(
                    "Horizontes (dias)",
                    options=default_robustness_horizons,
                    default=default_robustness_horizons,
                    help="Curto, médio e longo prazo são derivados dessas janelas históricas.",
                    key="bt_robustness_horizons",
                )
                robustness_overlay_mode = st.selectbox(
                    "Modo da Matriz",
                    options=list(robustness_overlay_mode_options.keys()),
                    help="Escolha se a validação usa a configuração atual pura ou com overlays recomendados por família.",
                    key="bt_robustness_overlay_mode",
                    format_func=lambda value: robustness_overlay_mode_options[value],
                )
                robustness_combo_count = len(robustness_symbols) * len(robustness_horizons)
                st.caption(
                    f"Matriz configurada: {len(robustness_symbols)} símbolo(s) x "
                    f"{len(robustness_horizons)} horizonte(s) = {robustness_combo_count} cenário(s)"
                )
                st.caption(
                    f"Data final âncora: {bt_end_date} | a data inicial é recalculada automaticamente por horizonte."
                )

            if "bt_enable_walk_forward" not in st.session_state:
                st.session_state.bt_enable_walk_forward = True
            if "bt_walk_forward_windows" not in st.session_state:
                st.session_state.bt_walk_forward_windows = 3

            enable_walk_forward = st.checkbox(
                "🧭 Walk-Forward",
                help="Executa validação sequencial em múltiplas janelas temporais",
                key="bt_enable_walk_forward",
            )

            walk_forward_windows = st.slider(
                "Janelas Walk-Forward",
                2,
                5,
                disabled=not enable_walk_forward,
                help="Quantidade de janelas out-of-sample sequenciais",
                key="bt_walk_forward_windows",
            )

        allowed_signal_directions = list(dict.fromkeys(bt_direction_focus)) or None
        execution_context_timeframe = bt_context_timeframe
        ai_assist_mode = ProductionConfig.AI_ASSIST_MODE if ProductionConfig.ENABLE_AI_ASSISTANT else "disabled"
        ai_min_win_probability = float(
            st.session_state.get("bt_ai_min_win_probability", ProductionConfig.AI_MIN_WIN_PROBABILITY)
            or ProductionConfig.AI_MIN_WIN_PROBABILITY
        )
        ai_compare_baseline = bool(st.session_state.get("bt_ai_compare_baseline", ProductionConfig.AI_COMPARE_BASELINE_DEFAULT))

        # Validation and execution
        date_valid = bt_start_date < bt_end_date
        period_days = (bt_end_date - bt_start_date).days

        st.markdown("---")
        st.markdown("### Criterios de Aprovacao Real")

        required_trade_velocity = (
            ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION
            / max(ProductionConfig.MIN_PROMOTION_PERIOD_DAYS, 1)
        )
        risk_reward_ratio = (
            take_profit_pct / stop_loss_pct
            if stop_loss_pct > 0 and take_profit_pct > 0
            else None
        )
        selected_direction_label = ", ".join(
            direction_filter_labels[direction]
            for direction in (allowed_signal_directions or list(direction_filter_labels.keys()))
            if direction in direction_filter_labels
        )
        selected_market_family = market_reading_family_configs.get(
            st.session_state.get("bt_market_family", "all_states"),
            market_reading_family_configs["all_states"],
        )
        selected_risk_profile_label = risk_profile_configs.get(
            st.session_state.get("bt_risk_profile", "manual"),
            risk_profile_configs["manual"],
        )["label"]

        approval_col1, approval_col2 = st.columns(2)
        with approval_col1:
            st.info(
                "\n".join(
                    [
                        f"Leitura em foco: {selected_market_family['label']}",
                        f"Direções operacionais: {selected_direction_label}",
                        (
                            f"Contexto operacional: {bt_context_timeframe}"
                            if bt_context_timeframe
                            else f"Contexto operacional: somente {bt_timeframe}"
                        ),
                        f"Meta minima: {ProductionConfig.MIN_BACKTEST_TRADES_FOR_PROMOTION} trades em {ProductionConfig.MIN_PROMOTION_PERIOD_DAYS} dias",
                        f"Meta de throughput: {required_trade_velocity:.2f} trades aprovados/dia",
                    ]
                )
            )
        with approval_col2:
            st.info(
                "\n".join(
                    [
                        f"Perfil de risco: {selected_risk_profile_label}",
                        f"OOS minimo: {ProductionConfig.MIN_PROMOTION_OOS_TRADES} trades | PF >= {ProductionConfig.MIN_PROMOTION_OOS_PROFIT_FACTOR:.2f}",
                        f"Walk-forward minimo: {ProductionConfig.MIN_WALK_FORWARD_PASS_RATE_PCT:.0f}% das janelas",
                        f"Max drawdown: {ProductionConfig.MAX_PROMOTION_DRAWDOWN:.1f}%",
                        (
                            f"RR atual: {risk_reward_ratio:.2f}:1"
                            if risk_reward_ratio is not None
                            else "RR atual: defina SL e TP para medir risco/retorno"
                        ),
                    ]
                )
            )

        if period_days < ProductionConfig.MIN_PROMOTION_PERIOD_DAYS:
            st.warning(
                f"Janela curta: a aprovacao real exige pelo menos {ProductionConfig.MIN_PROMOTION_PERIOD_DAYS} dias de dados."
            )
        if risk_reward_ratio is not None and risk_reward_ratio < 1.5:
            st.warning("RR abaixo de 1.5:1. Com essa relacao, a consistencia fica estatisticamente mais dificil.")

        st.markdown("### 🚀 Executar Testes")

        # Status da configuração
        col1, col2 = st.columns(2)

        with col1:
            if not date_valid:
                st.error("❌ Data inicial deve ser anterior à data final")
            elif period_days > max_backtest_days:
                st.error(f"❌ Período muito longo. Máximo suportado: {max_backtest_days} dias")
            elif period_days > 365:
                st.warning("⚠️ Período longo de sobrevivência. O backtest pode demorar bastante.")
            elif period_days > 90:
                st.warning("⚠️ Período longo pode demorar mais")
            elif period_days < 1:
                st.error("❌ Período muito curto. Mínimo: 1 dia")
            else:
                st.success(f"✅ Configuração válida - {period_days} dias")

        with col2:
            # Estimativa de tempo
            if date_valid and period_days > 0:
                estimated_time = max(5, min(period_days * 0.35, 180))
                st.info(f"⏱️ Tempo estimado: ~{estimated_time:.0f}s")

        # Execution buttons
        robustness_ready = bool(robustness_symbols and robustness_horizons)
        col1, col2, col3, col4 = st.columns(4)
        run_optimization = False
        run_market_scan = False
        run_robustness_matrix = False

        with col1:
            bt_execute = st.button(
                "🚀 Executar Backtest", 
                disabled=not date_valid or period_days < 1 or period_days > max_backtest_days or not allowed_signal_directions,
                help="Rodar simulação com configurações atuais",
                width="stretch",
                key="bt_execute"
            )

        with col2:
            if enable_optimization and st.button(
                "⚡ Otimização Automática",
                disabled=not date_valid or period_days < 1 or period_days > max_backtest_days or not allowed_signal_directions,
                help="Testar múltiplas combinações automaticamente",
                width="stretch",
                key="bt_optimize"
            ):
                run_optimization = True
                bt_execute = True

        with col3:
            if (compare_timeframes or compare_symbols) and st.button(
                "🧭 Scan Comparativo",
                disabled=not date_valid or period_days < 1 or period_days > max_backtest_days or not allowed_signal_directions,
                help="Testar a estratégia em múltiplos pares e/ou timeframes",
                width="stretch",
                key="bt_compare"
            ):
                run_market_scan = True
                bt_execute = True

        with col4:
            if enable_global_robustness and st.button(
                "🧱 Matriz Global",
                disabled=not robustness_ready or not allowed_signal_directions,
                help="Valida a leitura atual em uma cesta oficial multi-mercado e multi-horizonte.",
                width="stretch",
                key="bt_global_matrix",
            ):
                run_robustness_matrix = True
                bt_execute = True

        if bt_execute and (date_valid or run_robustness_matrix):
            with st.spinner("🔄 Executando backtest... Isso pode levar alguns minutos."):
                try:
                    # Convert dates to datetime
                    start_dt = datetime.combine(bt_start_date, datetime.min.time())
                    end_dt = datetime.combine(bt_end_date, datetime.max.time())

                    # Validações adicionais
                    if not run_robustness_matrix and period_days > max_backtest_days:
                        st.error(f"❌ Período muito longo. Máximo suportado: {max_backtest_days} dias")
                        st.stop()

                    if run_optimization:
                        st.info(
                            f"⚡ Executando otimização RSI para {bt_symbol} {bt_timeframe} "
                            f"em até {int(max_tests)} combinações..."
                        )

                        optimization_results = backtest_engine.optimize_rsi_parameters(
                            symbol=bt_symbol,
                            timeframe=bt_timeframe,
                            rsi_min_range=rsi_min_range,
                            rsi_max_range=rsi_max_range,
                            max_tests=int(max_tests),
                            optimization_metric=optimization_metric,
                            start_date=start_dt,
                            end_date=end_dt,
                            initial_balance=int(bt_initial_balance),
                            rsi_period=bt_rsi_period,
                            context_timeframe=execution_context_timeframe,
                            stop_loss_pct=stop_loss_pct,
                            take_profit_pct=take_profit_pct,
                            require_volume=enable_volume_filter,
                            require_trend=enable_trend_filter,
                            avoid_ranging=enable_avoid_ranging,
                            validation_split_pct=validation_split_pct if enable_oos_validation else 0.0,
                            walk_forward_windows=walk_forward_windows if enable_walk_forward else 0,
                            allowed_execution_setups=allowed_signal_directions,
                            fast_mode=fast_backtest_mode,
                        )

                        if optimization_results and optimization_results.get('rows'):
                            st.session_state.backtest_scan_results = None
                            st.session_state.backtest_optimization_results = optimization_results
                            st.session_state.backtest_robustness_results = None
                            st.session_state.backtest_results = optimization_results.get('best_result')
                            best_optimization = optimization_results.get('best') or {}
                            st.success("✅ Otimização concluída com sucesso!")
                            if best_optimization:
                                st.caption(
                                    f"Melhor configuração: RSI {best_optimization.get('rsi_min')}-"
                                    f"{best_optimization.get('rsi_max')} | "
                                    f"Score {best_optimization.get('quality_score', 0):.1f}"
                                )
                            st.balloons()
                        else:
                            st.error("❌ A otimização não retornou resultados válidos")
                    elif run_market_scan:
                        st.info(
                            f"📊 Executando scan comparativo com {len(comparison_symbols)} par(es) e "
                            f"{len(comparison_timeframes)} timeframe(s)..."
                        )

                        scan_results = backtest_engine.run_market_scan(
                            symbols=comparison_symbols,
                            timeframes=comparison_timeframes,
                            start_date=start_dt,
                            end_date=end_dt,
                            initial_balance=int(bt_initial_balance),
                            rsi_period=bt_rsi_period,
                            rsi_min=bt_rsi_min,
                            rsi_max=bt_rsi_max,
                            context_timeframe=execution_context_timeframe,
                            stop_loss_pct=stop_loss_pct,
                            take_profit_pct=take_profit_pct,
                            require_volume=enable_volume_filter,
                            require_trend=enable_trend_filter,
                            avoid_ranging=enable_avoid_ranging,
                            validation_split_pct=validation_split_pct if enable_oos_validation else 0.0,
                            walk_forward_windows=walk_forward_windows if enable_walk_forward else 0,
                            allowed_execution_setups=allowed_signal_directions,
                            fast_mode=fast_backtest_mode,
                        )

                        if scan_results and scan_results.get('rows'):
                            st.session_state.backtest_scan_results = scan_results
                            st.session_state.backtest_optimization_results = None
                            st.session_state.backtest_robustness_results = None
                            st.session_state.backtest_results = scan_results.get('best_result')
                            best_scan = scan_results.get('best') or {}
                            st.success("✅ Scan comparativo concluído com sucesso!")
                            if best_scan:
                                st.caption(
                                    f"Melhor cenário: {best_scan.get('symbol')} {best_scan.get('timeframe')} "
                                    f"| Score {best_scan.get('quality_score', 0):.1f}"
                                )
                            st.balloons()
                        else:
                            st.error("❌ O scan comparativo não retornou resultados válidos")
                    elif run_robustness_matrix:
                        st.info(
                            f"🧱 Executando matriz global com {len(robustness_symbols)} símbolo(s) e "
                            f"{len(robustness_horizons)} horizonte(s) no {bt_timeframe}..."
                        )

                        robustness_results = backtest_engine.run_global_robustness_matrix(
                            symbols=robustness_symbols,
                            horizon_days=robustness_horizons,
                            timeframe=bt_timeframe,
                            end_date=end_dt,
                            family_overlay_mode=robustness_overlay_mode,
                            initial_balance=int(bt_initial_balance),
                            rsi_period=bt_rsi_period,
                            rsi_min=bt_rsi_min,
                            rsi_max=bt_rsi_max,
                            context_timeframe=execution_context_timeframe,
                            stop_loss_pct=stop_loss_pct,
                            take_profit_pct=take_profit_pct,
                            require_volume=enable_volume_filter,
                            require_trend=enable_trend_filter,
                            avoid_ranging=enable_avoid_ranging,
                            validation_split_pct=validation_split_pct if enable_oos_validation else 0.0,
                            walk_forward_windows=walk_forward_windows if enable_walk_forward else 0,
                            allowed_execution_setups=allowed_signal_directions,
                            fast_mode=fast_backtest_mode,
                        )

                        if robustness_results and robustness_results.get('rows'):
                            st.session_state.backtest_scan_results = None
                            st.session_state.backtest_optimization_results = None
                            st.session_state.backtest_robustness_results = robustness_results
                            st.session_state.backtest_results = robustness_results.get('best_result')
                            robustness_summary = robustness_results.get('summary', {})
                            best_robustness = robustness_results.get('best') or {}
                            st.success("✅ Matriz global concluída com sucesso!")
                            if best_robustness:
                                st.caption(
                                    f"Score global {robustness_summary.get('robustness_score', 0):.1f} | "
                                    f"Melhor cenário: {best_robustness.get('symbol')} {best_robustness.get('horizon_days')}d "
                                    f"| Score {best_robustness.get('quality_score', 0):.1f}"
                                )
                            st.balloons()
                        else:
                            st.error("❌ A matriz global não retornou resultados válidos")
                    else:
                        st.info(f"📊 Executando backtest para {bt_symbol} no período de {period_days} dias...")

                        results = backtest_engine.run_backtest(
                            symbol=bt_symbol,
                            timeframe=bt_timeframe,
                            start_date=start_dt,
                            end_date=end_dt,
                            initial_balance=int(bt_initial_balance),
                            rsi_period=bt_rsi_period,
                            rsi_min=bt_rsi_min,
                            rsi_max=bt_rsi_max,
                            context_timeframe=execution_context_timeframe,
                            stop_loss_pct=stop_loss_pct,
                            take_profit_pct=take_profit_pct,
                            require_volume=enable_volume_filter,
                            require_trend=enable_trend_filter,
                            avoid_ranging=enable_avoid_ranging,
                            validation_split_pct=validation_split_pct if enable_oos_validation else 0.0,
                            walk_forward_windows=walk_forward_windows if enable_walk_forward else 0,
                            allowed_execution_setups=allowed_signal_directions,
                            ai_assist_mode=ai_assist_mode,
                            ai_min_win_probability=ai_min_win_probability,
                            ai_compare_baseline=ai_compare_baseline,
                            fast_mode=fast_backtest_mode,
                        )

                        if results and 'stats' in results:
                            st.session_state.backtest_scan_results = None
                            st.session_state.backtest_optimization_results = None
                            st.session_state.backtest_robustness_results = None
                            st.session_state.backtest_results = results
                            st.success("✅ Backtest concluído com sucesso!")
                            if results.get('saved_run_id'):
                                st.caption(f"Backtest salvo no banco com ID #{results['saved_run_id']}")
                            st.balloons()
                        else:
                            st.error("❌ Backtest não retornou resultados válidos")

                except Exception as e:
                    error_msg = str(e)
                    st.error(f"❌ Erro durante o backtest: {error_msg}")

                    # Mensagens de ajuda específicas
                    if "Dados insuficientes" in error_msg:
                        st.warning("⚠️ **Solução**: Tente um período maior (mínimo 7 dias) ou um timeframe menor")
                    elif "Ainda nao ha historico suficiente da WebSocket publica" in error_msg:
                        st.warning(
                            "⚠️ **Solução**: esta aba agora usa apenas candles acumulados da mesma WebSocket pública da dashboard. "
                            "Reduza o período solicitado ou deixe a dashboard acumular mais histórico antes de rodar."
                        )
                    elif "API" in error_msg or "connection" in error_msg.lower():
                        st.warning("⚠️ **Solução**: Verifique sua conexão com a internet e tente novamente")
                    elif "Rate limit" in error_msg or "limit" in error_msg.lower():
                        st.warning("⚠️ **Solução**: Aguarde alguns minutos antes de tentar novamente")
                    else:
                        st.info("💡 **Dicas**:\n- Tente um período menor\n- Verifique se o par selecionado está disponível\n- Aguarde alguns segundos e tente novamente")

                    # Log do erro para debug
                    with st.expander("🔍 Detalhes técnicos (para debug)"):
                        st.code(error_msg)

        # Display results if available
        if st.session_state.backtest_results:
            results = st.session_state.backtest_results
            stats = results['stats']
            result_meta = results.get('meta', {})
            result_symbol = result_meta.get('symbol', bt_symbol)
            result_timeframe = result_meta.get('timeframe', bt_timeframe)
            result_strategy_version = result_meta.get('strategy_version')
            result_rsi_min = result_meta.get('rsi_min', bt_rsi_min)
            result_rsi_max = result_meta.get('rsi_max', bt_rsi_max)
            result_ai_mode = str(result_meta.get('ai_assist_mode') or "disabled")
            result_ai_min_prob = float(result_meta.get('ai_min_win_probability', ai_min_win_probability) or 0.0)
            ai_summary = results.get('ai_summary') or {}
            ai_comparison = results.get('ai_comparison') or {}
            scan_results = st.session_state.get('backtest_scan_results')
            optimization_results = st.session_state.get('backtest_optimization_results')
            robustness_results = st.session_state.get('backtest_robustness_results')

            st.markdown("---")
            st.subheader("📊 Resultados do Backtest")
            st.caption(f"Cenário exibido: {result_symbol} {result_timeframe}")
            if result_strategy_version:
                st.caption(f"Versão da estratégia: {result_strategy_version}")
            result_data_source = str(result_meta.get('data_source') or "-")
            data_source_label = {
                "shared_websocket_buffer": "buffer WebSocket compartilhado da dashboard",
                "persisted_public_websocket_db": "histórico persistido da WebSocket pública no banco",
            }.get(result_data_source, result_data_source)
            st.caption(f"Fonte de dados: {data_source_label}")
            if result_ai_mode in {"shadow", "filter"}:
                st.caption(
                    f"IA integrada: {result_ai_mode} | piso {result_ai_min_prob:.2f} | "
                    f"modelo {ai_summary.get('latest_model_version') or '-'}"
                )
            score_pct = calculate_backtest_score_pct(stats)
            result_view_mode = st.radio(
                "Visão do Resultado",
                options=["Resumo", "Validação", "Execução", "Governança", "Trades"],
                horizontal=True,
                key="bt_result_view_mode",
                help="Renderização otimizada: apenas o bloco selecionado é montado neste ciclo.",
            )
            show_summary_view = result_view_mode == "Resumo"
            show_validation_view = result_view_mode == "Validação"
            show_execution_view = result_view_mode == "Execução"
            show_governance_view = result_view_mode == "Governança"
            show_trades_view = result_view_mode == "Trades"
            st.caption("Modo leve: a dashboard monta só o grupo selecionado para reduzir travamentos.")

            market_state_summary = results.get('market_state_summary') or stats.get('market_state_breakdown') or []
            execution_mode_summary = results.get('execution_mode_summary') or stats.get('execution_mode_breakdown') or []
            active_strategy_profile = get_cached_active_strategy_profile(result_symbol, result_timeframe)
            promotion_readiness = None
            if results.get('saved_run_id'):
                promotion_readiness = get_cached_backtest_run_promotion_readiness(results['saved_run_id'])
            if show_summary_view:
                strategy_col1, strategy_col2 = st.columns(2)
                with strategy_col1:
                    if active_strategy_profile:
                        active_market_states = active_strategy_profile.get('allowed_market_states') or []
                        active_market_state_label = ", ".join(active_market_states) or active_strategy_profile.get('market_state') or "-"
                        st.info(
                            f"Leitura ativa em paper: {active_market_state_label} "
                            f"| {active_strategy_profile.get('strategy_version')} "
                            f"| RSI {active_strategy_profile.get('rsi_min')}-{active_strategy_profile.get('rsi_max')}"
                        )
                    else:
                        st.info("Nenhuma leitura ativa em paper para este mercado/timeframe.")
                    if promotion_readiness:
                        ready_market_states = promotion_readiness.get("approved_market_states") or []
                        ready_market_state_label = ", ".join(ready_market_states) if ready_market_states else "-"
                        if promotion_readiness.get("ready"):
                            st.success(
                                f"Leitura apta para ativação em paper com base nos critérios mínimos de backtest. "
                                f"Estados aprovados: {ready_market_state_label}"
                            )
                        else:
                            reasons_text = "\n".join(f"- {reason}" for reason in promotion_readiness.get("reasons", []))
                            st.warning(f"Leitura ainda não apta para ativação em paper:\n{reasons_text}")
                with strategy_col2:
                    action_col1, action_col2 = st.columns(2)
                    with action_col1:
                        if results.get('saved_run_id') and st.button(
                            "🚀 Ativar Leitura em Paper",
                            key=f"promote_setup_{results.get('saved_run_id')}",
                            disabled=bool(promotion_readiness and not promotion_readiness.get("ready")),
                        ):
                            promoted = db.promote_backtest_run(
                                results['saved_run_id'],
                                notes="Ativado em paper via dashboard",
                            )
                            if promoted:
                                clear_dashboard_data_caches()
                                promoted_states = promoted.get('allowed_market_states') or []
                                state_label = ", ".join(promoted_states) or promoted.get('market_state') or "-"
                                st.success(f"Leitura ativa em paper: {state_label} | {promoted.get('strategy_version')}")
                                st.rerun()
                            else:
                                st.error("Não foi possível ativar a leitura atual em paper.")
                    with action_col2:
                        if active_strategy_profile and st.button(
                            "⛔ Desligar Ativo",
                            key=f"disable_setup_{active_strategy_profile.get('id')}",
                        ):
                            db.deactivate_strategy_profile(
                                active_strategy_profile['id'],
                                reason="Desativado via dashboard",
                            )
                            clear_dashboard_data_caches()
                            st.warning("Leitura ativa desativada.")
                            st.rerun()

                dominant_market_state = market_state_summary[0] if market_state_summary else {}
                dominant_execution_mode = execution_mode_summary[0] if execution_mode_summary else {}
                objective_check = results.get("objective_check") or {}
                approved_market_states = objective_check.get("approved_market_states") or []
                approved_market_state_label = ", ".join(approved_market_states) if approved_market_states else "-"
                if market_state_summary or objective_check:
                    st.markdown("### 🧭 Leitura Operacional")
                    market_col1, market_col2, market_col3, market_col4 = st.columns(4)
                    with market_col1:
                        st.metric("Estado Dominante", dominant_market_state.get("market_state", "-"))
                    with market_col2:
                        st.metric("Estados Aprovados", approved_market_state_label)
                    with market_col3:
                        st.metric("Modo Dominante", dominant_execution_mode.get("execution_mode", "-"))
                    with market_col4:
                        st.metric(
                            "PF do Estado Líder",
                            f"{float(dominant_market_state.get('profit_factor', 0.0) or 0.0):.2f}",
                        )

                    st.caption(
                        "A leitura do mercado mostra o contexto que mais apareceu e o subconjunto que ficou elegível para promoção real."
                    )
                if objective_check:
                    st.markdown("### 🎯 Checagem Objetiva de Sobrevivência")
                    obj_col1, obj_col2, obj_col3, obj_col4 = st.columns(4)
                    with obj_col1:
                        st.metric("Status", str(objective_check.get("status", "-")).upper())
                    with obj_col2:
                        st.metric("Score", f"{float(objective_check.get('objective_score', 0.0) or 0.0):.2f}")
                    with obj_col3:
                        st.metric("Grade", objective_check.get("objective_grade", "-"))
                    with obj_col4:
                        st.metric("Estado Foco", objective_check.get("recommended_market_state") or "-")

                    status_value = str(objective_check.get("status", "")).lower()
                    status_message = (
                        f"Objetivo de robustez: {status_value.upper()} | "
                        f"Score {float(objective_check.get('objective_score', 0.0) or 0.0):.2f} "
                        f"(Grade {objective_check.get('objective_grade', '-')})"
                    )
                    if status_value == "approved":
                        st.success(status_message)
                    elif status_value == "candidate":
                        st.warning(status_message)
                    else:
                        st.error(status_message)

                    objective_checks = objective_check.get("checks") or []
                    if objective_checks:
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "Critério": item.get("name"),
                                        "Valor": item.get("value"),
                                        "Meta": item.get("target"),
                                        "Passou": "✅" if item.get("passed") else "❌",
                                        "Peso": item.get("weight"),
                                        "Hard": "sim" if item.get("hard") else "não",
                                    }
                                    for item in objective_checks
                                ]
                            ),
                            width="stretch",
                            hide_index=True,
                        )

                    objective_col1, objective_col2 = st.columns(2)
                    with objective_col1:
                        blockers = objective_check.get("blockers") or []
                        if blockers:
                            st.caption("Blockers")
                            st.write("\n".join(f"- {item}" for item in blockers))
                        else:
                            st.caption("Blockers")
                            st.info("Nenhum blocker crítico.")
                    with objective_col2:
                        warnings_list = objective_check.get("warnings") or []
                        if warnings_list:
                            st.caption("Warnings")
                            st.write("\n".join(f"- {item}" for item in warnings_list))
                        else:
                            st.caption("Warnings")
                            st.info("Sem alertas adicionais.")

                    market_state_candidates = objective_check.get("market_state_candidates") or []
                    if market_state_candidates:
                        st.caption("Ranking de Estados de Mercado")
                        st.dataframe(pd.DataFrame(market_state_candidates), width="stretch", hide_index=True)

                    reading_candidates = objective_check.get("reading_candidates") or objective_check.get("setup_candidates") or []
                    if reading_candidates:
                        st.caption("Ranking de Perfis de Leitura")
                        st.dataframe(pd.DataFrame(reading_candidates), width="stretch", hide_index=True)

                    next_actions = objective_check.get("next_actions") or []
                    if next_actions:
                        st.caption("Próximas Ações")
                        st.write("\n".join(f"- {item}" for item in next_actions))

            if show_governance_view:
                try:
                    edge_summary = get_cached_edge_monitor_summary(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        strategy_version=result_strategy_version,
                    )
                    st.markdown("### 📡 Edge Live vs Backtest")
                    edge_col1, edge_col2, edge_col3, edge_col4 = st.columns(4)
                    with edge_col1:
                        st.metric("Baseline PF", f"{edge_summary.get('baseline_profit_factor', 0):.2f}")
                    with edge_col2:
                        st.metric("Paper PF", f"{edge_summary.get('paper_profit_factor', 0):.2f}")
                    with edge_col3:
                        st.metric("Paper Trades", edge_summary.get('paper_closed_trades', 0))
                    with edge_col4:
                        st.metric("Alinhamento PF", f"{edge_summary.get('profit_factor_alignment_pct', 0):.1f}%")

                    edge_message = (
                        f"{edge_summary.get('baseline_source', 'Baseline')} retorno {edge_summary.get('baseline_return_pct', 0):.2f}% "
                        f"| Paper acumulado {edge_summary.get('paper_total_result_pct', 0):.2f}% "
                        f"| {edge_summary.get('status_message')}"
                    )
                    edge_status = edge_summary.get('status')
                    if edge_status == "aligned":
                        st.success(edge_message)
                    elif edge_status in {"degraded", "watchlist"}:
                        st.warning(edge_message)
                    else:
                        st.info(edge_message)
                except Exception as edge_error:
                    st.info(f"Edge monitor indisponivel: {edge_error}")

            if show_governance_view:
                try:
                    governance_summary = get_cached_strategy_governance_summary(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        active_only=False,
                        limit=10,
                    )
                    governance_counts = governance_summary.get('counts', {})
                    governance_profiles = governance_summary.get('profiles', [])

                    st.markdown("### 🧭 Governança Operacional")
                    gov_col1, gov_col2, gov_col3, gov_col4, gov_col5 = st.columns(5)
                    with gov_col1:
                        st.metric("Aprovados", governance_counts.get('approved', 0))
                    with gov_col2:
                        st.metric("Observando", governance_counts.get('observing', 0))
                    with gov_col3:
                        st.metric("Bloqueados", governance_counts.get('blocked', 0))
                    with gov_col4:
                        st.metric("Prontos p/ Paper", governance_counts.get('ready_for_paper', 0))
                    with gov_col5:
                        st.metric("Precisam Ajuste", governance_counts.get('needs_work', 0))

                    if governance_profiles:
                        governance_df = pd.DataFrame(governance_profiles)
                        governance_df = governance_df[
                            [
                                'strategy_version',
                                'profile_status',
                                'governance_status',
                                'governance_mode',
                                'alignment_status',
                                'paper_closed_trades',
                                'baseline_profit_factor',
                                'paper_profit_factor',
                                'governance_message',
                            ]
                        ].rename(
                            columns={
                                'strategy_version': 'Versao',
                                'profile_status': 'Perfil',
                                'governance_status': 'Status',
                                'governance_mode': 'Modo',
                                'alignment_status': 'Alignment',
                                'paper_closed_trades': 'Paper Trades',
                                'baseline_profit_factor': 'PF Baseline',
                                'paper_profit_factor': 'PF Paper',
                                'governance_message': 'Mensagem',
                            }
                        )
                        st.dataframe(governance_df, width="stretch", hide_index=True)

                    adaptive_governance = get_cached_governance_evaluation(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        strategy_version=result_strategy_version,
                    )
                    regime_baselines = get_cached_setup_regime_baselines(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        strategy_version=result_strategy_version,
                    )
                    alignment_history = get_cached_alignment_metrics(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        strategy_version=result_strategy_version,
                        limit=5,
                    )
                    governance_history = get_cached_governance_history(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        strategy_version=result_strategy_version,
                        limit=10,
                    )

                    st.markdown("### Governança Adaptativa")
                    adaptive_col1, adaptive_col2, adaptive_col3, adaptive_col4 = st.columns(4)
                    with adaptive_col1:
                        st.metric("Status", adaptive_governance.get("governance_status", "-"))
                    with adaptive_col2:
                        st.metric("Modo", adaptive_governance.get("governance_mode", "-"))
                    with adaptive_col3:
                        st.metric("Alignment", adaptive_governance.get("alignment_status", "-"))
                    with adaptive_col4:
                        st.metric("Score", f"{adaptive_governance.get('quality_score', 0):.1f}")

                    st.caption(
                        f"Acao: {adaptive_governance.get('action', '-')} | "
                        f"Motivo: {adaptive_governance.get('action_reason', '-')}"
                    )
                    st.caption(
                        f"Regimes aprovados: {', '.join(adaptive_governance.get('allowed_regimes', [])) or '-'} | "
                        f"Reduzidos: {', '.join(adaptive_governance.get('reduced_regimes', [])) or '-'} | "
                        f"Bloqueados: {', '.join(adaptive_governance.get('blocked_regimes', [])) or '-'}"
                    )

                    if regime_baselines:
                        regime_df = pd.DataFrame(regime_baselines)[
                            [
                                'regime',
                                'performance_status',
                                'baseline_trade_count',
                                'baseline_profit_factor',
                                'baseline_expectancy_pct',
                                'baseline_win_rate',
                                'total_return_pct',
                            ]
                        ].rename(
                            columns={
                                'regime': 'Regime',
                                'performance_status': 'Status',
                                'baseline_trade_count': 'Trades',
                                'baseline_profit_factor': 'PF',
                                'baseline_expectancy_pct': 'Expectancy %',
                                'baseline_win_rate': 'Win Rate %',
                                'total_return_pct': 'Retorno %',
                            }
                        )
                        st.dataframe(regime_df, width="stretch", hide_index=True)

                    if alignment_history:
                        alignment_df = pd.DataFrame(alignment_history)[
                            [
                                'regime',
                                'alignment_status',
                                'paper_trade_count',
                                'paper_profit_factor',
                                'paper_pf_alignment_pct',
                                'live_trade_count',
                                'live_pf_alignment_pct',
                                'created_at',
                            ]
                        ].rename(
                            columns={
                                'regime': 'Regime',
                                'alignment_status': 'Status',
                                'paper_trade_count': 'Paper Trades',
                                'paper_profit_factor': 'PF Paper',
                                'paper_pf_alignment_pct': 'PF Paper %',
                                'live_trade_count': 'Live Trades',
                                'live_pf_alignment_pct': 'PF Live %',
                                'created_at': 'Snapshot',
                            }
                        )
                        st.dataframe(alignment_df, width="stretch", hide_index=True)

                    if governance_history:
                        governance_history_df = pd.DataFrame(governance_history)[
                            [
                                'regime',
                                'previous_status',
                                'governance_status',
                                'governance_mode',
                                'alignment_status',
                                'action_reason',
                                'created_at',
                            ]
                        ].rename(
                            columns={
                                'regime': 'Regime',
                                'previous_status': 'Status Anterior',
                                'governance_status': 'Status Atual',
                                'governance_mode': 'Modo',
                                'alignment_status': 'Alignment',
                                'action_reason': 'Motivo',
                                'created_at': 'Quando',
                            }
                        )
                        st.dataframe(governance_history_df, width="stretch", hide_index=True)
                except Exception as governance_error:
                    st.info(f"Governança operacional indisponível: {governance_error}")

            if show_governance_view:
                try:
                    recent_strategy_evaluations = get_cached_strategy_evaluations(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        strategy_version=result_strategy_version,
                        limit=5,
                    )
                    if not recent_strategy_evaluations:
                        recent_strategy_evaluations = get_cached_strategy_evaluations(
                            symbol=result_symbol,
                            timeframe=result_timeframe,
                            limit=5,
                        )
                    evaluation_overview = get_cached_strategy_evaluation_overview(
                        symbol=result_symbol,
                        timeframe=result_timeframe,
                        limit=10,
                    )

                    st.markdown("### Strategy Evaluations")
                    latest_evaluation = recent_strategy_evaluations[0] if recent_strategy_evaluations else None
                    if latest_evaluation:
                        eval_col1, eval_col2, eval_col3, eval_col4 = st.columns(4)
                        with eval_col1:
                            st.metric("Score Atual", f"{latest_evaluation.get('quality_score', 0):.1f}")
                        with eval_col2:
                            st.metric("Origem", latest_evaluation.get("evaluation_type", "-"))
                        with eval_col3:
                            st.metric("Edge", latest_evaluation.get("edge_status", "-"))
                        with eval_col4:
                            st.metric("Governanca", latest_evaluation.get("governance_status", "-"))

                        st.caption(
                            f"Snapshot mais recente: {latest_evaluation.get('created_at_br', '-')}"
                            f" | PF Backtest {latest_evaluation.get('avg_profit_factor', 0):.2f}"
                            f" | PF OOS {latest_evaluation.get('avg_out_of_sample_profit_factor', 0):.2f}"
                            f" | PF Paper {latest_evaluation.get('paper_profit_factor', 0):.2f}"
                        )
                        st.dataframe(
                            build_strategy_evaluation_display_df(recent_strategy_evaluations),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.info("Ainda nao existem snapshots em strategy_evaluations para este mercado/timeframe.")

                    overview_counts = evaluation_overview.get("governance_counts", {})
                    edge_counts = evaluation_overview.get("edge_counts", {})
                    overview_col1, overview_col2, overview_col3, overview_col4 = st.columns(4)
                    with overview_col1:
                        st.metric("Perfis Monitorados", evaluation_overview.get("total_strategies", 0))
                    with overview_col2:
                        st.metric("Aprovados", overview_counts.get("approved", 0))
                    with overview_col3:
                        st.metric("Bloqueados", overview_counts.get("blocked", 0))
                    with overview_col4:
                        st.metric("Edge Degradado", edge_counts.get("degraded", 0))

                    if evaluation_overview.get("rows"):
                        st.caption("Ultimo snapshot por estrategia neste mercado/timeframe.")
                        st.dataframe(
                            build_strategy_evaluation_display_df(evaluation_overview["rows"]),
                            width="stretch",
                            hide_index=True,
                        )
                except Exception as evaluation_error:
                    st.info(f"Strategy evaluations indisponiveis: {evaluation_error}")

            if show_validation_view and optimization_results and optimization_results.get('rows'):
                optimization_summary = optimization_results.get('summary', {})
                best_optimization = optimization_results.get('best') or {}

                st.markdown("### ⚡ Ranking de Otimização")
                opt_col1, opt_col2, opt_col3, opt_col4 = st.columns(4)
                with opt_col1:
                    st.metric("Testes", optimization_summary.get('completed_tests', 0))
                with opt_col2:
                    st.metric("Candidatos Robustos", optimization_summary.get('passed_candidates', 0))
                with opt_col3:
                    st.metric("Melhor Score", f"{optimization_summary.get('best_quality_score', 0):.1f}")
                with opt_col4:
                    st.metric("Métrica", optimization_summary.get('optimization_metric', '-'))

                if best_optimization:
                    st.info(
                        f"Melhor configuração: RSI {best_optimization.get('rsi_min')}-{best_optimization.get('rsi_max')} | "
                        f"Score {best_optimization.get('quality_score', 0):.1f} | "
                        f"OOS PF {best_optimization.get('oos_profit_factor', 0):.2f} | "
                        f"WF Pass Rate {best_optimization.get('walk_forward_pass_rate_pct', 0):.1f}%"
                    )

                optimization_df = pd.DataFrame(optimization_results['rows'])
                optimization_df = optimization_df[
                    [
                        'rsi_min',
                        'rsi_max',
                        'metric_value',
                        'quality_score',
                        'total_return_pct',
                        'profit_factor',
                        'oos_return_pct',
                        'oos_profit_factor',
                        'walk_forward_pass_rate_pct',
                        'robust_candidate',
                    ]
                ]
                optimization_df.columns = [
                    'RSI Min',
                    'RSI Max',
                    'Métrica',
                    'Score',
                    'Retorno %',
                    'PF',
                    'OOS %',
                    'OOS PF',
                    'WF Pass Rate %',
                    'Robusto',
                ]
                st.dataframe(optimization_df, width='stretch', hide_index=True)

                if optimization_results.get('failed_runs'):
                    with st.expander("Falhas da Otimização"):
                        st.dataframe(pd.DataFrame(optimization_results['failed_runs']), width='stretch', hide_index=True)

                st.caption("O detalhamento abaixo corresponde à melhor configuração de RSI encontrada.")

            if show_validation_view and scan_results and scan_results.get('rows'):
                scan_summary = scan_results.get('summary', {})
                best_scan = scan_results.get('best') or {}

                st.markdown("### 🧭 Ranking Comparativo")
                scan_col1, scan_col2, scan_col3, scan_col4 = st.columns(4)
                with scan_col1:
                    st.metric("Cenários", scan_summary.get('completed_runs', 0))
                with scan_col2:
                    st.metric("OOS Aprovados", scan_summary.get('oos_passed_runs', 0))
                with scan_col3:
                    st.metric("WF Aprovados", scan_summary.get('walk_forward_passed_runs', 0))
                with scan_col4:
                    st.metric("Melhor Score", f"{scan_summary.get('best_quality_score', 0):.1f}")

                if best_scan:
                    st.info(
                        f"Melhor combinação: {best_scan.get('symbol')} {best_scan.get('timeframe')} | "
                        f"Score {best_scan.get('quality_score', 0):.1f} | "
                        f"OOS PF {best_scan.get('oos_profit_factor', 0):.2f} | "
                        f"WF Pass Rate {best_scan.get('walk_forward_pass_rate_pct', 0):.1f}%"
                    )

                scan_df = pd.DataFrame(scan_results['rows'])
                scan_df = scan_df[
                    [
                        'symbol',
                        'timeframe',
                        'quality_score',
                        'total_return_pct',
                        'profit_factor',
                        'oos_return_pct',
                        'oos_profit_factor',
                        'walk_forward_pass_rate_pct',
                        'max_drawdown',
                        'total_trades',
                    ]
                ]
                scan_df.columns = [
                    'Símbolo',
                    'Timeframe',
                    'Score',
                    'Retorno %',
                    'PF',
                    'OOS %',
                    'OOS PF',
                    'WF Pass Rate %',
                    'Drawdown %',
                    'Trades',
                ]
                st.dataframe(scan_df, width='stretch', hide_index=True)

                if scan_results.get('failed_runs'):
                    with st.expander("Falhas do Scan"):
                        st.dataframe(pd.DataFrame(scan_results['failed_runs']), width='stretch', hide_index=True)

                st.caption("O detalhamento abaixo corresponde ao melhor cenário encontrado no scan.")

            if show_validation_view and robustness_results and robustness_results.get('rows'):
                robustness_summary = robustness_results.get('summary', {})
                best_robustness = robustness_results.get('best') or {}
                anchor_end_date = robustness_summary.get('anchor_end_date')
                anchor_end_label = (
                    pd.Timestamp(anchor_end_date).strftime("%d/%m/%Y")
                    if anchor_end_date
                    else "-"
                )

                st.markdown("### 🧱 Matriz de Robustez Global")
                rob_col1, rob_col2, rob_col3, rob_col4, rob_col5 = st.columns(5)
                with rob_col1:
                    st.metric("Cenários", robustness_summary.get('completed_runs', 0))
                with rob_col2:
                    st.metric("Score Global", f"{robustness_summary.get('robustness_score', 0):.1f}")
                with rob_col3:
                    st.metric("Positivos", robustness_summary.get('profitable_runs', 0))
                with rob_col4:
                    st.metric("OOS Aprovados", robustness_summary.get('oos_passed_runs', 0))
                with rob_col5:
                    st.metric("Robustos", robustness_summary.get('robust_runs', 0))

                rob_col6, rob_col7, rob_col8, rob_col9, rob_col10 = st.columns(5)
                with rob_col6:
                    st.metric("PF Mediano", f"{robustness_summary.get('median_profit_factor', 0):.2f}")
                with rob_col7:
                    st.metric("OOS PF Med.", f"{robustness_summary.get('median_oos_profit_factor', 0):.2f}")
                with rob_col8:
                    st.metric("Famílias", robustness_summary.get('families_covered', 0))
                with rob_col9:
                    st.metric("Horizontes", robustness_summary.get('horizons_covered', 0))
                with rob_col10:
                    st.metric("Drawdown Pior", f"{robustness_summary.get('worst_drawdown', 0):.2f}%")

                if best_robustness:
                    st.info(
                        f"Modo: {robustness_summary.get('family_overlay_mode_label', '-')} | "
                        f"Âncora: {anchor_end_label} | "
                        f"Melhor cenário: {best_robustness.get('symbol')} {best_robustness.get('horizon_days')}d "
                        f"| Score {best_robustness.get('quality_score', 0):.1f}"
                    )

                breakdown_col1, breakdown_col2 = st.columns(2)
                with breakdown_col1:
                    st.caption("Resumo por família")
                    st.dataframe(
                        build_backtest_robustness_breakdown_display_df(
                            robustness_results.get('family_breakdown'),
                            group_label="Família",
                        ),
                        width='stretch',
                        hide_index=True,
                    )
                with breakdown_col2:
                    st.caption("Resumo por horizonte")
                    st.dataframe(
                        build_backtest_robustness_breakdown_display_df(
                            robustness_results.get('horizon_breakdown'),
                            group_label="Horizonte",
                        ),
                        width='stretch',
                        hide_index=True,
                    )

                st.caption("Matriz completa ordenada pelo cenário mais robusto.")
                st.dataframe(
                    build_backtest_robustness_matrix_display_df(robustness_results['rows']),
                    width='stretch',
                    hide_index=True,
                )

                if robustness_results.get('failed_runs'):
                    with st.expander("Falhas da Matriz Global"):
                        st.dataframe(pd.DataFrame(robustness_results['failed_runs']), width='stretch', hide_index=True)

                st.caption("O detalhamento abaixo corresponde ao melhor cenário encontrado na matriz.")

            if show_summary_view:
                # Performance Overview
                col1, col2, col3, col4 = st.columns(4)

                with col1:
                    st.metric(
                        "💰 Retorno Total", 
                        f"{stats['total_return_pct']:.2f}%",
                        delta=f"${stats['final_balance'] - stats['initial_balance']:,.2f}"
                    )
                with col2:
                    st.metric("🔢 Total de Trades", stats['total_trades'])
                with col3:
                    st.metric("🎯 Taxa de Acerto", f"{stats['win_rate']:.1f}%")
                with col4:
                    st.metric("📉 Max Drawdown", f"-{stats['max_drawdown']:.2f}%")

                # Additional metrics row
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("📈 Sharpe Ratio", f"{stats['sharpe_ratio']:.2f}")
                with col2:
                    st.metric("💹 Profit Factor", f"{stats.get('profit_factor', 0):.2f}")
                with col3:
                    st.metric("✅ Trades Vencedores", stats['winning_trades'])
                with col4:
                    st.metric("❌ Trades Perdedores", stats['losing_trades'])

                render_backtest_portfolio_section(
                    results=results,
                    stats=stats,
                    result_symbol=result_symbol,
                    result_timeframe=result_timeframe,
                )

            if show_execution_view:
                signal_pipeline_stats = results.get('signal_pipeline_stats') or {
                    'candidate_count': results.get('candidate_count', 0),
                    'approved_count': results.get('approved_count', 0),
                    'blocked_count': results.get('blocked_count', 0),
                    'approval_rate_pct': results.get('approval_rate_pct', 0.0),
                    'block_reason_counts': results.get('block_reason_counts', {}),
                    'regime_counts': results.get('regime_counts', {}),
                    'structure_state_counts': results.get('structure_state_counts', {}),
                    'confirmation_state_counts': results.get('confirmation_state_counts', {}),
                    'entry_quality_counts': results.get('entry_quality_counts', {}),
                    'market_state_counts': results.get('market_state_counts', {}),
                    'market_state_approved_counts': results.get('market_state_approved_counts', {}),
                    'market_state_blocked_counts': results.get('market_state_blocked_counts', {}),
                    'execution_mode_counts': results.get('execution_mode_counts', {}),
                    'market_pattern_counts': results.get('market_pattern_counts', results.get('setup_type_counts', {})),
                    'market_pattern_approved_counts': results.get('market_pattern_approved_counts', results.get('setup_type_approved_counts', {})),
                    'market_pattern_blocked_counts': results.get('market_pattern_blocked_counts', results.get('setup_type_blocked_counts', {})),
                    'market_pattern_approval_rates': results.get('market_pattern_approval_rates', results.get('setup_type_approval_rates', {})),
                    'market_pattern_block_rates': results.get('market_pattern_block_rates', results.get('setup_type_block_rates', {})),
                }

            if show_execution_view:
                st.markdown("---")
                st.subheader("🧠 Pipeline de Sinais")

                pipeline_col1, pipeline_col2, pipeline_col3, pipeline_col4 = st.columns(4)
                with pipeline_col1:
                    st.metric("Candidatos", int(signal_pipeline_stats.get('candidate_count', 0) or 0))
                with pipeline_col2:
                    st.metric("Aprovados", int(signal_pipeline_stats.get('approved_count', 0) or 0))
                with pipeline_col3:
                    st.metric("Bloqueados", int(signal_pipeline_stats.get('blocked_count', 0) or 0))
                with pipeline_col4:
                    st.metric("Taxa de Aprovação", f"{float(signal_pipeline_stats.get('approval_rate_pct', 0.0) or 0.0):.2f}%")

                breakdown_col1, breakdown_col2, breakdown_col3 = st.columns(3)
                with breakdown_col1:
                    st.caption("Motivos de Bloqueio")
                    block_reason_counts = signal_pipeline_stats.get('block_reason_counts') or {}
                    if block_reason_counts:
                        st.dataframe(
                            pd.DataFrame(
                                [{"Motivo": reason, "Qtd": count} for reason, count in block_reason_counts.items()]
                            ),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.info("Nenhum bloqueio registrado neste backtest.")
                with breakdown_col2:
                    st.caption("Estrutura / Confirmação")
                    structure_state_counts = signal_pipeline_stats.get('structure_state_counts') or {}
                    confirmation_state_counts = signal_pipeline_stats.get('confirmation_state_counts') or {}
                    if structure_state_counts or confirmation_state_counts:
                        structure_rows = [
                            {"Tipo": "Estrutura", "Estado": state, "Qtd": count}
                            for state, count in structure_state_counts.items()
                        ]
                        confirmation_rows = [
                            {"Tipo": "Confirmação", "Estado": state, "Qtd": count}
                            for state, count in confirmation_state_counts.items()
                        ]
                        st.dataframe(
                            pd.DataFrame(structure_rows + confirmation_rows),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.info("Sem estados estruturais agregados para exibir.")
                with breakdown_col3:
                    st.caption("Qualidade da Entrada")
                    entry_quality_counts = signal_pipeline_stats.get('entry_quality_counts') or {}
                    if entry_quality_counts:
                        st.dataframe(
                            pd.DataFrame(
                                [{"Qualidade": quality, "Qtd": count} for quality, count in entry_quality_counts.items()]
                            ),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.info("Sem estatísticas de entrada para exibir.")

                side_regime_analytics = results.get("side_regime_analytics") or stats.get("side_regime_analytics") or []
                neutral_regime_analytics = results.get("neutral_regime_analytics") or stats.get("neutral_regime_analytics") or []
                if side_regime_analytics or neutral_regime_analytics:
                    st.markdown("### ⚖️ Long / Short / Neutral por Regime")
                    comparison_order = [
                        "long_in_trend_bull",
                        "long_in_trend_bear",
                        "short_in_trend_bear",
                        "short_in_trend_bull",
                    ]
                    comparison_rows = []
                    for comparison_key in comparison_order:
                        row = next((item for item in side_regime_analytics if item.get("comparison_key") == comparison_key), None)
                        if row:
                            comparison_rows.append(
                                {
                                    "Comparação": comparison_key,
                                    "Candidatos": row.get("candidate_signals", 0),
                                    "Aprovados": row.get("approved_signals", 0),
                                    "Approval Rate %": row.get("approval_rate_pct", 0.0),
                                    "Trades": row.get("trades", 0),
                                    "Retorno %": row.get("return_pct", 0.0),
                                    "PF": row.get("profit_factor", 0.0),
                                    "Win Rate %": row.get("win_rate", 0.0),
                                }
                            )
                    if comparison_rows:
                        st.dataframe(pd.DataFrame(comparison_rows), width="stretch", hide_index=True)

                    range_neutral = next(
                        (item for item in neutral_regime_analytics if item.get("comparison_key") == "neutral_blocks_in_range"),
                        None,
                    )
                    if range_neutral:
                        neutral_col1, neutral_col2, neutral_col3, neutral_col4 = st.columns(4)
                        with neutral_col1:
                            st.metric("Range Avaliado", int(range_neutral.get("evaluated_rows", 0) or 0))
                        with neutral_col2:
                            st.metric("Neutral em Range", int(range_neutral.get("neutral_outcomes", 0) or 0))
                        with neutral_col3:
                            st.metric("Neutral Rate %", f"{float(range_neutral.get('neutral_rate_pct', 0.0) or 0.0):.2f}")
                        with neutral_col4:
                            st.metric("Approval Rate Range %", f"{float(range_neutral.get('approval_rate_pct', 0.0) or 0.0):.2f}")

            if show_execution_view:
                if ai_summary:
                    st.markdown("---")
                    st.subheader("🤖 IA no Motor")

                    ai_col1, ai_col2, ai_col3, ai_col4 = st.columns(4)
                    with ai_col1:
                        st.metric("Modo", str(ai_summary.get("mode") or "disabled"))
                    with ai_col2:
                        st.metric("Scored", int(ai_summary.get("scored_count", 0) or 0))
                    with ai_col3:
                        st.metric("Bloqueados IA", int(ai_summary.get("blocked_count", 0) or 0))
                    with ai_col4:
                        st.metric("Prob. Média", f"{float(ai_summary.get('avg_win_probability', 0.0) or 0.0):.2f}")

                    ai_col5, ai_col6, ai_col7, ai_col8 = st.columns(4)
                    with ai_col5:
                        st.metric("Alta Qualidade", int(ai_summary.get("high_quality_count", 0) or 0))
                    with ai_col6:
                        st.metric("Média Qualidade", int(ai_summary.get("medium_quality_count", 0) or 0))
                    with ai_col7:
                        st.metric("Baixa Qualidade", int(ai_summary.get("low_quality_count", 0) or 0))
                    with ai_col8:
                        st.metric("Runtime", "carregado" if ai_summary.get("model_loaded") else "neutro")

                    if ai_summary.get("model_loaded"):
                        st.success(
                            f"Modelo ativo: {ai_summary.get('latest_model_version') or '-'} | "
                            f"approval rate IA {float(ai_summary.get('approval_rate_pct', 0.0) or 0.0):.2f}%"
                        )
                    else:
                        st.warning("A IA ficou neutra neste backtest porque o modelo não estava carregado.")

                    performance_by_quality_band = ai_summary.get("performance_by_quality_band") or {}
                    if performance_by_quality_band:
                        st.caption("Performance por faixa de qualidade da IA")
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "Faixa": quality_band,
                                        "Trades": metrics.get("trades", 0),
                                        "Wins": metrics.get("wins", 0),
                                        "Losses": metrics.get("losses", 0),
                                        "Win Rate %": metrics.get("win_rate", 0.0),
                                        "Net Profit": metrics.get("net_profit", 0.0),
                                        "Prob. Média": metrics.get("avg_win_probability", 0.0),
                                    }
                                    for quality_band, metrics in performance_by_quality_band.items()
                                ]
                            ),
                            width="stretch",
                            hide_index=True,
                        )

                    if ai_comparison:
                        st.caption("Comparação direta: baseline sem IA vs cenário assistido")
                        comparison_rows = [
                            {
                                "Cenário": "Baseline",
                                "Modo": ai_comparison.get("baseline", {}).get("mode"),
                                "Retorno %": ai_comparison.get("baseline", {}).get("total_return_pct"),
                                "PF": ai_comparison.get("baseline", {}).get("profit_factor"),
                                "Drawdown %": ai_comparison.get("baseline", {}).get("max_drawdown"),
                                "Trades": ai_comparison.get("baseline", {}).get("total_trades"),
                            },
                            {
                                "Cenário": "Assistido",
                                "Modo": ai_comparison.get("assisted", {}).get("mode"),
                                "Retorno %": ai_comparison.get("assisted", {}).get("total_return_pct"),
                                "PF": ai_comparison.get("assisted", {}).get("profit_factor"),
                                "Drawdown %": ai_comparison.get("assisted", {}).get("max_drawdown"),
                                "Trades": ai_comparison.get("assisted", {}).get("total_trades"),
                            },
                        ]
                        st.dataframe(pd.DataFrame(comparison_rows), width="stretch", hide_index=True)

                        delta = ai_comparison.get("delta") or {}
                        flag_map = ai_comparison.get("improvement_flags") or {}
                        delta_col1, delta_col2, delta_col3, delta_col4 = st.columns(4)
                        with delta_col1:
                            st.metric("Δ Retorno %", f"{float(delta.get('total_return_pct', 0.0) or 0.0):.2f}")
                        with delta_col2:
                            st.metric("Δ PF", f"{float(delta.get('profit_factor', 0.0) or 0.0):.2f}")
                        with delta_col3:
                            st.metric("Δ Drawdown %", f"{float(delta.get('max_drawdown', 0.0) or 0.0):.2f}")
                        with delta_col4:
                            st.metric("Δ Trades", int(delta.get("total_trades", 0) or 0))

                        if flag_map.get("return_improved") or flag_map.get("profit_factor_improved"):
                            st.success("A IA integrada melhorou pelo menos um dos indicadores principais neste cenário.")
                        else:
                            st.info("Neste cenário, a IA ainda não superou o baseline de forma clara.")

            if show_execution_view:
                risk_engine_summary = results.get('risk_engine_summary') or {}
                if risk_engine_summary:
                    st.caption("Risk Engine")
                    risk_col1, risk_col2, risk_col3 = st.columns(3)
                    with risk_col1:
                        st.metric("Bloqueados por Risco", int(risk_engine_summary.get('risk_blocked_count', 0) or 0))
                    with risk_col2:
                        st.metric("Size Reduzida", int(risk_engine_summary.get('reduced_size_count', 0) or 0))
                    with risk_col3:
                        st.metric("Modos de Risco", len(risk_engine_summary.get('risk_mode_counts') or {}))

                    risk_breakdown_col1, risk_breakdown_col2 = st.columns(2)
                    with risk_breakdown_col1:
                        st.caption("Motivos de Bloqueio por Risco")
                        risk_block_reason_counts = risk_engine_summary.get('risk_block_reason_counts') or {}
                        if risk_block_reason_counts:
                            st.dataframe(
                                pd.DataFrame(
                                    [{"Motivo": reason, "Qtd": count} for reason, count in risk_block_reason_counts.items()]
                                ),
                                width="stretch",
                                hide_index=True,
                            )
                        else:
                            st.info("Nenhum sinal foi bloqueado pela risk engine neste backtest.")
                    with risk_breakdown_col2:
                        st.caption("Performance por Risk Mode")
                        performance_by_risk_mode = risk_engine_summary.get('performance_by_risk_mode') or {}
                        if performance_by_risk_mode:
                            st.dataframe(
                                pd.DataFrame(
                                    [
                                        {
                                            "Risk Mode": mode,
                                            "Trades": metrics.get('trades', 0),
                                            "Net Profit": metrics.get('net_profit', 0.0),
                                            "Wins": metrics.get('wins', 0),
                                            "Losses": metrics.get('losses', 0),
                                            "Win Rate %": metrics.get('win_rate', 0.0),
                                        }
                                        for mode, metrics in performance_by_risk_mode.items()
                                    ]
                                ),
                                width="stretch",
                                hide_index=True,
                            )
                        else:
                            st.info("Sem trades suficientes para agregar por risk mode.")

                regime_summary = results.get('regime_summary') or stats.get('regime_breakdown') or []
                market_state_summary = results.get('market_state_summary') or stats.get('market_state_breakdown') or []
                execution_mode_summary = results.get('execution_mode_summary') or stats.get('execution_mode_breakdown') or []
                market_pattern_summary = results.get('market_pattern_summary') or results.get('setup_type_summary') or stats.get('market_pattern_breakdown') or stats.get('setup_type_breakdown') or []
                regime_counts = signal_pipeline_stats.get('regime_counts') or {}
                market_state_counts = signal_pipeline_stats.get('market_state_counts') or {}
                market_state_approved_counts = signal_pipeline_stats.get('market_state_approved_counts') or {}
                market_state_blocked_counts = signal_pipeline_stats.get('market_state_blocked_counts') or {}
                execution_mode_counts = signal_pipeline_stats.get('execution_mode_counts') or {}
                market_pattern_counts = signal_pipeline_stats.get('market_pattern_counts') or signal_pipeline_stats.get('setup_type_counts') or {}
                market_pattern_approved_counts = signal_pipeline_stats.get('market_pattern_approved_counts') or signal_pipeline_stats.get('setup_type_approved_counts') or {}
                market_pattern_blocked_counts = signal_pipeline_stats.get('market_pattern_blocked_counts') or signal_pipeline_stats.get('setup_type_blocked_counts') or {}
                market_pattern_approval_rates = signal_pipeline_stats.get('market_pattern_approval_rates') or signal_pipeline_stats.get('setup_type_approval_rates') or {}
                market_pattern_block_rates = signal_pipeline_stats.get('market_pattern_block_rates') or signal_pipeline_stats.get('setup_type_block_rates') or {}
                if regime_counts:
                    st.caption("Regimes Detectados no Pipeline")
                    st.dataframe(
                        pd.DataFrame(
                            [{"Regime": regime, "Qtd": count} for regime, count in regime_counts.items()]
                        ),
                        width="stretch",
                        hide_index=True,
                    )
                if regime_summary:
                    st.caption("Performance por Regime")
                    st.dataframe(
                        pd.DataFrame(regime_summary),
                        width="stretch",
                        hide_index=True,
                    )
                if market_state_counts:
                    st.caption("Entradas por Estado de Mercado")
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Estado": market_state,
                                    "Candidatos": count,
                                    "Aprovados": int(market_state_approved_counts.get(market_state, 0) or 0),
                                    "Bloqueados": int(market_state_blocked_counts.get(market_state, 0) or 0),
                                }
                                for market_state, count in market_state_counts.items()
                            ]
                        ),
                        width="stretch",
                        hide_index=True,
                    )
                if market_state_summary:
                    st.caption("Performance por Estado de Mercado")
                    st.dataframe(
                        pd.DataFrame(market_state_summary),
                        width="stretch",
                        hide_index=True,
                    )
                if execution_mode_counts:
                    st.caption("Execução por Modo Operacional")
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {"Modo": execution_mode, "Qtd": count}
                                for execution_mode, count in execution_mode_counts.items()
                            ]
                        ),
                        width="stretch",
                        hide_index=True,
                    )
                if execution_mode_summary:
                    st.caption("Performance por Modo Operacional")
                    st.dataframe(
                        pd.DataFrame(execution_mode_summary),
                        width="stretch",
                        hide_index=True,
                    )
                if market_pattern_counts:
                    st.caption("Entradas por Perfil de Leitura")
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Perfil de Leitura": market_pattern,
                                    "Candidatos": count,
                                    "Aprovados": int(market_pattern_approved_counts.get(market_pattern, 0) or 0),
                                    "Bloqueados": int(market_pattern_blocked_counts.get(market_pattern, 0) or 0),
                                    "Taxa de Aprovação %": float(market_pattern_approval_rates.get(market_pattern, 0.0) or 0.0),
                                    "Taxa de Bloqueio %": float(market_pattern_block_rates.get(market_pattern, 0.0) or 0.0),
                                }
                                for market_pattern, count in market_pattern_counts.items()
                            ]
                        ),
                        width="stretch",
                        hide_index=True,
                    )
                if market_pattern_summary:
                    st.caption("Performance por Perfil de Leitura")
                    st.dataframe(
                        pd.DataFrame(market_pattern_summary),
                        width="stretch",
                        hide_index=True,
                    )

            if show_execution_view:
                position_management_summary = results.get('position_management_summary') or {}
                exit_type_summary = results.get('exit_type_summary') or stats.get('exit_type_breakdown') or []
                entry_quality_summary = results.get('entry_quality_summary') or stats.get('entry_quality_breakdown') or []
                risk_mode_summary = results.get('risk_mode_summary') or stats.get('risk_mode_breakdown') or []
                signal_audit_summary = results.get('signal_audit_summary') or {}
                if position_management_summary or exit_type_summary:
                    st.markdown("---")
                    st.subheader("🛡️ Gestão da Posição")

                    mgmt_col1, mgmt_col2, mgmt_col3, mgmt_col4 = st.columns(4)
                    with mgmt_col1:
                        st.metric("Break-even", int(position_management_summary.get('break_even_activated_count', 0) or 0))
                    with mgmt_col2:
                        st.metric("Trailing", int(position_management_summary.get('trailing_activated_count', 0) or 0))
                    with mgmt_col3:
                        st.metric("Proteção Pós-Pump", int(position_management_summary.get('post_pump_protection_count', 0) or 0))
                    with mgmt_col4:
                        st.metric(
                            "MFE / MAE Médio",
                            f"{float(position_management_summary.get('avg_mfe_pct', 0.0) or 0.0):.2f}% / "
                            f"{float(position_management_summary.get('avg_mae_pct', 0.0) or 0.0):.2f}%"
                        )

                    exit_counts = stats.get('exit_reason_counts') or {}
                    if exit_counts:
                        exit_rows = [{"Saída": reason, "Qtd": count} for reason, count in exit_counts.items()]
                        st.caption("Saídas por Tipo")
                        st.dataframe(pd.DataFrame(exit_rows), width="stretch", hide_index=True)

                    if exit_type_summary:
                        st.caption("Performance por Tipo de Saída")
                        st.dataframe(pd.DataFrame(exit_type_summary), width="stretch", hide_index=True)

                if entry_quality_summary or risk_mode_summary or signal_audit_summary:
                    st.markdown("---")
                    st.subheader("🧬 Analytics Agregados")

                    analytics_col1, analytics_col2, analytics_col3, analytics_col4 = st.columns(4)
                    with analytics_col1:
                        st.metric("Approval Rate", f"{float(signal_audit_summary.get('approval_rate_pct', 0.0) or 0.0):.2f}%")
                    with analytics_col2:
                        st.metric("MFE Médio", f"{float(stats.get('avg_mfe_pct', 0.0) or 0.0):.2f}%")
                    with analytics_col3:
                        st.metric("MAE Médio", f"{float(stats.get('avg_mae_pct', 0.0) or 0.0):.2f}%")
                    with analytics_col4:
                        st.metric("Lucro Devolvido", f"{float(stats.get('avg_profit_given_back_pct', 0.0) or 0.0):.2f}%")

                    analytics_breakdown_col1, analytics_breakdown_col2, analytics_breakdown_col3 = st.columns(3)
                    with analytics_breakdown_col1:
                        if entry_quality_summary:
                            st.caption("Performance por Entry Quality")
                            st.dataframe(pd.DataFrame(entry_quality_summary), width="stretch", hide_index=True)
                    with analytics_breakdown_col2:
                        if risk_mode_summary:
                            st.caption("Performance por Risk Mode")
                            st.dataframe(pd.DataFrame(risk_mode_summary), width="stretch", hide_index=True)
                    with analytics_breakdown_col3:
                        approval_by_regime = signal_audit_summary.get('approval_by_regime') or {}
                        if approval_by_regime:
                            st.caption("Aprovação por Regime")
                            st.dataframe(
                                pd.DataFrame(
                                    [
                                        {
                                            "Regime": regime,
                                            "Candidatos": payload.get('candidate_count', 0),
                                            "Aprovados": payload.get('approved_count', 0),
                                            "Taxa %": payload.get('approval_rate_pct', 0.0),
                                        }
                                        for regime, payload in approval_by_regime.items()
                                    ]
                                ),
                                width="stretch",
                                hide_index=True,
                            )

                    block_reason_counts = signal_audit_summary.get('block_reason_counts') or {}
                    time_analytics = results.get('time_analytics') or {}
                    hour_of_day_breakdown = time_analytics.get('hour_of_day_breakdown') or []
                    day_of_week_breakdown = time_analytics.get('day_of_week_breakdown') or []
                    holding_time_breakdown = time_analytics.get('holding_time_breakdown') or []
                    if block_reason_counts or hour_of_day_breakdown or day_of_week_breakdown or holding_time_breakdown:
                        analytics_time_col1, analytics_time_col2, analytics_time_col3 = st.columns(3)
                        with analytics_time_col1:
                            if block_reason_counts:
                                st.caption("Top Block Reasons")
                                st.dataframe(
                                    pd.DataFrame(
                                        [{"Motivo": reason, "Qtd": count} for reason, count in block_reason_counts.items()]
                                    ),
                                    width="stretch",
                                    hide_index=True,
                                )
                        with analytics_time_col2:
                            if hour_of_day_breakdown or day_of_week_breakdown:
                                st.caption("Performance por Hora / Dia")
                                time_rows = [
                                    {
                                        "Tipo": "Hora",
                                        "Bucket": row.get('hour_of_day'),
                                        "Trades": row.get('total_trades', 0),
                                        "Retorno %": row.get('total_return_pct', 0.0),
                                        "Win Rate %": row.get('win_rate', 0.0),
                                    }
                                    for row in hour_of_day_breakdown
                                ]
                                time_rows.extend(
                                    [
                                        {
                                            "Tipo": "Dia",
                                            "Bucket": row.get('day_of_week'),
                                            "Trades": row.get('total_trades', 0),
                                            "Retorno %": row.get('total_return_pct', 0.0),
                                            "Win Rate %": row.get('win_rate', 0.0),
                                        }
                                        for row in day_of_week_breakdown
                                    ]
                                )
                                st.dataframe(pd.DataFrame(time_rows), width="stretch", hide_index=True)
                        with analytics_time_col3:
                            if holding_time_breakdown:
                                st.caption("Holding Time Buckets")
                                st.dataframe(pd.DataFrame(holding_time_breakdown), width="stretch", hide_index=True)

            if show_validation_view and results.get('validation'):
                validation = results['validation']
                in_sample_stats = validation['in_sample']['stats']
                out_of_sample_stats = validation['out_of_sample']['stats']

                st.markdown("---")
                st.subheader("🧪 Validação Fora da Amostra")
                st.caption(
                    f"Split temporal: {100 - validation['split_pct']:.0f}% in-sample / "
                    f"{validation['split_pct']:.0f}% out-of-sample até {pd.Timestamp(validation['split_date']).strftime('%d/%m/%Y %H:%M')}"
                )

                val_col1, val_col2, val_col3, val_col4 = st.columns(4)
                with val_col1:
                    st.metric("IS Retorno", f"{in_sample_stats['total_return_pct']:.2f}%")
                with val_col2:
                    st.metric("OOS Retorno", f"{out_of_sample_stats['total_return_pct']:.2f}%")
                with val_col3:
                    st.metric("OOS Profit Factor", f"{out_of_sample_stats['profit_factor']:.2f}")
                with val_col4:
                    st.metric("OOS Expectancy", f"{out_of_sample_stats['expectancy_pct']:.2f}%")

                val_col1, val_col2, val_col3 = st.columns(3)
                with val_col1:
                    st.metric("IS Trades", in_sample_stats['total_trades'])
                with val_col2:
                    st.metric("OOS Trades", out_of_sample_stats['total_trades'])
                with val_col3:
                    st.metric("OOS Win Rate", f"{out_of_sample_stats['win_rate']:.1f}%")

                if validation.get('oos_passed'):
                    st.success(
                        f"✅ OOS aprovado: {ProductionConfig.MIN_PROMOTION_OOS_TRADES}+ trades, "
                        f"retorno > 0 e profit factor >= {ProductionConfig.MIN_PROMOTION_OOS_PROFIT_FACTOR:.2f}"
                    )
                else:
                    st.warning("⚠️ OOS fraco: a estratégia ainda não provou edge suficiente fora da amostra")

            if show_validation_view and results.get('walk_forward'):
                walk_forward = results['walk_forward']

                st.markdown("---")
                st.subheader("🧭 Walk-Forward")

                wf_col1, wf_col2, wf_col3, wf_col4 = st.columns(4)
                with wf_col1:
                    st.metric("Janelas", walk_forward['total_windows'])
                with wf_col2:
                    st.metric("Pass Rate", f"{walk_forward['pass_rate_pct']:.1f}%")
                with wf_col3:
                    st.metric("WF Avg OOS", f"{walk_forward['avg_oos_return_pct']:.2f}%")
                with wf_col4:
                    st.metric("WF Avg PF", f"{walk_forward['avg_oos_profit_factor']:.2f}")

                wf_col1, wf_col2 = st.columns(2)
                with wf_col1:
                    st.metric("WF Avg Expectancy", f"{walk_forward['avg_oos_expectancy_pct']:.2f}%")
                with wf_col2:
                    st.metric("Janelas Aprovadas", f"{walk_forward['passed_windows']}/{walk_forward['total_windows']}")

                if walk_forward.get('overall_passed'):
                    st.success("✅ Walk-forward consistente: a maioria das janelas OOS manteve edge")
                else:
                    st.warning("⚠️ Walk-forward inconsistente: o edge ainda não se sustenta bem entre janelas")

                walk_forward_rows = []
                for window in walk_forward['windows']:
                    walk_forward_rows.append({
                        'Janela': window['window_index'],
                        'IS Fim': pd.Timestamp(window['in_sample_end']).strftime('%d/%m/%Y %H:%M'),
                        'OOS Início': pd.Timestamp(window['out_of_sample_start']).strftime('%d/%m/%Y %H:%M'),
                        'OOS Fim': pd.Timestamp(window['out_of_sample_end']).strftime('%d/%m/%Y %H:%M'),
                        'OOS Retorno %': window['out_of_sample']['stats']['total_return_pct'],
                        'OOS Profit Factor': window['out_of_sample']['stats']['profit_factor'],
                        'OOS Expectancy %': window['out_of_sample']['stats']['expectancy_pct'],
                        'Aprovada': 'Sim' if window['passed'] else 'Não',
                    })

                st.dataframe(pd.DataFrame(walk_forward_rows), width='stretch', hide_index=True)

            if show_summary_view:
                # Detailed Performance Analysis
                st.markdown("---")
                st.subheader("📈 Análise Detalhada de Performance")

                col1, col2 = st.columns(2)

                with col1:
                    st.markdown("**💰 Métricas Financeiras**")
                    profit_loss = stats['final_balance'] - stats['initial_balance']
                    profit_color = "🟢" if profit_loss >= 0 else "🔴"

                    st.info(f"""
                    **Saldo Inicial:** ${stats['initial_balance']:,.2f}  
                    **Saldo Final:** ${stats['final_balance']:,.2f}  
                    **Lucro/Prejuízo:** {profit_color} ${profit_loss:,.2f}  
                    **Retorno Percentual:** {stats['total_return_pct']:.2f}%  
                    **Sharpe Ratio:** {stats['sharpe_ratio']:.2f}
                    """)

                with col2:
                    st.markdown("**📊 Métricas de Trading**")
                    avg_profit_color = "🟢" if stats['avg_profit'] > 0 else "🟡"
                    avg_loss_color = "🔴" if stats['avg_loss'] > 0 else "🟡"

                    st.info(f"""
                    **Trades Vencedores:** {stats['winning_trades']} ({stats['win_rate']:.1f}%)  
                    **Trades Perdedores:** {stats['losing_trades']} ({100-stats['win_rate']:.1f}%)  
                    **Lucro Médio:** {avg_profit_color} {stats['avg_profit']:.2f}%  
                    **Perda Média:** {avg_loss_color} {stats['avg_loss']:.2f}%  
                    **Máximo Drawdown:** {stats['max_drawdown']:.2f}%
                    """)

                st.markdown("**🎯 Análise Inteligente dos Resultados**")

                if score_pct >= 80:
                    st.success(f"🏆 **ESTRATÉGIA EXCELENTE** - Score: {score_pct:.0f}/100")
                    st.success("✅ Esta estratégia demonstra alta qualidade e pode ser considerada para trading real!")
                elif score_pct >= 60:
                    st.success(f"🎯 **BOA ESTRATÉGIA** - Score: {score_pct:.0f}/100")
                    st.info("💡 Estratégia promissora, considere ajustes finos nos parâmetros.")
                elif score_pct >= 40:
                    st.warning(f"⚠️ **ESTRATÉGIA MÉDIA** - Score: {score_pct:.0f}/100")
                    st.warning("🔧 Precisa de otimização. Teste diferentes parâmetros de RSI.")
                else:
                    st.error(f"❌ **ESTRATÉGIA FRACA** - Score: {score_pct:.0f}/100")
                    st.error("🚫 Não recomendada para trading real. Revise completamente a abordagem.")

                st.markdown("**🎯 Recomendações Específicas:**")
                recommendations = []

                if stats['total_return_pct'] < 0:
                    recommendations.append("📉 **Retorno negativo**: Considere inverter a lógica ou usar timeframe maior")
                if stats['win_rate'] < 50:
                    recommendations.append("🎯 **Taxa de acerto baixa**: Teste RSI mais restritivo (ex: 15-85)")
                if stats['max_drawdown'] > 20:
                    recommendations.append("⚠️ **Alto risco**: Implemente stop-loss ou reduza tamanho das posições")
                if stats['total_trades'] < 10:
                    recommendations.append("📊 **Poucos trades**: Use timeframe menor ou período maior")
                if stats['sharpe_ratio'] < 0.5:
                    recommendations.append("📈 **Baixo Sharpe**: Estratégia inconsistente, revise parâmetros")
                if stats.get('profit_factor', 0) < 1.2:
                    recommendations.append("💰 **Profit Factor baixo**: Ajuste take-profit ou melhore timing de entrada")
                if not recommendations:
                    recommendations.append("🏆 **Excelente trabalho!** Esta estratégia está bem calibrada.")

                for i, rec in enumerate(recommendations, 1):
                    st.markdown(f"{i}. {rec}")

                st.markdown("**⚡ Testes Rápidos Sugeridos:**")
                opt_col1, opt_col2 = st.columns(2)

                with opt_col1:
                    if st.button(
                        "✅ Baseline Aprovado",
                        help=f"Aplicar RSI {AppConfig.DEFAULT_RSI_MIN}/{AppConfig.DEFAULT_RSI_MAX}",
                    ):
                        st.session_state.bt_rsi_period = AppConfig.DEFAULT_RSI_PERIOD
                        st.session_state.bt_rsi_min = AppConfig.DEFAULT_RSI_MIN
                        st.session_state.bt_rsi_max = AppConfig.DEFAULT_RSI_MAX
                        st.rerun()

                    if st.button("📈 Timeframe Maior", help="Mudar para timeframe superior"):
                        current_tf = st.session_state.get('bt_timeframe', '15m')
                        tf_hierarchy = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
                        if current_tf in tf_hierarchy:
                            current_idx = tf_hierarchy.index(current_tf)
                            if current_idx < len(tf_hierarchy) - 1:
                                st.session_state.bt_timeframe = tf_hierarchy[current_idx + 1]
                                st.rerun()

                with opt_col2:
                    if st.button("✅ Perfil Global", help="Reaplicar o baseline global EMA/RSI"):
                        _apply_bt_preset(
                            AppConfig.DEFAULT_BACKTEST_PRESET,
                            start_days=AppConfig.DEFAULT_BACKTEST_WINDOW_DAYS,
                        )
                        st.rerun()

                    if st.button("🔄 Período Maior", help="Dobrar período de teste"):
                        current_days = (st.session_state.bt_end_date - st.session_state.bt_start_date).days
                        new_start = st.session_state.bt_end_date - timedelta(days=min(current_days * 2, max_backtest_days))
                        st.session_state.bt_start_date = new_start
                        st.rerun()

            if show_trades_view:
                if results['trades']:
                    st.markdown("---")
                    st.subheader("📋 Histórico de Trades")

                    trade_df = backtest_engine.get_trade_summary_df()
                    if not trade_df.empty:
                        trade_df_display = trade_df.copy()
                        trade_df_display['timestamp'] = trade_df_display['timestamp'].dt.strftime('%d/%m/%Y %H:%M')
                        trade_df_display['entry_price'] = trade_df_display['entry_price'].apply(lambda x: f"${x:.6f}")
                        trade_df_display['price'] = trade_df_display['price'].apply(lambda x: f"${x:.6f}")
                        trade_df_display['profit_loss_pct'] = trade_df_display['profit_loss_pct'].apply(lambda x: f"{x:.2f}%")
                        trade_df_display['profit_loss'] = trade_df_display['profit_loss'].apply(lambda x: f"${x:.2f}")

                        trade_df_display.columns = [
                            'Data/Hora', 'Preço Entrada', 'Preço Saída',
                            'Retorno %', 'Lucro/Perda $', 'Sinal'
                        ]

                        display_limit = min(20, len(trade_df_display))
                        st.info(f"📊 Mostrando os últimos {display_limit} trades de {len(trade_df_display)} total")

                        st.dataframe(
                            trade_df_display.tail(display_limit),
                            width='stretch',
                            hide_index=True
                        )

                        if len(trade_df_display) > display_limit:
                            if st.button(f"📋 Ver todos os {len(trade_df_display)} trades", key="show_all_trades"):
                                st.dataframe(trade_df_display, width='stretch', hide_index=True)

                if results.get('trade_autopsy'):
                    st.markdown("---")
                    st.subheader("🧪 Trade Autópsia")

                    autopsy_df = pd.DataFrame(results['trade_autopsy'])
                    if not autopsy_df.empty:
                        filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)
                        with filter_col1:
                            regime_filter = st.multiselect(
                                "Regime",
                                sorted(str(x) for x in autopsy_df['regime'].dropna().unique().tolist()),
                                default=[],
                                key="autopsy_regime_filter",
                            )
                        with filter_col2:
                            pattern_column = 'market_pattern' if 'market_pattern' in autopsy_df.columns else 'setup_name'
                            setup_filter = st.multiselect(
                                "Perfil de Leitura",
                                sorted(str(x) for x in autopsy_df[pattern_column].dropna().unique().tolist()),
                                default=[],
                                key="autopsy_setup_filter",
                            )
                        with filter_col3:
                            exit_filter = st.multiselect(
                                "Saída",
                                sorted(str(x) for x in autopsy_df['exit_reason'].dropna().unique().tolist()),
                                default=[],
                                key="autopsy_exit_filter",
                            )
                        with filter_col4:
                            risk_filter = st.multiselect(
                                "Risk Mode",
                                sorted(str(x) for x in autopsy_df['risk_mode'].dropna().unique().tolist()),
                                default=[],
                                key="autopsy_risk_filter",
                            )

                        filtered_autopsy = autopsy_df.copy()
                        if regime_filter:
                            filtered_autopsy = filtered_autopsy[filtered_autopsy['regime'].astype(str).isin(regime_filter)]
                        if setup_filter:
                            filtered_autopsy = filtered_autopsy[filtered_autopsy[pattern_column].astype(str).isin(setup_filter)]
                        if exit_filter:
                            filtered_autopsy = filtered_autopsy[filtered_autopsy['exit_reason'].astype(str).isin(exit_filter)]
                        if risk_filter:
                            filtered_autopsy = filtered_autopsy[filtered_autopsy['risk_mode'].astype(str).isin(risk_filter)]

                        visible_columns = [
                            'entry_timestamp', 'timestamp', 'market_pattern', 'setup_name', 'regime', 'structure_state',
                            'confirmation_state', 'entry_quality', 'entry_score', 'risk_mode',
                            'exit_reason', 'profit_loss_pct', 'profit_loss', 'mfe_pct', 'mae_pct',
                            'rr_realized', 'profit_given_back_pct', 'holding_time_minutes'
                        ]
                        available_columns = [column for column in visible_columns if column in filtered_autopsy.columns]
                        st.dataframe(filtered_autopsy[available_columns], width='stretch', hide_index=True)

            if show_trades_view:
                if results.get('signal_audit'):
                    st.markdown("---")
                    st.subheader("🚧 Block Analytics")
                    signal_audit_df = pd.DataFrame(results['signal_audit'])
                    if not signal_audit_df.empty:
                        timeline_df = signal_audit_df.copy()
                        timeline_df['timestamp'] = pd.to_datetime(timeline_df.get('timestamp'), errors='coerce')
                        timeline_df = timeline_df.dropna(subset=['timestamp']).sort_values('timestamp')
                        actionable_signals = {'COMPRA', 'VENDA'}
                        if not timeline_df.empty:
                            candidate_col = (
                                timeline_df['candidate_signal']
                                if 'candidate_signal' in timeline_df.columns
                                else pd.Series('', index=timeline_df.index, dtype='object')
                            )
                            approved_col = (
                                timeline_df['approved_signal']
                                if 'approved_signal' in timeline_df.columns
                                else pd.Series('', index=timeline_df.index, dtype='object')
                            )
                            blocked_col = (
                                timeline_df['blocked_signal']
                                if 'blocked_signal' in timeline_df.columns
                                else pd.Series('', index=timeline_df.index, dtype='object')
                            )
                            scenario_col = (
                                timeline_df['scenario_score']
                                if 'scenario_score' in timeline_df.columns
                                else pd.Series(pd.NA, index=timeline_df.index, dtype='object')
                            )

                            timeline_df['candidate_flag'] = candidate_col.isin(actionable_signals).astype(int)
                            timeline_df['approved_flag'] = approved_col.isin(actionable_signals).astype(int)
                            timeline_df['blocked_flag'] = blocked_col.isin(actionable_signals).astype(int)
                            timeline_df['scenario_score'] = pd.to_numeric(scenario_col, errors='coerce')

                            if len(timeline_df) > 2000:
                                timeline_freq = '1D'
                            elif len(timeline_df) > 800:
                                timeline_freq = '6H'
                            else:
                                timeline_freq = '1H'

                            execution_timeline = (
                                timeline_df.set_index('timestamp')
                                .resample(timeline_freq)
                                .agg(
                                    candidate_count=('candidate_flag', 'sum'),
                                    approved_count=('approved_flag', 'sum'),
                                    blocked_count=('blocked_flag', 'sum'),
                                    avg_scenario_score=('scenario_score', 'mean'),
                                )
                                .reset_index()
                            )
                            execution_timeline['approval_rate_pct'] = (
                                (
                                    execution_timeline['approved_count']
                                    / execution_timeline['candidate_count'].replace({0: pd.NA})
                                ) * 100.0
                            ).fillna(0.0)

                            fig_execution = make_subplots(
                                rows=1,
                                cols=1,
                                specs=[[{"secondary_y": True}]],
                            )
                            fig_execution.add_trace(
                                go.Bar(
                                    x=execution_timeline['timestamp'],
                                    y=execution_timeline['approved_count'],
                                    name='Aprovados',
                                    marker_color='#2ca02c',
                                    opacity=0.75,
                                ),
                                row=1,
                                col=1,
                                secondary_y=False,
                            )
                            fig_execution.add_trace(
                                go.Bar(
                                    x=execution_timeline['timestamp'],
                                    y=execution_timeline['blocked_count'],
                                    name='Bloqueados',
                                    marker_color='#d62728',
                                    opacity=0.65,
                                ),
                                row=1,
                                col=1,
                                secondary_y=False,
                            )
                            fig_execution.add_trace(
                                go.Scatter(
                                    x=execution_timeline['timestamp'],
                                    y=execution_timeline['approval_rate_pct'],
                                    mode='lines',
                                    name='Taxa Aprovação %',
                                    line=dict(color='#9467bd', width=2),
                                ),
                                row=1,
                                col=1,
                                secondary_y=True,
                            )
                            fig_execution.add_trace(
                                go.Scatter(
                                    x=execution_timeline['timestamp'],
                                    y=execution_timeline['avg_scenario_score'],
                                    mode='lines',
                                    name='Cenário Médio',
                                    line=dict(color='#17becf', width=1.6, dash='dash'),
                                ),
                                row=1,
                                col=1,
                                secondary_y=True,
                            )

                            fig_execution.update_layout(
                                barmode='stack',
                                title=f"Timeline de Sinais (Backtest) - {result_symbol} {result_timeframe}",
                                height=420,
                                legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
                                margin=dict(l=30, r=30, t=70, b=30),
                            )
                            fig_execution.update_yaxes(title_text="Sinais", row=1, col=1, secondary_y=False)
                            fig_execution.update_yaxes(title_text="Taxa / Score", row=1, col=1, secondary_y=True)

                            st.plotly_chart(fig_execution, width='stretch')
                            st.caption(f"Agregação temporal automática: {timeline_freq} | foco em aprovação/bloqueio e qualidade de sinais.")

                        preview_columns = [
                            'timestamp', 'candidate_signal', 'approved_signal', 'blocked_signal',
                            'block_reason', 'regime', 'structure_state', 'confirmation_state',
                            'entry_quality', 'entry_score', 'scenario_score', 'risk_mode'
                        ]
                        available_preview_columns = [column for column in preview_columns if column in signal_audit_df.columns]
                        st.dataframe(signal_audit_df[available_preview_columns].tail(50), width='stretch', hide_index=True)

                st.markdown("---")
                st.subheader("📊 Histórico de Testes")

                if 'backtest_history' not in st.session_state:
                    st.session_state.backtest_history = []

                if st.button("💾 Salvar Teste Atual", key="save_current_test"):
                    test_record = {
                        'timestamp': datetime.now().strftime('%d/%m/%Y %H:%M'),
                        'symbol': result_symbol,
                        'timeframe': result_timeframe,
                        'strategy_version': result_strategy_version,
                        'period_days': period_days,
                        'rsi_min': result_rsi_min,
                        'rsi_max': result_rsi_max,
                        'return_pct': stats['total_return_pct'],
                        'win_rate': stats['win_rate'],
                        'total_trades': stats['total_trades'],
                        'max_drawdown': stats['max_drawdown'],
                        'sharpe_ratio': stats['sharpe_ratio'],
                        'score': score_pct
                    }
                    st.session_state.backtest_history.append(test_record)
                    st.success("✅ Teste salvo no histórico!")

                if st.session_state.backtest_history:
                    history_df = pd.DataFrame(st.session_state.backtest_history)

                    def style_history(val):
                        if isinstance(val, (int, float)):
                            if val > 0:
                                return 'color: green'
                            if val < 0:
                                return 'color: red'
                        return ''

                    display_history = history_df.tail(10).copy()
                    display_history = display_history.round(2)

                    st.dataframe(
                        display_history.style.applymap(style_history, subset=['return_pct']),
                        width="stretch",
                        hide_index=True
                    )

                    best_test = history_df.loc[history_df['score'].idxmax()]
                    st.success(
                        f"🏆 **Melhor Teste**: {best_test['symbol']} {best_test['timeframe']} "
                        f"- Score: {best_test['score']:.0f} - Retorno: {best_test['return_pct']:.2f}%"
                    )

                col1, col2, col3 = st.columns(3)

                with col1:
                    if st.button("🗑️ Limpar Resultados", key="clear_backtest_results"):
                        st.session_state.backtest_results = None
                        st.session_state.backtest_scan_results = None
                        st.session_state.backtest_optimization_results = None
                        st.session_state.backtest_robustness_results = None
                        st.rerun()

                with col2:
                    if st.button("📋 Limpar Histórico", key="clear_history"):
                        st.session_state.backtest_history = []
                        st.rerun()

                with col3:
                    if st.session_state.backtest_history:
                        history_csv = pd.DataFrame(st.session_state.backtest_history).to_csv(index=False)
                        st.download_button(
                            "💾 Exportar Histórico",
                            data=history_csv,
                            file_name=f"backtest_history_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                            mime="text/csv"
                        )

        else:
            # Enhanced help section when no results
            st.markdown("---")
            st.markdown("### 📚 Guia de Backtesting")

            # Quick start guide in columns
            guide_col1, guide_col2 = st.columns(2)

            with guide_col1:
                st.markdown(f"""
                **🚀 Como Começar:**

                1. **Escolha um par** (ex: BTC-USD para volatilidade)
                2. **Selecione timeframe** (15m é bom para iniciantes)
                3. **Configure período** (comece com 1-2 semanas)
                4. **Use o baseline aprovado** ({AppConfig.DEFAULT_RSI_MIN}/{AppConfig.DEFAULT_RSI_MAX})
                5. **Execute e analise**

                **💡 Dicas de Performance:**
                - Timeframes menores = mais trades
                - RSI restritivo = menos trades, mais precisão
                - Períodos maiores = resultados mais confiáveis
                """)

            with guide_col2:
                st.markdown("""
                **🎯 Métricas Importantes:**

                - **Total Return**: Quanto ganhou/perdeu
                - **Win Rate**: % de trades vencedores
                - **Max Drawdown**: Maior perda consecutiva
                - **Sharpe Ratio**: Retorno vs risco
                - **Score**: Avaliação geral (0-100)

                **⚠️ Interpretação:**
                - Score > 80: Estratégia excelente
                - Score 60-80: Boa estratégia
                - Score < 40: Precisa melhorar
                """)

            # Sample configurations
            st.markdown("**🔧 Configurações Populares:**")

            sample_col1, sample_col2, sample_col3 = st.columns(3)

            with sample_col1:
                st.info(
                    f"""
                **✅ Setup Validado**
                - Timeframe: 15m
                - RSI: {AppConfig.DEFAULT_RSI_MIN}/{AppConfig.DEFAULT_RSI_MAX}
                - Período: 90 dias
                - Para: baseline principal
                """
                )

            with sample_col2:
                st.info("""
                **⚖️ Swing Trading**
                - Timeframe: 1h
                - RSI: ajuste conforme contexto
                - Período: 1 mês
                - Para: trading moderado
                """)

            with sample_col3:
                st.info("""
                **🛡️ Posição Longa**
                - Timeframe: 4h
                - RSI: 50/50
                - Período: 3 meses
                - Para: investidores
                """)

        with st.expander("💾 Exportacoes", expanded=False):
            render_export_data_panel(symbol=symbol, timeframe=timeframe, key_prefix="backtest_hub")

    # Admin Panel Tab
    if active_dashboard_section == "admin":
        st.subheader("👑 Painel Administrativo")

        # Admin authentication
        configured_admin_password = ProductionConfig.ADMIN_PANEL_PASSWORD
        if "admin_authenticated" not in st.session_state:
            st.session_state.admin_authenticated = False
        if "admin_auth_error" not in st.session_state:
            st.session_state.admin_auth_error = ""
        dashboard_access_total = 0
        dashboard_access_error = ""
        try:
            dashboard_access_total = len(db.list_dashboard_user_access(limit=200))
        except Exception as bootstrap_exc:
            dashboard_access_error = str(bootstrap_exc)

        with st.expander("🧭 Bootstrap do Workspace", expanded=not st.session_state.get("admin_authenticated")):
            boot_col1, boot_col2, boot_col3 = st.columns(3)
            with boot_col1:
                st.metric("Senha Admin", "OK" if configured_admin_password else "PENDENTE")
            with boot_col2:
                st.metric("Sessão Admin", "ON" if st.session_state.get("admin_authenticated") else "OFF")
            with boot_col3:
                st.metric("Acessos Criados", int(dashboard_access_total))
            if dashboard_access_error:
                st.error(f"Não foi possível ler acessos da dashboard: {dashboard_access_error}")

            st.markdown(
                """
                1. Defina `ADMIN_PANEL_PASSWORD` no ambiente e faça deploy.
                2. Entre com essa senha no bloco de autenticação Admin abaixo.
                3. Crie os acessos manualmente em `Visão Admin -> Acessos`.
                4. Ajuste plano/assinatura (semanal, mensal, anual) para cada cliente.
                5. O bot só exige assinatura ativa para usuários comuns; o Admin tem bypass operacional.
                6. Em seguida, cadastre conta/risco/credenciais nas visões `Contas` e `Resumo`.
                """
            )
            st.caption(
                f"Regra de senha inicial: mínimo {int(ProductionConfig.DASHBOARD_MIN_PASSWORD_LENGTH)} caracteres."
            )

        if not configured_admin_password:
            st.warning("⚠️ Configure ADMIN_PANEL_PASSWORD para liberar o painel admin.")
        else:
            auth_col1, auth_col2 = st.columns([4, 1])
            with auth_col1:
                if not st.session_state.admin_authenticated:
                    st.text_input("🔐 Senha de Admin", type="password", key="admin_pass")
            with auth_col2:
                if st.session_state.admin_authenticated:
                    if st.button("🔒 Sair", key="admin_logout"):
                        st.session_state.admin_authenticated = False
                        st.session_state.admin_auth_error = ""
                        st.session_state.admin_pass = ""
                        st.rerun()
                else:
                    if st.button("🔓 Entrar", key="admin_login"):
                        provided_password = str(st.session_state.get("admin_pass") or "")
                        if hmac.compare_digest(provided_password, configured_admin_password):
                            st.session_state.admin_authenticated = True
                            st.session_state.admin_auth_error = ""
                            st.session_state.admin_pass = ""
                            st.rerun()
                        else:
                            st.session_state.admin_auth_error = "❌ Senha incorreta"

            if st.session_state.admin_authenticated:
                st.success("✅ Sessão administrativa autenticada.")
            elif st.session_state.admin_auth_error:
                st.error(st.session_state.admin_auth_error)
            else:
                st.info("🔐 Digite a senha de administrador para acessar o painel")
                st.caption("Se a página parecer vazia, primeiro autentique com a senha de Admin.")

        if st.session_state.get("admin_authenticated") and configured_admin_password:
            st.success("✅ Acesso autorizado!")

            user_manager = get_user_manager()

            # Admin stats
            stats = user_manager.get_user_stats()

            col1, col2, col3, col4 = st.columns(4)

            with col1:
                st.metric("👥 Total Usuários", stats['total_users'])
            with col2:
                st.metric("🆓 Usuários Free", stats['free_users'])
            with col3:
                st.metric("💎 Usuários Premium", stats['premium_users'])
            with col4:
                st.metric("🔥 Ativos Hoje", stats['active_today'])

            admin_view_mode = st.radio(
                "Visão Admin",
                options=["Resumo", "Acessos", "Contas", "Usuários", "Bots"],
                horizontal=True,
                key="admin_view_mode",
                help="Renderização otimizada: o painel administrativo monta apenas o grupo selecionado.",
            )
            admin_show_summary = admin_view_mode == "Resumo"
            admin_show_access = admin_view_mode == "Acessos"
            admin_show_accounts = admin_view_mode == "Contas"
            admin_show_users = admin_view_mode == "Usuários"
            admin_show_bots = admin_view_mode == "Bots"
            st.caption("Modo leve do admin: carregue apenas a área que você quer operar nesta sessão.")

            vault = None
            vault_error = ""
            if admin_show_summary or admin_show_accounts:
                try:
                    from services.credential_vault import CredentialVault

                    vault = CredentialVault(strict=False)
                except Exception as exc:
                    vault_error = str(exc)

            if admin_show_summary:
                st.markdown("---")
                st.subheader("🧩 Runtime Multiuser")
                multiuser_summary = db.get_multiuser_dashboard_summary()
                mu_col1, mu_col2, mu_col3, mu_col4, mu_col5 = st.columns(5)
                with mu_col1:
                    st.metric("Contas Ativas", int(multiuser_summary.get("active_accounts", 0) or 0))
                with mu_col2:
                    st.metric("Somente Paper", int(multiuser_summary.get("paper_accounts", 0) or 0))
                with mu_col3:
                    st.metric("Bloqueadas", int(multiuser_summary.get("blocked_accounts", 0) or 0))
                with mu_col4:
                    st.metric("Erro Operacional", int(multiuser_summary.get("operational_error_accounts", 0) or 0))
                with mu_col5:
                    st.metric("Mismatch", int(multiuser_summary.get("mismatch_accounts", 0) or 0))

                st.markdown("---")
                st.subheader("🔐 Segurança Multiuser")
                sec_col1, sec_col2, sec_col3, sec_col4, sec_col5 = st.columns(5)
                with sec_col1:
                    st.metric("Runtime", "ON" if ProductionConfig.ENABLE_MULTIUSER_RUNTIME else "OFF")
                with sec_col2:
                    st.metric("Auto Exec", "ON" if ProductionConfig.ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION else "OFF")
                with sec_col3:
                    st.metric("Vault", "OK" if vault and vault.is_configured() else "PENDENTE")
                with sec_col4:
                    st.metric("Token Guard", "ON" if ProductionConfig.REQUIRE_MULTIUSER_VALID_TOKEN else "OFF")
                with sec_col5:
                    permission_stack = (
                        ProductionConfig.REQUIRE_MULTIUSER_VALID_PERMISSIONS
                        and ProductionConfig.REQUIRE_MULTIUSER_RECONCILIATION_OK
                    )
                    st.metric("Perm/Recon", "ON" if permission_stack else "OFF")

                if vault_error:
                    st.error(f"Vault indisponível: {vault_error}")
                elif not vault or not vault.is_configured():
                    st.warning("Configure CREDENTIAL_ENCRYPTION_KEY para armazenar credenciais de exchange com segurança.")
                else:
                    st.success("Credenciais multiuser serão persistidas criptografadas com Fernet.")

                st.markdown("---")
                st.subheader("🗂️ Volume e Históricos")
                history_dir = get_history_data_dir()
                volume_col1, volume_col2, volume_col3 = st.columns(3)
                with volume_col1:
                    st.metric("DB Path", getattr(AppConfig, "DB_PATH", "n/d"))
                with volume_col2:
                    st.metric("History Dir", str(history_dir))
                with volume_col3:
                    st.metric("Arquivos Históricos", len(list_history_data_files(limit=500)))

                st.caption(
                    "Use este bloco apenas se quiser manter arquivos historicos auxiliares no volume. "
                    "O bot e o backtest nao dependem mais de CSV local por padrao."
                )

                history_files = list_history_data_files(limit=200)
                if history_files:
                    st.dataframe(pd.DataFrame(history_files), width="stretch", hide_index=True)
                else:
                    st.info(
                        f"Nenhum histórico encontrado em {history_dir}. "
                        "Isso nao bloqueia mais o runtime do bot. So use arquivos aqui se quiser manter historicos auxiliares."
                    )

                uploaded_history_files = st.file_uploader(
                    "Enviar históricos para o volume",
                    type=["csv", "gz"],
                    accept_multiple_files=True,
                    key="admin_history_csv_uploader",
                    help="Aceita arquivos .csv e .csv.gz, por exemplo BTCUSDT_15m.csv.gz.",
                )
                if st.button("Salvar históricos no volume", key="admin_save_history_files"):
                    if not uploaded_history_files:
                        st.warning("Selecione ao menos um arquivo .csv ou .csv.gz antes de salvar.")
                    else:
                        saved_files = []
                        failed_files = []
                        for uploaded_file in uploaded_history_files:
                            try:
                                saved_path = save_uploaded_history_file(uploaded_file)
                                saved_files.append(saved_path.name)
                            except Exception as exc:
                                failed_files.append(f"{getattr(uploaded_file, 'name', 'arquivo')}: {exc}")
                        if saved_files:
                            st.success(f"Arquivos salvos no volume: {', '.join(saved_files)}")
                        if failed_files:
                            st.error("Falha ao salvar alguns arquivos:\n" + "\n".join(failed_files))

            if admin_show_access:
                st.subheader("👤 Acesso da Dashboard")
                signup_requests = db.list_dashboard_signup_requests(limit=300)
                pending_signup_requests = [
                    item for item in signup_requests
                    if str(item.get("status") or "").strip().lower() == "pending"
                ]
                approved_signup_requests = [
                    item for item in signup_requests
                    if str(item.get("status") or "").strip().lower() == "approved"
                ]
                rejected_signup_requests = [
                    item for item in signup_requests
                    if str(item.get("status") or "").strip().lower() == "rejected"
                ]

                req_col1, req_col2, req_col3 = st.columns(3)
                with req_col1:
                    st.metric("Solicitações Pendentes", len(pending_signup_requests))
                with req_col2:
                    st.metric("Solicitações Aprovadas", len(approved_signup_requests))
                with req_col3:
                    st.metric("Solicitações Rejeitadas", len(rejected_signup_requests))

                st.markdown("### 📨 Solicitações de Cadastro")
                if pending_signup_requests:
                    pending_df = pd.DataFrame(pending_signup_requests)[
                        [
                            "id",
                            "login_name",
                            "display_name",
                            "contact_text",
                            "requested_at",
                            "notes",
                        ]
                    ].rename(
                        columns={
                            "id": "Request ID",
                            "login_name": "Login",
                            "display_name": "Nome",
                            "contact_text": "Contato",
                            "requested_at": "Solicitado em",
                            "notes": "Observações",
                        }
                    )
                    st.dataframe(pending_df, width="stretch", hide_index=True)
                else:
                    st.info("Não há solicitações pendentes no momento.")

                with st.form("dashboard_signup_review_form"):
                    if pending_signup_requests:
                        request_options = {
                            f"#{int(item['id'])} | {item.get('login_name')} | {item.get('display_name') or 'sem nome'}": item
                            for item in pending_signup_requests
                        }
                        selected_request_label = st.selectbox(
                            "Solicitação pendente",
                            options=list(request_options.keys()),
                            key="dashboard_signup_selected_request",
                        )
                        selected_request = request_options[selected_request_label]
                        review_action = st.radio(
                            "Ação",
                            options=["Aprovar", "Rejeitar"],
                            horizontal=True,
                            key="dashboard_signup_review_action",
                        )
                        review_user_id = st.number_input(
                            "User ID para aprovação (0 = automático)",
                            min_value=0,
                            step=1,
                            value=0,
                            key="dashboard_signup_review_user_id",
                        )
                        review_notes = st.text_area(
                            "Notas da revisão",
                            key="dashboard_signup_review_notes",
                        )
                        if st.form_submit_button("Processar Solicitação"):
                            try:
                                reviewer_name = str(st.session_state.get("dashboard_user_login") or "admin")
                                result = db.review_dashboard_signup_request(
                                    request_id=int(selected_request["id"]),
                                    action="approve" if review_action == "Aprovar" else "reject",
                                    reviewed_by=reviewer_name,
                                    review_notes=review_notes,
                                    approved_user_id=(int(review_user_id) if int(review_user_id) > 0 else None),
                                )
                                if result.get("status") == "approved":
                                    st.success(
                                        f"Solicitação aprovada. Login {result.get('login_name')} liberado para User ID {result.get('approved_user_id')}."
                                    )
                                else:
                                    st.warning(f"Solicitação {result.get('request_id')} rejeitada.")
                                st.rerun()
                            except Exception as review_error:
                                st.error(f"Falha ao revisar solicitação: {review_error}")
                    else:
                        st.info("Sem pendências para revisar.")
                        st.form_submit_button("Processar Solicitação", disabled=True)

                with st.expander("Histórico de solicitações", expanded=False):
                    if signup_requests:
                        history_df = pd.DataFrame(signup_requests)[
                            [
                                "id",
                                "login_name",
                                "display_name",
                                "status",
                                "requested_at",
                                "reviewed_at",
                                "reviewed_by",
                                "approved_user_id",
                                "review_notes",
                            ]
                        ].rename(
                            columns={
                                "id": "Request ID",
                                "login_name": "Login",
                                "display_name": "Nome",
                                "status": "Status",
                                "requested_at": "Solicitado em",
                                "reviewed_at": "Revisado em",
                                "reviewed_by": "Revisado por",
                                "approved_user_id": "User ID Aprovado",
                                "review_notes": "Notas",
                            }
                        )
                        st.dataframe(history_df, width="stretch", hide_index=True)
                    else:
                        st.caption("Sem histórico de solicitações.")

                st.markdown("---")
                dashboard_access_rows = db.list_dashboard_user_access(limit=200)
                if dashboard_access_rows:
                    access_df = pd.DataFrame(dashboard_access_rows)[
                        [
                            "user_id",
                            "login_name",
                            "is_active",
                            "require_password_change",
                            "plan_code",
                            "subscription_status",
                            "subscription_expires_at",
                            "telegram_username",
                            "telegram_first_name",
                            "telegram_plan",
                            "account_count",
                            "last_login_at",
                        ]
                    ].rename(
                        columns={
                            "user_id": "User ID",
                            "login_name": "Login",
                            "is_active": "Ativo",
                            "require_password_change": "Troca Senha",
                            "plan_code": "Assinatura",
                            "subscription_status": "Status Assinatura",
                            "subscription_expires_at": "Expira em",
                            "telegram_username": "Telegram Username",
                            "telegram_first_name": "Nome",
                            "telegram_plan": "Plano",
                            "account_count": "Contas",
                            "last_login_at": "Último Login",
                        }
                    )
                    st.dataframe(access_df, width="stretch", hide_index=True)
                else:
                    st.warning("Nenhum acesso de dashboard provisionado ainda. Crie o primeiro acesso no formulário abaixo.")
                    st.caption("Depois disso o usuário já consegue fazer login no Workspace pela barra lateral.")

                with st.form("dashboard_user_access_form"):
                    access_col1, access_col2, access_col3 = st.columns(3)
                    with access_col1:
                        dashboard_access_user_id = st.number_input("User ID (Dashboard)", min_value=1, step=1, key="dashboard_access_user_id")
                        dashboard_access_login = st.text_input("Login da Dashboard", key="dashboard_access_login")
                    with access_col2:
                        dashboard_access_password = st.text_input("Senha Inicial", type="password", key="dashboard_access_password")
                        dashboard_access_active = st.checkbox("Acesso Ativo", value=True, key="dashboard_access_active")
                    with access_col3:
                        dashboard_force_password_change = st.checkbox(
                            "Forçar Troca de Senha",
                            value=True,
                            key="dashboard_force_password_change",
                        )
                        dashboard_access_notes = st.text_area("Notas do Acesso", key="dashboard_access_notes")

                    if st.form_submit_button("Salvar Acesso da Dashboard"):
                        try:
                            db.upsert_dashboard_user_access(
                                {
                                    "user_id": int(dashboard_access_user_id),
                                    "login_name": str(dashboard_access_login).strip(),
                                    "password": str(dashboard_access_password),
                                    "is_active": bool(dashboard_access_active),
                                    "require_password_change": bool(dashboard_force_password_change),
                                    "notes": dashboard_access_notes,
                                }
                            )
                            st.success("Acesso da dashboard salvo com sucesso.")
                            st.rerun()
                        except Exception as access_error:
                            st.error(f"Falha ao salvar acesso da dashboard: {access_error}")

                st.markdown("### 🛡️ Licenças de IP/Dispositivo")
                license_rows = db.list_dashboard_device_licenses(limit=300)
                if license_rows:
                    license_df = pd.DataFrame(license_rows)[
                        [
                            "user_id",
                            "login_name",
                            "license_scope",
                            "is_active",
                            "bind_ip",
                            "bind_device",
                            "has_ip_binding",
                            "has_device_binding",
                            "first_seen_at",
                            "last_seen_at",
                        ]
                    ].rename(
                        columns={
                            "user_id": "User ID",
                            "login_name": "Login",
                            "license_scope": "Escopo",
                            "is_active": "Ativa",
                            "bind_ip": "Trava IP",
                            "bind_device": "Trava Dispositivo",
                            "has_ip_binding": "IP Vinculado",
                            "has_device_binding": "Dispositivo Vinculado",
                            "first_seen_at": "Primeiro Acesso",
                            "last_seen_at": "Último Acesso",
                        }
                    )
                    st.dataframe(license_df, width="stretch", hide_index=True)
                else:
                    st.info("Nenhuma licença vinculada ainda. O primeiro login do cliente cria o vínculo automaticamente.")

                with st.form("dashboard_license_reset_form"):
                    reset_user_id = st.number_input(
                        "User ID para resetar licença",
                        min_value=1,
                        step=1,
                        key="dashboard_license_reset_user_id",
                    )
                    reset_scope = st.text_input(
                        "Escopo",
                        value="workspace",
                        key="dashboard_license_reset_scope",
                    )
                    st.caption("Use isto quando o cliente trocar de internet/dispositivo e você quiser liberar um novo primeiro acesso.")
                    if st.form_submit_button("Resetar Licença do Usuário"):
                        try:
                            removed = db.reset_dashboard_device_license(
                                int(reset_user_id),
                                scope=str(reset_scope or "workspace"),
                            )
                            if removed:
                                st.success("Licença resetada. O próximo acesso do cliente será vinculado novamente.")
                            else:
                                st.info("Nenhuma licença existente para esse usuário/escopo.")
                            st.rerun()
                        except Exception as license_error:
                            st.error(f"Falha ao resetar licença: {license_error}")

                st.markdown("### 💳 Assinaturas e Créditos")
                subscription_rows = db.list_dashboard_user_subscriptions(limit=300)
                if subscription_rows:
                    subscription_df = pd.DataFrame(subscription_rows)[
                        [
                            "user_id",
                            "login_name",
                            "plan_code",
                            "status",
                            "expires_at",
                            "days_remaining",
                            "is_active",
                            "expiring_soon",
                            "credits_balance",
                            "auto_renew",
                        ]
                    ].rename(
                        columns={
                            "user_id": "User ID",
                            "login_name": "Login",
                            "plan_code": "Plano",
                            "status": "Status",
                            "expires_at": "Expira em",
                            "days_remaining": "Dias Restantes",
                            "is_active": "Ativa",
                            "expiring_soon": "Expira em Breve",
                            "credits_balance": "Créditos",
                            "auto_renew": "Auto Renew",
                        }
                    )
                    st.dataframe(subscription_df, width="stretch", hide_index=True)
                else:
                    st.caption("Nenhuma assinatura cadastrada ainda.")

                with st.form("dashboard_subscription_admin_form"):
                    sub_col1, sub_col2, sub_col3 = st.columns(3)
                    with sub_col1:
                        sub_user_id = st.number_input("User ID Assinatura", min_value=1, step=1, key="sub_user_id")
                        sub_plan_code = st.selectbox(
                            "Plano",
                            options=["weekly", "monthly", "yearly", "free"],
                            key="sub_plan_code",
                        )
                    with sub_col2:
                        sub_action = st.selectbox(
                            "Ação",
                            options=["Ativar/Renovar", "Bloquear", "Inativar"],
                            key="sub_action",
                        )
                        sub_auto_renew = st.checkbox("Auto Renew", value=False, key="sub_auto_renew")
                    with sub_col3:
                        sub_extend = st.checkbox("Somar ao período atual", value=True, key="sub_extend")
                        sub_credits = st.number_input("Créditos (+/-)", value=0.0, step=10.0, key="sub_credits")
                        sub_notes = st.text_area("Notas da Assinatura", key="sub_notes")

                    if st.form_submit_button("Salvar Assinatura"):
                        try:
                            if sub_action == "Ativar/Renovar":
                                result_sub = db.activate_dashboard_user_subscription(
                                    user_id=int(sub_user_id),
                                    plan_code=str(sub_plan_code),
                                    approved_by="admin",
                                    extend_from_current=bool(sub_extend),
                                    auto_renew=bool(sub_auto_renew),
                                    payment_provider="manual",
                                    credits_delta=float(sub_credits),
                                    notes=sub_notes,
                                )
                                st.success(
                                    f"Assinatura atualizada: {result_sub.get('plan_code')} | "
                                    f"status={result_sub.get('status')} | expira={result_sub.get('expires_at')}"
                                )
                            elif sub_action == "Bloquear":
                                result_sub = db.set_dashboard_user_subscription_status(
                                    user_id=int(sub_user_id),
                                    status="blocked",
                                    notes=sub_notes,
                                )
                                st.warning(
                                    f"Assinatura bloqueada para User ID {int(sub_user_id)} "
                                    f"(status {result_sub.get('status')})."
                                )
                            else:
                                result_sub = db.set_dashboard_user_subscription_status(
                                    user_id=int(sub_user_id),
                                    status="inactive",
                                    notes=sub_notes,
                                )
                                st.info(
                                    f"Assinatura inativada para User ID {int(sub_user_id)} "
                                    f"(status {result_sub.get('status')})."
                                )
                            st.rerun()
                        except Exception as sub_error:
                            st.error(f"Falha ao salvar assinatura: {sub_error}")

            if admin_show_accounts:
                st.subheader("🏦 Contas Multiuser")
                account_overview = db.get_multiuser_account_overview(limit=200)
                if account_overview:
                    account_df = pd.DataFrame(account_overview)
                    st.dataframe(account_df, width="stretch", hide_index=True)
                else:
                    st.info("Nenhuma conta multiuser cadastrada.")

                st.subheader("🧾 Onboarding de Conta")
                with st.form("multiuser_account_form"):
                    acc_col1, acc_col2, acc_col3 = st.columns(3)
                    with acc_col1:
                        mu_user_id = st.number_input("User ID", min_value=1, step=1, key="mu_user_id")
                        mu_account_id = st.text_input("Account ID", key="mu_account_id")
                        mu_account_alias = st.text_input("Alias", key="mu_account_alias")
                    with acc_col2:
                        mu_exchange = st.selectbox(
                            "Exchange",
                            options=AppConfig.BRAZIL_SUPPORTED_EXCHANGES or ["binance"],
                            key="mu_exchange",
                        )
                        mu_status = st.selectbox("Status", options=["active", "disabled"], key="mu_status")
                        mu_capital_base = st.number_input("Capital Base", min_value=0.0, value=10000.0, step=100.0, key="mu_capital_base")
                    with acc_col3:
                        mu_live_enabled = st.checkbox("Live Enabled", value=True, key="mu_live_enabled")
                        mu_paper_enabled = st.checkbox("Paper Enabled", value=True, key="mu_paper_enabled")
                        mu_risk_mode = st.selectbox("Risk Mode", options=["normal", "reduced", "blocked"], key="mu_risk_mode")

                    mu_allowed_symbols = st.text_input(
                        "Símbolos Permitidos",
                        value="BTC/USDT,ETH/USDT",
                        help="Lista separada por vírgula.",
                        key="mu_allowed_symbols",
                    )
                    mu_allowed_timeframes = st.multiselect(
                        "Timeframes Permitidos",
                        options=["5m", "15m", "30m", "1h", "4h", "1d"],
                        default=["15m", "1h"],
                        key="mu_allowed_timeframes",
                    )
                    mu_account_notes = st.text_area("Notas da Conta", key="mu_account_notes")
                    if st.form_submit_button("Salvar Conta Multiuser"):
                        db.upsert_user_account(
                            {
                                "user_id": int(mu_user_id),
                                "account_id": str(mu_account_id).strip(),
                                "account_alias": str(mu_account_alias or mu_account_id).strip(),
                                "exchange": mu_exchange,
                                "status": mu_status,
                                "live_enabled": bool(mu_live_enabled),
                                "paper_enabled": bool(mu_paper_enabled),
                                "capital_base": float(mu_capital_base),
                                "risk_mode": mu_risk_mode,
                                "allowed_symbols": [item.strip() for item in str(mu_allowed_symbols).split(",") if item.strip()],
                                "allowed_timeframes": list(mu_allowed_timeframes),
                                "notes": mu_account_notes,
                            }
                        )
                        st.success("Conta multiuser salva com sucesso.")

                st.subheader("🛡️ Perfil de Risco")
                with st.form("multiuser_risk_profile_form"):
                    risk_col1, risk_col2, risk_col3 = st.columns(3)
                    with risk_col1:
                        risk_user_id = st.number_input("User ID (Risco)", min_value=1, step=1, key="risk_user_id")
                        risk_account_id = st.text_input("Account ID (Risco)", key="risk_account_id")
                        risk_mode_profile = st.selectbox("Modo", options=["normal", "reduced", "blocked"], key="risk_mode_profile")
                    with risk_col2:
                        max_risk_per_trade = st.number_input("Risco por Trade %", min_value=0.0, value=0.5, step=0.1, key="max_risk_per_trade")
                        max_daily_loss = st.number_input("Loss Diário %", min_value=0.0, value=2.0, step=0.1, key="max_daily_loss")
                        max_drawdown = st.number_input("Drawdown Máx %", min_value=0.0, value=10.0, step=0.5, key="max_drawdown")
                    with risk_col3:
                        max_portfolio_open_risk_pct = st.number_input(
                            "Risco Aberto Máx %",
                            min_value=0.0,
                            value=2.0,
                            step=0.1,
                            key="max_portfolio_open_risk_pct",
                        )
                        allowed_position_count = st.number_input("Máx Posições", min_value=0, value=3, step=1, key="allowed_position_count")
                        leverage_cap = st.number_input("Leverage Cap", min_value=0.0, value=5.0, step=0.5, key="leverage_cap")

                    preferred_symbols = st.text_input(
                        "Símbolos Preferidos",
                        value="BTC/USDT,ETH/USDT",
                        help="Lista separada por vírgula.",
                        key="preferred_symbols",
                    )
                    risk_is_valid = st.checkbox("Risk Profile Válido", value=True, key="risk_is_valid")
                    risk_live_enabled = st.checkbox("Live liberado no risco", value=True, key="risk_live_enabled")
                    risk_paper_enabled = st.checkbox("Paper liberado no risco", value=True, key="risk_paper_enabled")
                    if st.form_submit_button("Salvar Perfil de Risco"):
                        db.upsert_user_risk_profile(
                            {
                                "user_id": int(risk_user_id),
                                "account_id": str(risk_account_id).strip(),
                                "max_risk_per_trade": float(max_risk_per_trade),
                                "max_daily_loss": float(max_daily_loss),
                                "max_drawdown": float(max_drawdown),
                                "max_portfolio_open_risk_pct": float(max_portfolio_open_risk_pct),
                                "allowed_position_count": int(allowed_position_count),
                                "preferred_symbols": [item.strip() for item in str(preferred_symbols).split(",") if item.strip()],
                                "leverage_cap": float(leverage_cap),
                                "risk_mode": risk_mode_profile,
                                "is_valid": bool(risk_is_valid),
                                "live_enabled": bool(risk_live_enabled),
                                "paper_enabled": bool(risk_paper_enabled),
                            }
                        )
                        st.success("Perfil de risco salvo com sucesso.")

                st.subheader("🔑 Credenciais Criptografadas")
                if vault and vault.is_configured():
                    with st.form("multiuser_credentials_form"):
                        cred_col1, cred_col2, cred_col3 = st.columns(3)
                        with cred_col1:
                            cred_user_id = st.number_input("User ID (Credencial)", min_value=1, step=1, key="cred_user_id")
                            cred_account_id = st.text_input("Account ID (Credencial)", key="cred_account_id")
                            cred_exchange = st.selectbox(
                                "Exchange (Credencial)",
                                options=AppConfig.BRAZIL_SUPPORTED_EXCHANGES or ["binance"],
                                key="cred_exchange",
                            )
                        with cred_col2:
                            cred_alias = st.text_input("Alias da Credencial", key="cred_alias")
                            permission_status = st.selectbox("Permission Status", options=["valid", "unknown", "blocked"], key="permission_status")
                            token_status = st.selectbox("Token Status", options=["valid", "unknown", "expired"], key="token_status")
                        with cred_col3:
                            reconciliation_status = st.selectbox("Reconciliation", options=["ok", "unknown", "broken"], key="reconciliation_status")
                            permissions_trade = st.checkbox("Permissão de Trade", value=True, key="permissions_trade")
                            permissions_withdraw = st.checkbox("Permissão de Saque", value=False, key="permissions_withdraw")

                        api_key = st.text_input("API Key", type="password", key="cred_api_key")
                        api_secret = st.text_input("API Secret", type="password", key="cred_api_secret")
                        credential_notes = st.text_area("Notas da Credencial", key="credential_notes")
                        if st.form_submit_button("Salvar Credenciais com Vault"):
                            if api_key and api_secret and cred_account_id:
                                vault.store_exchange_credentials(
                                    db,
                                    user_id=int(cred_user_id),
                                    account_id=str(cred_account_id).strip(),
                                    exchange=str(cred_exchange).strip(),
                                    api_key=api_key,
                                    api_secret=api_secret,
                                    credential_alias=cred_alias,
                                    permissions_read=True,
                                    permissions_trade=bool(permissions_trade),
                                    permissions_withdraw=bool(permissions_withdraw),
                                    permission_status=permission_status,
                                    token_status=token_status,
                                    reconciliation_status=reconciliation_status,
                                    notes=credential_notes,
                                )
                                st.success("Credenciais armazenadas com criptografia.")
                            else:
                                st.error("Informe account_id, api_key e api_secret para salvar as credenciais.")
                else:
                    st.info("Configure o vault para liberar o cadastro seguro de credenciais.")

            if admin_show_summary:
                st.markdown("---")
                st.subheader("Strategy Evaluations")

                evaluation_overview = get_cached_strategy_evaluation_overview(limit=25)
                governance_counts = evaluation_overview.get("governance_counts", {})
                edge_counts = evaluation_overview.get("edge_counts", {})

                eval_col1, eval_col2, eval_col3, eval_col4 = st.columns(4)
                with eval_col1:
                    st.metric("Perfis com Snapshot", evaluation_overview.get("total_strategies", 0))
                with eval_col2:
                    st.metric("Aprovados", governance_counts.get("approved", 0))
                with eval_col3:
                    st.metric("Bloqueados", governance_counts.get("blocked", 0))
                with eval_col4:
                    st.metric("Edge Degradado", edge_counts.get("degraded", 0))

                if evaluation_overview.get("rows"):
                    st.dataframe(
                        build_strategy_evaluation_display_df(evaluation_overview["rows"]),
                        width="stretch",
                        hide_index=True,
                    )
                else:
                    st.info("Nenhum snapshot encontrado em strategy_evaluations.")

            if admin_show_users:
                st.markdown("---")
                st.subheader("👥 Gerenciamento de Usuários")

                users = user_manager.list_users(50)
                if users:
                    users_df = pd.DataFrame(users)

                    if 'joined' in users_df.columns:
                        users_df['joined'] = pd.to_datetime(users_df['joined']).dt.strftime('%d/%m/%Y')
                    if 'last_analysis' in users_df.columns:
                        users_df['last_analysis'] = users_df['last_analysis'].fillna('Nunca')
                        users_df.loc[users_df['last_analysis'] != 'Nunca', 'last_analysis'] = pd.to_datetime(
                            users_df.loc[users_df['last_analysis'] != 'Nunca', 'last_analysis']
                        ).dt.strftime('%d/%m/%Y %H:%M')

                    st.dataframe(users_df, width='stretch', hide_index=True)
                else:
                    st.info("Nenhum usuário encontrado.")

                st.markdown("---")
                st.subheader("🔧 Ações de Usuário")

                col1, col2 = st.columns(2)

                with col1:
                    user_id_upgrade = st.number_input("ID do Usuário para Upgrade", min_value=1, key="upgrade_user")
                    if st.button("💎 Promover para Premium"):
                        if user_manager.upgrade_to_premium(int(user_id_upgrade)):
                            st.success(f"✅ Usuário {user_id_upgrade} promovido para Premium!")
                        else:
                            st.error("❌ Usuário não encontrado")

                with col2:
                    new_admin_id = st.number_input("ID do Novo Admin", min_value=1, key="new_admin")
                    if st.button("👑 Adicionar Admin"):
                        user_manager.add_admin(int(new_admin_id))
                        st.success(f"✅ Usuário {new_admin_id} adicionado como Admin!")

            if admin_show_bots:
                admin_telegram_bot = get_or_init_admin_telegram_bot()

                st.markdown("---")
                st.subheader("🤖 Configuração do Bot Telegram")
                bot_token_admin = st.text_input(
                    "Token do Bot Telegram",
                    type="password",
                    help="Token para o bot interativo do Telegram",
                    key="bot_token_admin"
                )

                col1, col2 = st.columns(2)

                with col1:
                    if st.button("🚀 Configurar Bot") and bot_token_admin:
                        if admin_telegram_bot and admin_telegram_bot.configure(bot_token_admin):
                            st.success("✅ Bot Telegram configurado com sucesso!")
                            st.info("💡 O bot agora está pronto para receber comandos dos usuários!")
                        else:
                            st.error("❌ Erro na configuração do bot")

                with col2:
                    if st.button("📤 Testar Bot") and admin_telegram_bot and admin_telegram_bot.is_configured():
                        try:
                            success, message = run_async_task_sync(admin_telegram_bot.test_connection())
                            if success:
                                st.success(f"✅ {message}")
                            else:
                                st.error(f"❌ {message}")
                        except Exception as e:
                            st.error(f"❌ Erro: {str(e)}")

                if admin_telegram_bot and admin_telegram_bot.is_configured():
                    st.success("🟢 Bot Telegram está ativo e pronto para uso!")
                    st.info("💬 Os usuários podem usar comandos como /analise BTC/USDT")
                elif admin_telegram_bot is None:
                    st.warning("🟡 Bot Telegram admin não pôde ser inicializado neste ambiente")
                else:
                    st.warning("🟡 Bot Telegram não configurado")

                st.markdown("### ▶️ Runtime do Bot Trader")
                trader_bot_state = get_trader_bot_process_state()
                bot_runtime_col1, bot_runtime_col2, bot_runtime_col3 = st.columns(3)
                with bot_runtime_col1:
                    st.metric("Status Runtime", "ON" if trader_bot_state.get("running") else "OFF")
                with bot_runtime_col2:
                    st.metric("PID", trader_bot_state.get("pid") or "-")
                with bot_runtime_col3:
                    st.metric(
                        "Entrypoint",
                        Path(trader_bot_state.get("entrypoint", "bot_runner.py")).name,
                    )

                st.caption(
                    "A operacao de ligar e parar o runtime foi movida para a secao Bot Trader. "
                    "Aqui o admin acompanha apenas o status global do processo."
                )

                st.markdown("---")
                st.subheader("📢 Enviar Comunicado")

                broadcast_msg = st.text_area("Mensagem para todos os usuários", key="broadcast_msg")
                if st.button("📤 Enviar para Todos") and broadcast_msg:
                    st.info("Funcionalidade de broadcast disponível via comando /broadcast no Telegram")

        elif configured_admin_password:
            st.info("🔐 Digite a senha de administrador para acessar o painel")

    # Footer
    st.markdown("---")
    st.markdown("""
    <div style='text-align: center; color: gray;'>
    Trading Signals Dashboard - Desenvolvido com Streamlit | ⚠️ Este sistema é apenas para fins educacionais
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
