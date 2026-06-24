from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Optional

import pandas as pd

from config import BOT_WEBSOCKET_TIMEOUT_SEC, ProductionConfig
from market_data import fetch_candles

try:
    from websockets.sync.client import connect

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    connect = None
    WEBSOCKETS_AVAILABLE = False


def _normalize_stream_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    return raw.replace("/", "").replace(":", "").replace("-", "").lower()


def _normalize_stream_timeframe(timeframe: str) -> str:
    raw = str(timeframe or "15m").strip().lower()
    allowed = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}
    return raw if raw in allowed else "15m"


def _timeframe_to_milliseconds(timeframe: str) -> int:
    normalized = _normalize_stream_timeframe(timeframe)
    unit = normalized[-1]
    value = int(normalized[:-1] or "1")
    if unit == "m":
        return value * 60 * 1000
    if unit == "h":
        return value * 60 * 60 * 1000
    if unit == "d":
        return value * 24 * 60 * 60 * 1000
    return 15 * 60 * 1000


def _candles_to_dataframe(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "is_closed"])

    df = pd.DataFrame(rows)
    for column in ("open", "high", "low", "close", "volume"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True, errors="coerce")
    if "is_closed" not in df.columns:
        df["is_closed"] = False
    df["is_closed"] = df["is_closed"].fillna(False).astype(bool)
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    return df


class StreamlinedTradingBot:
    """Cliente websocket real para klines públicos da Binance com reconexão automática."""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        max_candles: int = 500,
        testnet: bool = False,
        allow_rest_fallback: bool = True,
        bootstrap_df: Optional[pd.DataFrame] = None,
    ):
        self.symbol = symbol
        self.timeframe = _normalize_stream_timeframe(timeframe)
        self.max_candles = max(200, int(max_candles))
        self.testnet = bool(testnet)
        self.allow_rest_fallback = bool(allow_rest_fallback)
        self._timeframe_ms = _timeframe_to_milliseconds(self.timeframe)
        self._stale_stream_timeout_sec = max(float(BOT_WEBSOCKET_TIMEOUT_SEC or 25.0), 20.0)

        self._stream_symbol = _normalize_stream_symbol(symbol)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()

        self._candles_by_ts: dict[int, dict] = {}
        self._ordered_ts: deque[int] = deque()
        self._last_price: Optional[float] = None
        self._last_message_at: float = 0.0
        self._last_closed_timestamp: Optional[int] = None
        self._last_gap_detected_ms: int = 0
        self._reconnect_count: int = 0
        self._last_reconnect_at: float = 0.0
        self._last_gap_repair_at: float = 0.0
        self._last_gap_repair_error: Optional[str] = None
        self._last_rest_refresh_at: float = 0.0
        self._last_rest_refresh_error: Optional[str] = None
        self._connected = False
        self.last_error: Optional[str] = None
        self.provider = "binance_websocket"
        self._active_endpoint = "none"

        if WEBSOCKETS_AVAILABLE and connect is not None:
            self._thread = threading.Thread(
                target=self._run,
                name=f"market-ws-{self._stream_symbol}-{self.timeframe}",
                daemon=True,
            )
            self._thread.start()
        else:
            self.provider = "rest_fallback"
            self.last_error = "Pacote websockets nao instalado; usando REST."

        self._seed_from_dataframe(bootstrap_df)

    def _endpoint_candidates(self) -> list[tuple[str, str]]:
        stream_name = f"{self._stream_symbol}@kline_{self.timeframe}"
        if self.testnet:
            return [
                ("binance_futures_testnet", f"{ProductionConfig.BINANCE_USER_STREAM_TESTNET_WS_URL}/{stream_name}"),
            ]
        return [
            ("binance_futures_mainnet", f"{ProductionConfig.BINANCE_USER_STREAM_MAINNET_WS_URL}/{stream_name}"),
            ("binance_spot_mainnet", f"wss://stream.binance.com:9443/ws/{stream_name}"),
            ("binance_us_spot", f"wss://stream.binance.us:9443/ws/{stream_name}"),
        ]

    def _upsert_kline(self, payload: dict) -> None:
        kline = payload.get("k") if isinstance(payload, dict) else None
        if not isinstance(kline, dict):
            return

        ts = int(kline.get("t") or 0)
        if ts <= 0:
            return

        row = {
            "timestamp": ts,
            "open": float(kline.get("o") or 0.0),
            "high": float(kline.get("h") or 0.0),
            "low": float(kline.get("l") or 0.0),
            "close": float(kline.get("c") or 0.0),
            "volume": float(kline.get("v") or 0.0),
            "is_closed": bool(kline.get("x")),
        }

        with self._lock:
            previous_closed_timestamp = self._last_closed_timestamp
            if ts not in self._candles_by_ts:
                self._ordered_ts.append(ts)
            self._candles_by_ts[ts] = row
            while len(self._ordered_ts) > self.max_candles:
                old_ts = self._ordered_ts.popleft()
                self._candles_by_ts.pop(old_ts, None)

            self._last_price = row["close"]
            self._last_message_at = time.time()
            if row["is_closed"]:
                if previous_closed_timestamp and ts > previous_closed_timestamp + self._timeframe_ms:
                    self._last_gap_detected_ms = ts - previous_closed_timestamp
                self._last_closed_timestamp = ts

    def _repair_recent_gap(self) -> None:
        # Usa um snapshot REST curto apenas para preencher candles fechados recentes
        # perdidos durante queda de conectividade; o fluxo principal continua sendo WS.
        repair_limit = min(max(self.max_candles, 300), 1000)
        try:
            repair_df = fetch_candles(
                self.symbol,
                self.timeframe,
                limit=repair_limit,
                testnet=self.testnet,
            )
            if repair_df is None or repair_df.empty:
                return
            repair_df = repair_df.copy()
            if "timestamp" not in repair_df.columns:
                return
            repair_df["timestamp"] = pd.to_datetime(repair_df["timestamp"], utc=True, errors="coerce")
            for column in ("open", "high", "low", "close", "volume"):
                if column not in repair_df.columns:
                    return
                repair_df[column] = pd.to_numeric(repair_df[column], errors="coerce")
            repair_df = repair_df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
            if repair_df.empty:
                return
            repair_df = repair_df.sort_values("timestamp").tail(repair_limit)
            for _, row in repair_df.iterrows():
                timestamp_ms = int(pd.Timestamp(row["timestamp"]).timestamp() * 1000)
                self._upsert_kline(
                    {
                        "k": {
                            "t": timestamp_ms,
                            "o": float(row["open"]),
                            "h": float(row["high"]),
                            "l": float(row["low"]),
                            "c": float(row["close"]),
                            "v": float(row["volume"]),
                            "x": True,
                        }
                    }
                )
            self._last_gap_repair_at = time.time()
            self._last_gap_repair_error = None
        except Exception as exc:
            self._last_gap_repair_error = str(exc)

    @staticmethod
    def _format_timestamp_label(timestamp_ms: Optional[int]) -> str:
        if timestamp_ms is None:
            return "desconhecido"
        try:
            return pd.to_datetime(timestamp_ms, unit="ms", utc=True).isoformat()
        except Exception:
            return str(timestamp_ms)

    def _latest_snapshot_timestamp_ms(
        self,
        rows: list[dict],
        *,
        include_current_candle: bool,
    ) -> Optional[int]:
        if not rows:
            return None

        selected_rows = rows
        if not include_current_candle:
            selected_rows = [row for row in rows if bool(row.get("is_closed"))]
            if not selected_rows and len(rows) > 1:
                selected_rows = rows[:-1]

        if not selected_rows:
            return None

        try:
            return int(selected_rows[-1].get("timestamp") or 0)
        except (TypeError, ValueError):
            return None

    def _rows_need_refresh(
        self,
        rows: list[dict],
        *,
        include_current_candle: bool,
    ) -> bool:
        latest_ts = self._latest_snapshot_timestamp_ms(
            rows,
            include_current_candle=include_current_candle,
        )
        if latest_ts is None or latest_ts <= 0:
            return True

        now = time.time()
        last_message_age = None
        if self._last_message_at:
            last_message_age = max(0.0, now - self._last_message_at)

        stream_inactive = (
            (not self._connected)
            or self._last_message_at <= 0.0
            or (
                last_message_age is not None
                and last_message_age >= self._stale_stream_timeout_sec
            )
        )
        if not stream_inactive:
            return False

        candle_age_sec = max(0.0, now - (latest_ts / 1000.0))
        max_stale_age_sec = max((self._timeframe_ms / 1000.0) * 2.0, self._stale_stream_timeout_sec)
        return candle_age_sec > max_stale_age_sec

    def _refresh_rows_from_rest(self, *, limit: int) -> pd.DataFrame:
        requested = max(50, min(int(limit or 200), self.max_candles))
        try:
            refresh_df = fetch_candles(
                self.symbol,
                self.timeframe,
                limit=requested,
                testnet=self.testnet,
            )
        except Exception as exc:
            self._last_rest_refresh_error = str(exc)
            raise

        if refresh_df is None or refresh_df.empty:
            self._last_rest_refresh_error = "Fallback REST retornou sem candles."
            raise RuntimeError(self._last_rest_refresh_error)

        working_df = refresh_df.copy()
        if "timestamp" not in working_df.columns:
            self._last_rest_refresh_error = "Fallback REST retornou candles sem timestamp."
            raise RuntimeError(self._last_rest_refresh_error)

        working_df["timestamp"] = pd.to_datetime(working_df["timestamp"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            if column not in working_df.columns:
                self._last_rest_refresh_error = f"Fallback REST retornou candles sem coluna {column}."
                raise RuntimeError(self._last_rest_refresh_error)
            working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

        working_df = working_df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        if working_df.empty:
            self._last_rest_refresh_error = "Fallback REST retornou candles invalidos."
            raise RuntimeError(self._last_rest_refresh_error)

        working_df = working_df.sort_values("timestamp").tail(requested).reset_index(drop=True)
        working_df["is_closed"] = True
        for _, row in working_df.iterrows():
            timestamp_ms = int(pd.Timestamp(row["timestamp"]).timestamp() * 1000)
            self._upsert_kline(
                {
                    "k": {
                        "t": timestamp_ms,
                        "o": float(row["open"]),
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                        "v": float(row["volume"]),
                        "x": True,
                    }
                }
            )

        now = time.time()
        self._last_rest_refresh_at = now
        self._last_rest_refresh_error = None
        self._last_gap_repair_at = now
        self._last_gap_repair_error = None
        return working_df

    def _seed_from_dataframe(self, bootstrap_df: Optional[pd.DataFrame]) -> None:
        if bootstrap_df is None or bootstrap_df.empty:
            return

        working_df = bootstrap_df.copy()
        if "timestamp" not in working_df.columns:
            return

        if pd.api.types.is_numeric_dtype(working_df["timestamp"]):
            working_df["timestamp"] = pd.to_datetime(working_df["timestamp"], unit="ms", utc=True, errors="coerce")
        else:
            working_df["timestamp"] = pd.to_datetime(working_df["timestamp"], utc=True, errors="coerce")

        required_columns = ["timestamp", "open", "high", "low", "close", "volume"]
        for column in required_columns:
            if column not in working_df.columns:
                return
            if column != "timestamp":
                working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

        working_df = working_df.dropna(subset=required_columns)
        if working_df.empty:
            return

        working_df = working_df.sort_values("timestamp").tail(self.max_candles)
        for _, row in working_df.iterrows():
            timestamp_ms = int(pd.Timestamp(row["timestamp"]).timestamp() * 1000)
            self._upsert_kline(
                {
                    "k": {
                        "t": timestamp_ms,
                        "o": float(row["open"]),
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                        "v": float(row["volume"]),
                        "x": True,
                    }
                }
            )
        self._ready_event.set()

    def _run(self) -> None:
        retry_seconds = 2
        endpoint_cursor = 0
        endpoints = self._endpoint_candidates()

        while not self._stop_event.is_set():
            endpoint_name, ws_url = endpoints[endpoint_cursor % len(endpoints)]
            endpoint_cursor += 1
            websocket = None
            try:
                websocket = connect(
                    ws_url,
                    open_timeout=10,
                    ping_interval=None,
                    close_timeout=5,
                )
                with websocket:
                    with self._lock:
                        self._connected = True
                        self.last_error = None
                        self.provider = "binance_websocket"
                        self._active_endpoint = endpoint_name
                        self._reconnect_count += 1
                        self._last_reconnect_at = time.time()
                    self._ready_event.set()
                    retry_seconds = 2
                    self._repair_recent_gap()

                    while not self._stop_event.is_set():
                        try:
                            raw_payload = websocket.recv(timeout=1)
                        except TimeoutError:
                            last_message_age = None
                            if self._last_message_at:
                                last_message_age = max(0.0, time.time() - self._last_message_at)
                            if last_message_age is not None and last_message_age >= self._stale_stream_timeout_sec:
                                raise TimeoutError(
                                    f"Stream sem mensagens por {last_message_age:.1f}s; reconectando websocket."
                                )
                            continue
                        payload = json.loads(raw_payload)
                        if isinstance(payload, dict):
                            if "k" in payload:
                                self._upsert_kline(payload)
                            elif "code" in payload and "msg" in payload:
                                raise ConnectionError(f"{payload.get('code')}: {payload.get('msg')}")
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self.last_error = str(exc)
                    self.provider = "websocket_reconnecting"
                    self._active_endpoint = endpoint_name
                if self._stop_event.is_set():
                    break
                time.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30)
            finally:
                with self._lock:
                    self._connected = False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def get_current_status(self) -> dict:
        with self._lock:
            message_age = None
            if self._last_message_at:
                message_age = max(0.0, time.time() - self._last_message_at)
            return {
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "provider": f"{self.provider}:{self._active_endpoint}",
                "connected": bool(self._connected),
                "candles": len(self._ordered_ts),
                "last_price": self._last_price,
                "last_message_age_sec": message_age,
                "last_closed_timestamp": self._last_closed_timestamp,
                "gap_detected_ms": self._last_gap_detected_ms,
                "reconnect_count": self._reconnect_count,
                "last_reconnect_at": self._last_reconnect_at,
                "last_gap_repair_at": self._last_gap_repair_at,
                "last_gap_repair_error": self._last_gap_repair_error,
                "last_rest_refresh_at": self._last_rest_refresh_at,
                "last_rest_refresh_error": self._last_rest_refresh_error,
                "last_error": self.last_error,
            }

    def _snapshot_rows(self) -> list[dict]:
        with self._lock:
            return [self._candles_by_ts[ts] for ts in self._ordered_ts if ts in self._candles_by_ts]

    def get_market_data(
        self,
        limit: int = 200,
        timeout: float = 20.0,
        include_current_candle: bool = False,
    ) -> pd.DataFrame:
        requested = max(50, min(int(limit or 200), self.max_candles))
        timeout = max(0.0, float(timeout or 0.0))

        deadline = time.time() + timeout
        rows = self._snapshot_rows()
        while not rows and time.time() < deadline and not self._stop_event.is_set():
            time.sleep(0.1)
            rows = self._snapshot_rows()

        if rows and self._rows_need_refresh(rows, include_current_candle=include_current_candle):
            latest_label = self._format_timestamp_label(
                self._latest_snapshot_timestamp_ms(
                    rows,
                    include_current_candle=include_current_candle,
                )
            )
            if not self.allow_rest_fallback:
                raise RuntimeError(
                    "Buffer de mercado desatualizado "
                    f"(ultimo candle {latest_label}) e fallback REST desativado."
                )

            self._refresh_rows_from_rest(limit=requested)
            rows = self._snapshot_rows()
            if self._rows_need_refresh(rows, include_current_candle=include_current_candle):
                refreshed_label = self._format_timestamp_label(
                    self._latest_snapshot_timestamp_ms(
                        rows,
                        include_current_candle=include_current_candle,
                    )
                )
                raise RuntimeError(
                    "Feed de mercado desatualizado apos fallback REST "
                    f"(ultimo candle {refreshed_label})."
                )

        if rows:
            selected_rows = rows[-requested:]
            df = _candles_to_dataframe(selected_rows)
            if not include_current_candle:
                closed_df = df[df["is_closed"]]
                if not closed_df.empty:
                    return closed_df.reset_index(drop=True)
                if len(df) > 1:
                    return df.iloc[:-1].reset_index(drop=True)
            return df.reset_index(drop=True)

        if not self.allow_rest_fallback:
            raise RuntimeError(
                "Sem dados no websocket e fallback REST desativado. "
                "Verifique conectividade do endpoint WS ou carregue bootstrap local."
            )

        # Segurança operacional: se websocket ainda não tiver buffer, tenta preencher por REST.
        df = fetch_candles(self.symbol, self.timeframe, limit=requested, testnet=self.testnet)
        if "is_closed" not in df.columns:
            df["is_closed"] = True
        return df.reset_index(drop=True)
