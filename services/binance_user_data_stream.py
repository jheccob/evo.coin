from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import config
from config import ProductionConfig
try:
    from websockets.sync.client import connect
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    connect = None
    WEBSOCKETS_AVAILABLE = False

logger = logging.getLogger(__name__)


class BinanceFuturesUserDataStream:
    """Cliente simples do user data stream da Binance Futures com rotacao e keepalive."""

    _TIMESTAMP_ERROR_MARKERS = (
        "-1021",
        "outside of the recvwindow",
        "timestamp for this request",
    )

    def __init__(
        self,
        exchange,
        *,
        testnet: bool = False,
        on_event: Optional[Callable[[dict], None]] = None,
        keepalive_interval_seconds: Optional[int] = None,
        reconnect_after_seconds: Optional[int] = None,
    ):
        self.exchange = exchange
        self.testnet = bool(testnet)
        self.on_event = on_event
        self.keepalive_interval_seconds = int(
            keepalive_interval_seconds or ProductionConfig.BINANCE_USER_STREAM_KEEPALIVE_SECONDS
        )
        self.reconnect_after_seconds = int(
            reconnect_after_seconds or ProductionConfig.BINANCE_USER_STREAM_RECONNECT_SECONDS
        )

        self._listen_key: Optional[str] = None
        self._last_event = None
        self._last_event_at = 0.0
        self._last_error = None
        self._events_processed = 0
        self._connected = False
        self._started_at = 0.0
        self._lock = threading.RLock()
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not WEBSOCKETS_AVAILABLE or connect is None:
            raise RuntimeError("Pacote websockets nao instalado para o user data stream da Binance Futures.")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="binance-user-data-stream",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._close_listen_key()

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        return self._ready_event.wait(timeout)

    def get_status(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "environment": "testnet" if self.testnet else "mainnet",
                "listen_key": self._listen_key,
                "events_processed": self._events_processed,
                "last_event_type": (self._last_event or {}).get("e") if isinstance(self._last_event, dict) else None,
                "last_event_at": self._last_event_at or None,
                "last_error": self._last_error,
                "started_at": self._started_at or None,
            }

    def _build_ws_url(self, listen_key: str) -> str:
        base_url = (
            ProductionConfig.BINANCE_USER_STREAM_TESTNET_WS_URL
            if self.testnet
            else ProductionConfig.BINANCE_USER_STREAM_MAINNET_WS_URL
        )
        return f"{base_url}/{listen_key}"

    @classmethod
    def _is_timestamp_sync_error(cls, exc: Exception) -> bool:
        error_text = str(exc or "").lower()
        return any(marker in error_text for marker in cls._TIMESTAMP_ERROR_MARKERS)

    def _refresh_exchange_time_difference(self) -> None:
        if hasattr(self.exchange, "load_time_difference"):
            self.exchange.load_time_difference()

    def _call_signed_exchange(self, method_name: str, params: Optional[dict] = None):
        method = getattr(self.exchange, method_name)
        request_params = dict(params or {})
        recv_window = int(getattr(config, "BINANCE_RECV_WINDOW_MS", 60000) or 60000)
        if recv_window > 0:
            request_params.setdefault("recvWindow", recv_window)
        try:
            return method(request_params)
        except Exception as exc:
            if not self._is_timestamp_sync_error(exc):
                raise
            logger.warning("Erro de timestamp no user data stream; recalibrando relogio da exchange e tentando novamente.")
            self._refresh_exchange_time_difference()
            return method(request_params)

    def _start_listen_key(self) -> str:
        payload = self._call_signed_exchange("fapiPrivatePostListenKey")
        listen_key = payload.get("listenKey") if isinstance(payload, dict) else None
        if not listen_key:
            raise ConnectionError("Binance nao retornou listenKey para o user data stream.")
        with self._lock:
            self._listen_key = listen_key
        return listen_key

    def _keepalive_listen_key(self):
        with self._lock:
            listen_key = self._listen_key
        if not listen_key:
            return
        self._call_signed_exchange("fapiPrivatePutListenKey", {"listenKey": listen_key})

    def _close_listen_key(self):
        with self._lock:
            listen_key = self._listen_key
            self._listen_key = None
        if not listen_key:
            return
        try:
            self._call_signed_exchange("fapiPrivateDeleteListenKey", {"listenKey": listen_key})
        except Exception as exc:
            logger.warning("Falha ao fechar listenKey da Binance Futures: %s", exc)

    def _dispatch_event(self, payload: dict):
        with self._lock:
            self._last_event = payload
            self._last_event_at = time.time()
            self._events_processed += 1

        callback = self.on_event
        if callback is None:
            return
        callback(payload)

    def _run(self):
        retry_seconds = 2
        while not self._stop_event.is_set():
            websocket = None
            try:
                listen_key = self._start_listen_key()
                ws_url = self._build_ws_url(listen_key)
                self._started_at = time.time()
                next_keepalive_at = time.time() + self.keepalive_interval_seconds
                websocket = connect(
                    ws_url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                )
                with websocket:
                    self._ready_event.set()
                    with self._lock:
                        self._connected = True
                        self._last_error = None

                    while not self._stop_event.is_set():
                        now = time.time()
                        if now >= next_keepalive_at:
                            self._keepalive_listen_key()
                            next_keepalive_at = now + self.keepalive_interval_seconds

                        if (now - self._started_at) >= self.reconnect_after_seconds:
                            raise ConnectionError("Janela de rotacao preventiva do user data stream atingida.")

                        try:
                            raw_payload = websocket.recv(timeout=1)
                        except TimeoutError:
                            continue

                        payload = json.loads(raw_payload)
                        if isinstance(payload, dict):
                            self._dispatch_event(payload)

                retry_seconds = 2
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._last_error = str(exc)
                if self._stop_event.is_set():
                    break
                logger.warning("Falha no user data stream da Binance Futures: %s", exc)
                time.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30)
            finally:
                with self._lock:
                    self._connected = False
                self._close_listen_key()
