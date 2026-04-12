from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Optional

import pandas as pd

from config import ProductionConfig
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

    def __init__(self, symbol: str, timeframe: str, max_candles: int = 500, testnet: bool = False):
        self.symbol = symbol
        self.timeframe = _normalize_stream_timeframe(timeframe)
        self.max_candles = max(200, int(max_candles))
        self.testnet = bool(testnet)

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
            if ts not in self._candles_by_ts:
                self._ordered_ts.append(ts)
            self._candles_by_ts[ts] = row
            while len(self._ordered_ts) > self.max_candles:
                old_ts = self._ordered_ts.popleft()
                self._candles_by_ts.pop(old_ts, None)

            self._last_price = row["close"]
            self._last_message_at = time.time()
            if row["is_closed"]:
                self._last_closed_timestamp = ts

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
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                )
                with websocket:
                    with self._lock:
                        self._connected = True
                        self.last_error = None
                        self.provider = "binance_websocket"
                        self._active_endpoint = endpoint_name
                    self._ready_event.set()
                    retry_seconds = 2

                    while not self._stop_event.is_set():
                        try:
                            raw_payload = websocket.recv(timeout=1)
                        except TimeoutError:
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

        # Segurança operacional: se websocket ainda não tiver buffer, tenta preencher por REST.
        df = fetch_candles(self.symbol, self.timeframe, limit=requested, testnet=self.testnet)
        if "is_closed" not in df.columns:
            df["is_closed"] = True
        return df.reset_index(drop=True)
